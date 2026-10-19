"""The deliberate agent loop.

A generator over model calls and tool executions. It owns no state beyond one run: the caller
supplies the compiled context and journals every message the loop emits, so a run can always
be reconstructed from the journal.

Termination is typed, budgets are enforced in code, repeated identical tool calls are detected,
and every tool call passes through the guardian before it runs.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

import structlog

from orion.core.events import Trust
from orion.guardian.policy import ActionProposal, Guardian
from orion.providers.base import (
    Message,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ToolCall,
    Usage,
)
from orion.tools.base import Tool, ToolContext, ToolInputError, ToolRegistry, ToolResult

log = structlog.get_logger(__name__)


class StopReason(StrEnum):
    COMPLETED = "completed"
    MAX_STEPS = "max_steps"
    BUDGET_EXHAUSTED = "budget_exhausted"
    STALLED = "stalled"
    PROVIDER_ERROR = "provider_error"
    TRUNCATED = "truncated"


@dataclass(frozen=True)
class Budget:
    max_steps: int = 12
    max_tool_calls: int = 40
    max_cost_usd: float = 0.50
    max_identical_calls: int = 2


@dataclass(frozen=True)
class LoopSettings:
    model: str
    session_id: str
    thread_id: str
    correlation_id: str
    origin_event_id: str | None = None
    timezone: str = "Europe/Paris"
    max_tokens: int | None = None
    budget: Budget = field(default_factory=Budget)


# --- events emitted by the loop ---


@dataclass(frozen=True)
class MessageEmitted:
    """A message to append to the conversation, in the exact form sent to the provider."""

    message: Message
    trust: Trust


@dataclass(frozen=True)
class ModelCalled:
    response: ModelResponse


@dataclass(frozen=True)
class ToolExecuted:
    call: ToolCall
    result: ToolResult
    decision_reason: str


@dataclass(frozen=True)
class Finished:
    reason: StopReason
    text: str
    usage: Usage
    detail: str = ""
    steps: int = 0
    """Model calls made."""
    tool_seconds: float = 0.0
    """Wall time spent running tools (the rest of the turn is the model)."""


Steer = Callable[[], Awaitable[tuple[Message, Trust] | None]]
"""Asked between steps for a message that arrived meanwhile (e.g. the owner wrote again while
Orion worked). The caller journals the message before returning it; the loop appends it to
the conversation before the next model call."""

LoopEvent = MessageEmitted | ModelCalled | ToolExecuted | Finished


async def run_agent(
    provider: ModelProvider,
    registry: ToolRegistry,
    guardian: Guardian,
    messages: list[Message],
    settings: LoopSettings,
    context_trust: Trust = Trust.OWNER,
    steer: Steer | None = None,
) -> AsyncIterator[LoopEvent]:
    """Run until the model answers without tool calls, or a limit is reached.

    `messages` is the compiled context; it is extended in place with every emitted message,
    and with every message `steer` hands over between steps.
    """
    budget = settings.budget
    usage = Usage()
    tool_calls_made = 0
    call_counts: Counter[tuple[str, str]] = Counter()
    last_text = ""
    steps = 0
    tool_seconds = 0.0

    def finished(reason: StopReason, text: str, detail: str = "") -> Finished:
        return Finished(reason, text, usage, detail, steps, round(tool_seconds, 3))

    for _step in range(budget.max_steps):
        request = ModelRequest(
            model=settings.model,
            messages=messages,
            tools=registry.specs(),
            session_id=settings.session_id,
            max_tokens=settings.max_tokens,
        )
        try:
            response = await provider.complete(request)
        except ProviderError as exc:
            log.exception("loop.provider_error", thread=settings.thread_id)
            yield finished(StopReason.PROVIDER_ERROR, last_text, str(exc))
            return

        usage = usage + response.usage
        steps += 1
        yield ModelCalled(response)
        messages.append(response.message)
        # The model's output is only as trustworthy as what it read.
        yield MessageEmitted(response.message, min(context_trust, Trust.SYSTEM))
        last_text = response.text or last_text

        if not response.tool_calls:
            reason = (
                StopReason.TRUNCATED if response.finish_reason == "length" else StopReason.COMPLETED
            )
            yield finished(reason, response.text)
            return

        if usage.cost_usd > budget.max_cost_usd:
            yield finished(StopReason.BUDGET_EXHAUSTED, last_text, "cost budget")
            return

        tool_calls_made += len(response.tool_calls)
        if tool_calls_made > budget.max_tool_calls:
            yield finished(StopReason.BUDGET_EXHAUSTED, last_text, "tool budget")
            return

        stalled = False
        for call in response.tool_calls:
            call_counts[(call.name, call.arguments)] += 1
            if call_counts[(call.name, call.arguments)] > budget.max_identical_calls + 1:
                stalled = True

        started = time.monotonic()
        executions = await _execute_calls(
            response.tool_calls, registry, guardian, settings, context_trust, call_counts, budget
        )
        tool_seconds += time.monotonic() - started
        for call, result, reason in executions:
            context_trust = min(context_trust, result.trust)
            tool_message: Message = {
                "role": "tool",
                "tool_call_id": call.id,
                "content": result.content,
            }
            messages.append(tool_message)
            yield ToolExecuted(call, result, reason)
            yield MessageEmitted(tool_message, result.trust)

        if stalled:
            yield finished(StopReason.STALLED, last_text, "repeated identical calls")
            return

        if steer is not None:
            interjection = await steer()
            if interjection is not None:
                message, trust = interjection
                messages.append(message)
                context_trust = min(context_trust, trust)

    yield finished(StopReason.MAX_STEPS, last_text)


async def _execute_calls(
    calls: list[ToolCall],
    registry: ToolRegistry,
    guardian: Guardian,
    settings: LoopSettings,
    context_trust: Trust,
    call_counts: Counter[tuple[str, str]],
    budget: Budget,
) -> list[tuple[ToolCall, ToolResult, str]]:
    """Run consecutive concurrency-safe calls in parallel, everything else one at a time,
    and return results in the original call order."""
    results: list[tuple[ToolCall, ToolResult, str]] = []
    batch: list[ToolCall] = []

    async def run_one(call: ToolCall) -> tuple[ToolCall, ToolResult, str]:
        result, reason = await _execute_one(
            call, registry, guardian, settings, context_trust, call_counts, budget
        )
        return call, result, reason

    async def flush() -> None:
        if batch:
            results.extend(await asyncio.gather(*(run_one(c) for c in batch)))
            batch.clear()

    for call in calls:
        tool = registry.get(call.name)
        if tool is not None and tool.concurrency_safe:
            batch.append(call)
            continue
        await flush()
        results.append(await run_one(call))
    await flush()
    return results


async def _execute_one(
    call: ToolCall,
    registry: ToolRegistry,
    guardian: Guardian,
    settings: LoopSettings,
    context_trust: Trust,
    call_counts: Counter[tuple[str, str]],
    budget: Budget,
) -> tuple[ToolResult, str]:
    if call_counts[(call.name, call.arguments)] > budget.max_identical_calls:
        return (
            ToolResult(
                "You already made this exact call. Its result is above; change approach.",
                is_error=True,
            ),
            "repeat refused",
        )
    tool = registry.get(call.name)
    if tool is None:
        return ToolResult(f"Unknown tool: {call.name}", is_error=True), "unknown tool"
    try:
        arguments = tool.parse(call.arguments)
    except ToolInputError as exc:
        return ToolResult(str(exc), is_error=True), "invalid input"

    decision = await guardian.decide(
        ActionProposal(tool, arguments, context_trust, settings.thread_id)
    )
    if not decision.allowed:
        return ToolResult(f"Refused by policy: {decision.reason}", is_error=True), decision.reason

    ctx = ToolContext(
        thread_id=settings.thread_id,
        correlation_id=settings.correlation_id,
        origin_event_id=settings.origin_event_id,
        context_trust=context_trust,
        timezone=settings.timezone,
        grant=decision.grant,
    )
    try:
        result = await tool.handler(arguments, ctx)
    except Exception as exc:
        log.exception("tool.failed", tool=tool.name)
        return ToolResult(f"Tool error: {type(exc).__name__}: {exc}", is_error=True), "tool error"
    return _clip(result, tool), decision.reason


def _clip(result: ToolResult, tool: Tool) -> ToolResult:
    if len(result.content) <= tool.max_output_chars:
        return result
    kept = result.content[: tool.max_output_chars]
    note = f"\n[truncated: {len(result.content) - len(kept)} more characters not shown]"
    return ToolResult(kept + note, result.is_error, result.data, result.trust)
