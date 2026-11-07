"""Calls a crash cut between `tool.started` and `tool.executed` (ORI-007, D271).

The loop journals `tool.started` before a call runs and `tool.executed` once it returns. A
process killed in between leaves the call without an outcome: if it could act outside (an
external effect, or a click on a page), what it did is not known. When the daemon starts, each
such call gets its `tool.executed` with `UNCERTAIN`, and the owner hears of it once. Nothing is
replayed: the conversation already shows the call as unanswered (`context.threads.NO_RESULT`),
and the model is told to check before doing it again.
"""

from __future__ import annotations

from dataclasses import dataclass

from orion.core.events import Event

STARTED = "tool.started"
EXECUTED = "tool.executed"
INTERRUPTED = "interrupted by a restart"


@dataclass(frozen=True)
class CutCall:
    call_id: str
    thread_id: str
    tool: str
    arguments: str
    correlation_id: str
    causation_id: str

    @property
    def key(self) -> str:
        return f"{self.correlation_id}:{self.call_id}"

    @property
    def action_id(self) -> str:
        return f"{self.tool}:{self.correlation_id}"


class CutCalls:
    """Projection: calls that may have acted and have no outcome yet (`open`), and those
    settled as UNCERTAIN after a restart (`settled`, what the owner is told)."""

    name = "cut_calls"

    def __init__(self) -> None:
        self.open: dict[str, CutCall] = {}
        self.settled: dict[str, CutCall] = {}
        self._seq = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.open.clear()
        self.settled.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted or event.kind not in (STARTED, EXECUTED):
            return
        p = event.payload
        call = CutCall(
            call_id=str(p.get("call_id", "")),
            thread_id=str(p.get("thread_id", "")),
            tool=str(p.get("tool", "")),
            arguments=str(p.get("arguments", "")),
            correlation_id=event.correlation_id or "",
            causation_id=event.causation_id or event.id,
        )
        if event.kind == STARTED:
            if p.get("may_act"):
                self.open[call.key] = call
            return
        started = self.open.pop(call.key, None)
        if p.get("decision") == INTERRUPTED:
            self.settled[call.key] = started or call
