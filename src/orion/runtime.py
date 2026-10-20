"""Orion runtime: wires the journal, projections, context, agent loop and guardian together.

One owner message becomes a chain of journal events sharing a correlation id:

    message.received → conversation.message (user) → [thread.rebased]
    → (conversation.message assistant/tool, model.usage)* → turn.finished

The thread projection is rebuilt from those events, so the next turn's context is a strict
extension of what was sent before.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog

from orion.cognition.loop import (
    Budget,
    Finished,
    LoopSettings,
    MessageEmitted,
    ModelCalled,
    StopReason,
    ToolExecuted,
    run_agent,
)
from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler, render_user_message
from orion.context.summary import summarize, summary_block
from orion.context.threads import MESSAGE_KIND, REBASED_KIND, Thread, ThreadProjection
from orion.core.events import NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.guardian.policy import TRUSTED_FLOOR, Guardian
from orion.memory.extractor import EXTRACTOR_TRUST, ExtractionReport, MemoryExtractor
from orion.memory.model import FACT_ASSERTED
from orion.memory.render import playbook_delta, recall_block, snapshot_block
from orion.memory.service import ForgetReport, MemoryService, Origin
from orion.memory.store import Fact
from orion.providers.base import Message, ModelProvider, Usage
from orion.tools.base import ToolRegistry

log = structlog.get_logger(__name__)

OWNER_THREAD = "owner"
"""The owner's single conversation thread, shared by every owner channel."""


@dataclass(frozen=True)
class TurnResult:
    text: str
    reason: StopReason
    usage: Usage
    correlation_id: str
    detail: str = ""
    thread_id: str = ""
    owner_text: str = ""
    origin_event_id: str = ""
    day: date | None = None
    steps: int = 0
    tool_seconds: float = 0.0
    interjections: int = 0
    """Owner messages that arrived during the turn and were read before it ended."""


@dataclass(frozen=True)
class Interjection:
    """An owner message that arrived while a turn was running."""

    text: str
    channel: str
    refs: tuple[str, ...] = ()


Interjections = Callable[[], Awaitable[list[Interjection]]]
Progress = Callable[[str, str], Awaitable[None]]
"""(text, correlation id) → delivered to the owner now: what Orion writes beside a tool call,
mid-turn, instead of holding everything for the final answer."""


class Orion:
    def __init__(
        self,
        journal: EventStore,
        provider: ModelProvider,
        registry: ToolRegistry,
        guardian: Guardian,
        compiler: ContextCompiler,
        agent: AgentConfig,
        memory: MemoryService | None = None,
        extractor: MemoryExtractor | None = None,
        snapshot_chars: int = 2400,
        recall_limit: int = 6,
        summary_model: str | None = None,
    ) -> None:
        self._journal = journal
        self._summary_model = summary_model
        """Summarizes what a rebase drops (None: dropped turns are left to memory alone)."""
        self._memory = memory
        self._extractor = extractor
        self._snapshot_chars = snapshot_chars
        self._recall_limit = recall_limit
        self._provider = provider
        self._registry = registry
        self._guardian = guardian
        self._compiler = compiler
        self._agent = agent
        self.threads = ThreadProjection()
        self._projections = ProjectionRunner(journal, [self.threads])
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    async def start(self) -> None:
        await self._projections.catch_up()
        if self._memory is not None:
            await self._memory.sync()

    async def handle_owner_message(
        self,
        thread_id: str,
        text: str,
        channel: str,
        now: datetime | None = None,
        refs: list[str] | None = None,
        interjections: Interjections | None = None,
        progress: Progress | None = None,
    ) -> TurnResult:
        """A message from the owner, on any channel. All owner channels share one thread.
        `refs` are the channel's own message ids, recorded so a restart knows they were handled.
        `interjections` is asked between steps for messages the owner sent meanwhile: they join
        the turn, so Orion can change course instead of finishing a plan the owner revoked."""
        owner = Provenance(source=f"{channel}:{thread_id}", actor="owner:main")
        payload: dict[str, object] = {"thread_id": thread_id, "channel": channel, "text": text}
        if refs:
            payload["refs"] = refs
        return await self._turn(
            thread_id,
            kind="message.received",
            payload=payload,
            provenance=owner,
            trust=Trust.OWNER,
            body=text,
            channel=channel,
            now=now,
            interjections=interjections,
            progress=progress,
        )

    async def handle_initiative(
        self,
        thread_id: str,
        notice: str,
        why: str,
        trust: Trust = Trust.SYSTEM,
        now: datetime | None = None,
    ) -> TurnResult:
        """Orion speaks first: a notice from the initiative engine becomes a turn in the owner's
        thread, so the owner's answer has the context. The reply may be `SILENCE` (nothing worth
        sending), which callers must not deliver."""
        body = f'<initiative pourquoi="{_attr(why)}">\n{notice}\n</initiative>'
        return await self._turn(
            thread_id,
            kind="initiative.triggered",
            payload={"thread_id": thread_id, "notice": notice, "why": why},
            provenance=Provenance(source="orion:initiative", actor="orion:initiative"),
            trust=min(trust, Trust.SYSTEM),
            body=body,
            channel="initiative",
            now=now,
            memory_query=notice,
            owner_text="",
        )

    def lock(self, thread_id: str) -> asyncio.Lock:
        """Turns on one thread are serialized: two channels never interleave a conversation."""
        return self._locks.setdefault(thread_id, asyncio.Lock())

    async def _turn(
        self,
        thread_id: str,
        *,
        kind: str,
        payload: dict[str, object],
        provenance: Provenance,
        trust: Trust,
        body: str,
        channel: str,
        now: datetime | None,
        memory_query: str | None = None,
        owner_text: str | None = None,
        interjections: Interjections | None = None,
        progress: Progress | None = None,
    ) -> TurnResult:
        async with self.lock(thread_id):
            return await self._turn_locked(
                thread_id,
                kind,
                payload,
                provenance,
                trust,
                body,
                channel,
                now,
                memory_query if memory_query is not None else body,
                owner_text if owner_text is not None else body,
                interjections,
                progress,
            )

    async def _turn_locked(
        self,
        thread_id: str,
        kind: str,
        payload: dict[str, object],
        provenance: Provenance,
        trust: Trust,
        body: str,
        channel: str,
        now: datetime | None,
        memory_query: str,
        owner_text: str,
        interjections: Interjections | None = None,
        progress: Progress | None = None,
    ) -> TurnResult:
        correlation_id = new_id()
        when = now or datetime.now(ZoneInfo(self._agent.timezone))
        received = NewEvent(
            kind=kind,
            payload=payload,
            provenance=provenance,
            trust=trust,
            correlation_id=correlation_id,
        )
        await self._projections.catch_up()
        thread = self.threads.get(thread_id)

        events: list[NewEvent] = [received]
        stale = self._untrusted_and_idle(thread, when)
        new_base = (
            len(thread.messages)
            if stale
            else self._compiler.rebase_point(thread, [render_user_message(body, when, channel)])
        )
        window = thread.messages[new_base if new_base is not None else thread.base :]
        blocks = await self._memory_blocks(memory_query, window)
        if new_base is not None and not stale:
            summary = await self._summarize_dropped(thread, new_base)
            if summary:
                blocks.insert(0, summary_block(summary))
        user_message = render_user_message(body, when, channel, blocks)
        if new_base is not None:
            log.info(
                "thread.rebase",
                thread=thread_id,
                base=new_base,
                reason="untrusted content, conversation idle" if stale else "window budget",
            )
            events.append(
                self._event(
                    REBASED_KIND,
                    {"thread_id": thread_id, "base": new_base},
                    Trust.SYSTEM,
                    correlation_id,
                    received.id,
                )
            )
        events.append(
            self._message_event(
                thread_id,
                user_message,
                trust,
                correlation_id,
                received.id,
                provenance=provenance,
                at=when,
            )
        )
        await self._journal.append(events)
        await self._projections.catch_up()

        thread = self.threads.get(thread_id)
        messages = self._compiler.compile(thread, [])
        settings = LoopSettings(
            model=self._agent.model,
            session_id=f"orion:{thread_id}",
            thread_id=thread_id,
            correlation_id=correlation_id,
            origin_event_id=received.id,
            timezone=self._agent.timezone,
            budget=Budget(
                max_steps=self._agent.max_steps,
                max_tool_calls=self._agent.max_tool_calls,
                max_cost_usd=self._agent.max_cost_usd_per_turn,
            ),
        )

        heard: list[str] = []

        async def steer() -> tuple[Message, Trust] | None:
            if interjections is None:
                return None
            items = await interjections()
            if not items:
                return None
            text = "\n".join(item.text for item in items)
            channel_now = items[0].channel
            provenance_now = Provenance(source=f"{channel_now}:{thread_id}", actor="owner:main")
            refs = [ref for item in items for ref in item.refs]
            payload_now: dict[str, object] = {
                "thread_id": thread_id,
                "channel": channel_now,
                "text": text,
                "during_turn": True,
            }
            if refs:
                payload_now["refs"] = refs
            event = self._event(
                "message.received",
                payload_now,
                Trust.OWNER,
                correlation_id,
                received.id,
                provenance=provenance_now,
            )
            message = render_user_message(
                text, now or datetime.now(ZoneInfo(self._agent.timezone)), channel_now
            )
            await self._journal.append(
                [
                    event,
                    self._message_event(
                        thread_id,
                        message,
                        Trust.OWNER,
                        correlation_id,
                        event.id,
                        provenance=provenance_now,
                        at=when,
                    ),
                ]
            )
            heard.append(text)
            log.info("turn.interjection", thread=thread_id, messages=len(items))
            return message, Trust.OWNER

        result: TurnResult | None = None
        async for event in run_agent(
            self._provider,
            self._registry,
            self._guardian,
            messages,
            settings,
            context_trust=thread.window_trust(),
            steer=steer,
        ):
            match event:
                case MessageEmitted(message=message, trust=trust):
                    await self._journal.append(
                        [
                            self._message_event(
                                thread_id, message, trust, correlation_id, received.id, at=when
                            )
                        ]
                    )
                case ModelCalled(response=response):
                    said = (response.text or "").strip()
                    if progress is not None and response.tool_calls and said:
                        await progress(said, correlation_id)
                    await self._journal.append(
                        [
                            self._event(
                                "model.usage",
                                {
                                    "thread_id": thread_id,
                                    "model": response.model,
                                    "provider": response.provider,
                                    "attempts": response.attempts,
                                    "finish_reason": response.finish_reason,
                                    **response.usage.model_dump(),
                                },
                                Trust.SYSTEM,
                                correlation_id,
                                received.id,
                            )
                        ]
                    )
                case ToolExecuted(call=call, result=tool_result, decision_reason=reason):
                    await self._journal.append(
                        [
                            self._event(
                                "tool.executed",
                                {
                                    "thread_id": thread_id,
                                    "tool": call.name,
                                    "arguments": call.arguments,
                                    "is_error": tool_result.is_error,
                                    "decision": reason,
                                },
                                Trust.SYSTEM,
                                correlation_id,
                                received.id,
                            )
                        ]
                    )
                case Finished(
                    reason=reason,
                    text=final_text,
                    usage=usage,
                    detail=detail,
                    steps=steps,
                    tool_seconds=tool_seconds,
                ):
                    await self._journal.append(
                        [
                            self._event(
                                "turn.finished",
                                {
                                    "thread_id": thread_id,
                                    "reason": reason.value,
                                    "detail": detail,
                                    **usage.model_dump(),
                                },
                                Trust.SYSTEM,
                                correlation_id,
                                received.id,
                            )
                        ]
                    )
                    result = TurnResult(
                        final_text,
                        reason,
                        usage,
                        correlation_id,
                        detail,
                        thread_id=thread_id,
                        owner_text="\n".join([owner_text, *heard]) if owner_text else "",
                        origin_event_id=received.id,
                        day=when.date(),
                        steps=steps,
                        tool_seconds=tool_seconds,
                        interjections=len(heard),
                    )

        await self._projections.catch_up()
        if result is None:  # pragma: no cover - run_agent always ends with Finished
            raise RuntimeError("agent loop ended without a Finished event")
        return result

    async def forget_source(self, source_prefix: str, reason: str) -> ForgetReport:
        """Forget a source everywhere: memory, and the conversations that will be replayed."""
        if self._memory is None:
            raise RuntimeError("memory is not configured")
        report = await self._memory.forget_source(source_prefix, reason, actor="owner:main")
        await self._projections.rebuild(self.threads.name)
        return report

    @property
    def journal(self) -> EventStore:
        return self._journal

    async def consolidate(self, turn: TurnResult) -> ExtractionReport | None:
        """Run background memory extraction for a finished turn (off the critical path)."""
        if (
            self._memory is None
            or self._extractor is None
            or turn.reason is not StopReason.COMPLETED
            or not turn.owner_text
        ):
            return None
        recorded: list[Fact] = []
        for event in await self._journal.read(0, 100_000):
            if event.correlation_id == turn.correlation_id and event.kind == FACT_ASSERTED:
                fact = await self._memory.store.fact(str(event.payload["fact_id"]))
                if fact is not None:
                    recorded.append(fact)
        origin = Origin(
            actor="orion:extractor",
            source=f"thread:{turn.thread_id}",
            trust=EXTRACTOR_TRUST,
            derived_from=(turn.origin_event_id,),
            correlation_id=turn.correlation_id,
        )
        return await self._extractor.consolidate(
            turn.owner_text, turn.text, origin, turn.day or date.today(), recorded
        )

    async def _summarize_dropped(self, thread: Thread, new_base: int) -> str | None:
        """What the turns leaving the window were about, if they were all trusted."""
        if self._summary_model is None:
            return None
        dropped = thread.messages[thread.base : new_base]
        trust = min(thread.trusts[thread.base : new_base], default=Trust.OWNER)
        if not dropped or trust < TRUSTED_FLOOR:
            return None
        return await summarize(
            self._provider, self._summary_model, dropped, f"orion:{thread.id}:summary"
        )

    def _untrusted_and_idle(self, thread: Thread, when: datetime) -> bool:
        """Untrusted content stays in the window, and everything written after it is tainted
        too, until the window starts afresh. Do it after a pause in the conversation: the
        content can no longer steer anything, and memory carries what mattered (D26)."""
        idle = self._agent.untrusted_idle_minutes
        return bool(
            idle
            and thread.window()
            and thread.window_trust() < TRUSTED_FLOOR
            and thread.last_at is not None
            and when - thread.last_at >= timedelta(minutes=idle)
        )

    async def _memory_blocks(self, text: str, window: list[Message]) -> list[str]:
        """Memory to attach to this message: core memory and the playbook when a window starts,
        then playbook changes and relevant facts not already visible in the window. Appended to
        the user message, never to the system prompt, so the cached prefix is untouched."""
        if self._memory is None:
            return []
        store = self._memory.store
        rules = await store.rules(include_retired=True)
        settings = list((await store.settings()).values())
        blocks: list[str] = []
        if not window:
            snapshot = snapshot_block(
                await store.facts(),
                await store.commitments(),
                self._snapshot_chars,
                rules=[r for r in rules if r.active],
                settings=settings,
            )
            if snapshot:
                blocks.append(snapshot)
        shown = "\n".join(str(m.get("content") or "") for m in window) + "\n".join(blocks)
        delta = playbook_delta(rules, settings, shown)
        if delta:
            blocks.append(delta)
        relevant = [
            f for f in await self._memory.search(text, self._recall_limit) if f.id not in shown
        ]
        recalled = recall_block(relevant)
        if recalled:
            blocks.append(recalled)
        return blocks

    @staticmethod
    def _event(
        kind: str,
        payload: dict[str, object],
        trust: Trust,
        correlation_id: str,
        causation_id: str,
        provenance: Provenance | None = None,
    ) -> NewEvent:
        return NewEvent(
            kind=kind,
            payload=payload,
            provenance=provenance or Provenance(source="orion:runtime", actor="orion:core"),
            trust=trust,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    def _message_event(
        self,
        thread_id: str,
        message: Message,
        trust: Trust,
        correlation_id: str,
        causation_id: str,
        provenance: Provenance | None = None,
        at: datetime | None = None,
    ) -> NewEvent:
        payload: dict[str, object] = {"thread_id": thread_id, "message": message}
        if at is not None:
            payload["at"] = at.isoformat()
        return self._event(
            MESSAGE_KIND,
            payload,
            trust,
            correlation_id,
            causation_id,
            provenance,
        )


SILENCE = "SILENCE"


def is_silence(text: str) -> bool:
    return text.strip().strip(".").upper() == SILENCE


def _attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("\n", " ")
