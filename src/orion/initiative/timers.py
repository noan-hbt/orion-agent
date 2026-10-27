"""Durable reminders: scheduled in the journal, so they survive restarts.

    timer.scheduled → (timer.fired | timer.cancelled)

`TimerProjection` is rebuilt from those events; nothing about a pending reminder lives only in
memory. Times are stored as ISO 8601 with an explicit offset.

A repeating reminder (D166) is one occurrence at a time: when one fires, the next is scheduled
in the same append, under a new id, counted from the series' first moment (`anchor`) in the
owner's timezone, so "the 31st of each month" falls on the last day of a short month and 9:00
stays 9:00 across a DST change. Cancelling the pending occurrence ends the series.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, get_args
from zoneinfo import ZoneInfo

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner

TIMER_SCHEDULED = "timer.scheduled"
TIMER_FIRED = "timer.fired"
TIMER_CANCELLED = "timer.cancelled"

Repeat = Literal["", "daily", "weekdays", "weekly", "monthly", "yearly"]
REPEATS: tuple[str, ...] = get_args(Repeat)


@dataclass(frozen=True)
class Timer:
    id: str
    due_at: datetime
    text: str
    thread_id: str
    trust: Trust
    scheduled_seq: int
    by_call: bool = False
    repeat: str = ""
    anchor: datetime | None = None
    """The series' first moment, in the owner's timezone; None for a one-off."""


def _shift(anchor: datetime, repeat: str, n: int) -> datetime:
    if repeat in ("daily", "weekdays"):
        return anchor + timedelta(days=n)  # same tzinfo: the wall-clock time is kept
    if repeat == "weekly":
        return anchor + timedelta(weeks=n)
    if repeat in ("monthly", "yearly"):
        months = anchor.month - 1 + (n if repeat == "monthly" else 12 * n)
        year, month = anchor.year + months // 12, months % 12 + 1
        day = min(anchor.day, calendar.monthrange(year, month)[1])
        return anchor.replace(year=year, month=month, day=day)
    raise ValueError(f"unknown repeat {repeat!r}")


def next_due(timer: Timer, after: datetime) -> datetime:
    """The series' first occurrence strictly after both `after` and this one: missed ones
    (Orion was stopped) are skipped, not delivered in a burst."""
    if not timer.repeat or timer.anchor is None:
        raise ValueError(f"{timer.id} does not repeat")
    floor = max(after, timer.due_at)
    n = 1
    while True:
        candidate = _shift(timer.anchor, timer.repeat, n)
        if candidate > floor and (timer.repeat != "weekdays" or candidate.weekday() < 5):
            return candidate
        n += 1


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
                by_call=bool(payload.get("by_call", False)),
                repeat=str(payload.get("repeat", "")),
                anchor=_anchor(payload),
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


def _anchor(payload: dict[str, Any]) -> datetime | None:
    if not payload.get("repeat"):
        return None
    return datetime.fromisoformat(str(payload["anchor"])).astimezone(
        ZoneInfo(str(payload["timezone"]))
    )


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
        by_call: bool = False,
        repeat: str = "",
        timezone: str = "",
    ) -> str:
        if due_at.tzinfo is None:
            raise ValueError("due_at must be timezone-aware")
        if repeat not in REPEATS:
            raise ValueError(f"unknown repeat {repeat!r}")
        if repeat and not timezone:
            raise ValueError("a repeating reminder needs the owner's timezone")
        timer_id = f"r_{new_id()}"
        await self._append(
            TIMER_SCHEDULED,
            _scheduled(timer_id, due_at, text, thread_id, by_call, repeat, due_at, timezone),
            provenance,
            trust,
            correlation_id,
            causation_id,
        )
        return timer_id

    async def fired(
        self, timer_id: str, correlation_id: str | None = None, now: datetime | None = None
    ) -> None:
        """A repeating reminder schedules its next occurrence, after `now`, in the same append."""
        correlation_id = correlation_id or new_id()
        system = Provenance(source="orion:initiative", actor="orion:initiative")
        events = [
            NewEvent(
                kind=TIMER_FIRED,
                payload={"timer_id": timer_id},
                provenance=system,
                trust=Trust.SYSTEM,
                correlation_id=correlation_id,
            )
        ]
        timer = self.projection.get(timer_id)
        if timer is not None and timer.repeat and timer.anchor is not None:
            events.append(
                NewEvent(
                    kind=TIMER_SCHEDULED,
                    payload=_scheduled(
                        f"r_{new_id()}",
                        next_due(timer, now or timer.due_at),
                        timer.text,
                        timer.thread_id,
                        timer.by_call,
                        timer.repeat,
                        timer.anchor,
                        str(timer.anchor.tzinfo),
                    ),
                    provenance=system,
                    trust=timer.trust,  # the owner's request, not the system's
                    correlation_id=correlation_id,
                )
            )
        await self._journal.append(events)
        await self.sync()

    async def cancel(self, timer_id: str, reason: str, provenance: Provenance) -> None:
        await self._append(
            TIMER_CANCELLED,
            {"timer_id": timer_id, "reason": reason},
            provenance,
            Trust.SYSTEM,
            None,
            None,
        )

    async def move(
        self, timer_id: str, due_at: datetime, provenance: Provenance, timezone: str
    ) -> str:
        """Cancel and schedule again, same text and pace; a series restarts from `due_at`."""
        timer = self.projection.get(timer_id)
        if timer is None:
            raise ValueError(f"no pending reminder {timer_id}")
        await self.cancel(timer_id, f"moved to {due_at.isoformat()}", provenance)
        return await self.schedule(
            due_at,
            timer.text,
            timer.thread_id,
            provenance,
            timer.trust,
            by_call=timer.by_call,
            repeat=timer.repeat,
            timezone=timezone,
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


def _scheduled(
    timer_id: str,
    due_at: datetime,
    text: str,
    thread_id: str,
    by_call: bool,
    repeat: str,
    anchor: datetime,
    timezone: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "timer_id": timer_id,
        "due_at": due_at.isoformat(),
        "text": text,
        "thread_id": thread_id,
        "by_call": by_call,
    }
    if repeat:  # a one-off keeps the payload it always had
        payload |= {"repeat": repeat, "anchor": anchor.isoformat(), "timezone": timezone}
    return payload
