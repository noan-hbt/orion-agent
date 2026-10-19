"""Orion runtime: wires the journal, projections, context, agent loop and guardian together.

One owner message becomes a chain of journal events sharing a correlation id:

    message.received → conversation.message (user) → [thread.rebased]
    → (conversation.message assistant/tool, model.usage)* → turn.finished

The thread projection is rebuilt from those events, so the next turn's context is a strict
extension of what was sent before.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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


class Orion:
    def __init__(
        self,
        journal: EventStore,
        provider: ModelProvider,
        registry: ToolRegistry,
        guardian: Guardian,
        compiler: ContextCompiler,
        agent: AgentConfig,
    ) -> None:
        self._journal = journal
        self._provider = provider
        self._registry = registry
        self._guardian = guardian
        self._compiler = compiler
        self._agent = agent
        self.threads = ThreadProjection()
        self._projections = ProjectionRunner(journal, [self.threads])

    async def start(self) -> None:
        await self._projections.catch_up()

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
        user_message = render_user_message(text, when, channel)
        await self._projections.catch_up()
        thread = self.threads.get(thread_id)

        events: list[NewEvent] = [received]
        new_base = self._compiler.rebase_point(thread, [user_message])
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
                    result = TurnResult(final_text, reason, usage, correlation_id, detail)

        await self._projections.catch_up()
        if result is None:  # pragma: no cover - run_agent always ends with Finished
            raise RuntimeError("agent loop ended without a Finished event")
        return result

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
