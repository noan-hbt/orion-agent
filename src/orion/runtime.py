"""Orion runtime: wires the journal, projections, context, agent loop and guardian together.

One owner message becomes a chain of journal events sharing a correlation id:

    message.received → conversation.message (user) → [thread.rebased]
    → (conversation.message assistant/tool, model.usage)* → turn.finished

The thread projection is rebuilt from those events, so the next turn's context is a strict
extension of what was sent before.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
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
from orion.context.threads import MESSAGE_KIND, REBASED_KIND, ThreadProjection
from orion.core.events import NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.guardian.policy import Guardian
from orion.memory.extractor import EXTRACTOR_TRUST, ExtractionReport, MemoryExtractor
from orion.memory.model import FACT_ASSERTED
from orion.memory.render import recall_block, snapshot_block
from orion.memory.service import ForgetReport, MemoryService, Origin
from orion.memory.store import Fact
from orion.providers.base import Message, ModelProvider, Usage
from orion.tools.base import ToolRegistry

log = structlog.get_logger(__name__)


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
    ) -> None:
        self._journal = journal
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

    async def start(self) -> None:
        await self._projections.catch_up()
        if self._memory is not None:
            await self._memory.sync()

    async def handle_owner_message(
        self, thread_id: str, text: str, channel: str, now: datetime | None = None
    ) -> TurnResult:
        correlation_id = new_id()
        when = now or datetime.now(ZoneInfo(self._agent.timezone))
        owner = Provenance(source=f"{channel}:{thread_id}", actor="owner:main")

        received = NewEvent(
            kind="message.received",
            payload={"thread_id": thread_id, "channel": channel, "text": text},
            provenance=owner,
            trust=Trust.OWNER,
            correlation_id=correlation_id,
        )
        await self._projections.catch_up()
        thread = self.threads.get(thread_id)

        events: list[NewEvent] = [received]
        new_base = self._compiler.rebase_point(thread, [render_user_message(text, when, channel)])
        window = thread.messages[new_base if new_base is not None else thread.base :]
        user_message = render_user_message(
            text, when, channel, await self._memory_blocks(text, window)
        )
        if new_base is not None:
            log.info("thread.rebase", thread=thread_id, base=new_base)
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
                thread_id, user_message, Trust.OWNER, correlation_id, received.id, provenance=owner
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

        result: TurnResult | None = None
        async for event in run_agent(
            self._provider,
            self._registry,
            self._guardian,
            messages,
            settings,
            context_trust=thread.window_trust(),
        ):
            match event:
                case MessageEmitted(message=message, trust=trust):
                    await self._journal.append(
                        [
                            self._message_event(
                                thread_id, message, trust, correlation_id, received.id
                            )
                        ]
                    )
                case ModelCalled(response=response):
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
                case Finished(reason=reason, text=final_text, usage=usage, detail=detail):
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
                        owner_text=text,
                        origin_event_id=received.id,
                        day=when.date(),
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

    async def consolidate(self, turn: TurnResult) -> ExtractionReport | None:
        """Run background memory extraction for a finished turn (off the critical path)."""
        if (
            self._memory is None
            or self._extractor is None
            or turn.reason is not StopReason.COMPLETED
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

    async def _memory_blocks(self, text: str, window: list[Message]) -> list[str]:
        """Memory to attach to this message: core memory when a window starts, then any
        relevant facts not already visible in the window. Appended to the user message, never to
        the system prompt, so the cached prefix is untouched."""
        if self._memory is None:
            return []
        blocks: list[str] = []
        if not window:
            snapshot = snapshot_block(
                await self._memory.store.facts(),
                await self._memory.store.commitments(),
                self._snapshot_chars,
            )
            if snapshot:
                blocks.append(snapshot)
        shown = "\n".join(str(m.get("content") or "") for m in window) + "\n".join(blocks)
        relevant = [
            f
            for f in await self._memory.store.search(text, self._recall_limit)
            if f.id not in shown
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
    ) -> NewEvent:
        return self._event(
            MESSAGE_KIND,
            {"thread_id": thread_id, "message": message},
            trust,
            correlation_id,
            causation_id,
            provenance,
        )
