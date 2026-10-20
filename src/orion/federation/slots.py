"""Free time, computed by code: the negotiator never needs a model (D29).

A slot is free when it lies within the owner's meeting hours, on an allowed day, in the future,
and overlaps nothing busy. What the other Orion learns is only which of its proposed starts
work, never why the others do not.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

STEP = timedelta(minutes=30)
SPREAD = timedelta(hours=2)
"""Two slots offered on the same day are at least this far apart."""

Busy = tuple[datetime, datetime]


@dataclass(frozen=True)
class Hours:
    start: time
    end: time
    weekends: bool
    timezone: str

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def is_free(
    slot: datetime, duration: timedelta, busy: Iterable[Busy], hours: Hours, now: datetime
) -> bool:
    local = slot.astimezone(hours.zone)
    end = local + duration
    if slot < now or end.date() != local.date():
        return False
    if not hours.weekends and local.weekday() >= 5:
        return False
    if local.time() < hours.start or end.time() > hours.end:
        return False
    return all(not (slot < b_end and b_start < slot + duration) for b_start, b_end in busy)


def free_slots(
    start: datetime,
    end: datetime,
    duration: timedelta,
    busy: list[Busy],
    hours: Hours,
    now: datetime,
    per_day: int = 2,
    limit: int = 8,
) -> list[datetime]:
    """Earliest free starts in [start, end), spread over the days."""
    zone = hours.zone
    found: list[datetime] = []
    day = start.astimezone(zone).date()
    while day <= end.astimezone(zone).date() and len(found) < limit:
        moment = datetime.combine(day, hours.start, zone)
        today: list[datetime] = []
        while moment + duration <= min(end, datetime.combine(day, hours.end, zone)):
            if (
                moment >= start
                and is_free(moment, duration, busy, hours, now)
                and (not today or moment - today[-1] >= SPREAD)
            ):
                today.append(moment)
                if len(today) == per_day:
                    break
            moment += STEP
        found += today[: limit - len(found)]
        day += timedelta(days=1)
    return found
