"""Durable reminders: scheduled in the journal, so they survive restarts.

    timer.scheduled → (timer.fired | timer.cancelled)

`TimerProjection` is rebuilt from those events; nothing about a pending reminder lives only in
memory. Times are stored as ISO 8601 with an explicit offset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner

TIMER_SCHEDULED = "timer.scheduled"
TIMER_FIRED = "timer.fired"
TIMER_CANCELLED = "timer.cancelled"


@dataclass(frozen=True)
class Timer:
    id: str
    due_at: datetime
    text: str
    thread_id: str
    trust: Trust
    scheduled_seq: int


class TimerProjection:
    """Pending reminders, in memory, rebuilt from the journal on start."""

    name = "timers"

    def __init__(self) -> None:
        self._pending: dict[str, Timer] = {}
        self._seq = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self._pending.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        if event.redacted:  # forgotten: as if it never happened
            self._seq = event.seq
            return
        payload: dict[str, Any] = event.payload
        if event.kind == TIMER_SCHEDULED:
            timer_id = str(payload["timer_id"])
            self._pending[timer_id] = Timer(
                id=timer_id,
                due_at=datetime.fromisoformat(str(payload["due_at"])),
                text=str(payload["text"]),
                thread_id=str(payload["thread_id"]),
                trust=event.trust,
                scheduled_seq=event.seq,
            )
        elif event.kind in (TIMER_FIRED, TIMER_CANCELLED):
            self._pending.pop(str(payload["timer_id"]), None)
        self._seq = event.seq

    def pending(self) -> list[Timer]:
        return sorted(self._pending.values(), key=lambda t: (t.due_at, t.id))

    def due(self, now: datetime) -> list[Timer]:
        return [t for t in self.pending() if t.due_at <= now]

    def get(self, timer_id: str) -> Timer | None:
        return self._pending.get(timer_id)


class Timers:
    """Writes timer events. Reads go through the projection."""

    def __init__(self, journal: EventStore, projection: TimerProjection) -> None:
        self._journal = journal
        self.projection = projection
        self._runner = ProjectionRunner(journal, [projection])

    async def sync(self) -> None:
        await self._runner.catch_up()

    async def schedule(
        self,
        due_at: datetime,
        text: str,
        thread_id: str,
        provenance: Provenance,
        trust: Trust,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> str:
        if due_at.tzinfo is None:
            raise ValueError("due_at must be timezone-aware")
        timer_id = f"r_{new_id()}"
        await self._append(
            TIMER_SCHEDULED,
            {
                "timer_id": timer_id,
                "due_at": due_at.isoformat(),
                "text": text,
                "thread_id": thread_id,
            },
            provenance,
            trust,
            correlation_id,
            causation_id,
        )
        return timer_id

    async def fired(self, timer_id: str, correlation_id: str | None = None) -> None:
        await self._append(
            TIMER_FIRED,
            {"timer_id": timer_id},
            Provenance(source="orion:initiative", actor="orion:initiative"),
            Trust.SYSTEM,
            correlation_id,
            None,
        )

    async def cancel(self, timer_id: str, reason: str, provenance: Provenance) -> None:
        await self._append(
            TIMER_CANCELLED,
            {"timer_id": timer_id, "reason": reason},
            provenance,
            Trust.SYSTEM,
            None,
            None,
        )

    async def _append(
        self,
        kind: str,
        payload: dict[str, object],
        provenance: Provenance,
        trust: Trust,
        correlation_id: str | None,
        causation_id: str | None,
    ) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=provenance,
                    trust=trust,
                    correlation_id=correlation_id or new_id(),
                    causation_id=causation_id,
                )
            ]
        )
        await self.sync()
