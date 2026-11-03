"""Cron lines for hooks at a set time (D225): « minute heure jour mois jour-de-semaine ».

The five usual fields, read in the owner's time zone: `*`, lists (`1,15`), ranges (`9-18`),
steps (`*/15`, `9-18/3`), and names for months and days (`jan`, `lun`/`mon`). Day 0 and 7 are
both Sunday. As in cron, when both the day of the month and the day of the week are
restricted, either one fits. No seconds, no `@reboot`: a hook is never more precise than the
minute.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

_MONTHS = {
    name: i + 1
    for i, names in enumerate(
        [
            ("jan", "janv"),
            ("feb", "fev", "févr", "fevr"),
            ("mar", "mars"),
            ("apr", "avr"),
            ("may", "mai"),
            ("jun", "juin"),
            ("jul", "juil"),
            ("aug", "aou", "août"),
            ("sep", "sept"),
            ("oct",),
            ("nov",),
            ("dec", "déc"),
        ]
    )
    for name in names
}
_DAYS = {
    name: i
    for i, names in enumerate(
        [
            ("sun", "dim"),
            ("mon", "lun"),
            ("tue", "mar"),
            ("wed", "mer"),
            ("thu", "jeu"),
            ("fri", "ven"),
            ("sat", "sam"),
        ]
    )
    for name in names
}
_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
SEARCH = timedelta(days=366 * 4)
"""How far `next_after` looks: a line that never fits (« 0 0 30 2 * ») is said to."""


class CronError(ValueError):
    """A cron line that cannot be read."""


def _value(text: str, field: int) -> int:
    names = _MONTHS if field == 3 else _DAYS if field == 4 else {}
    lowered = text.lower()
    if lowered in names:
        return names[lowered]
    if not text.isdigit():
        raise CronError(f"« {text} » n'est pas un nombre")
    return int(text)


def _field(text: str, field: int) -> frozenset[int]:
    low, high = _BOUNDS[field]
    found: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise CronError("une liste vide")
        step = 1
        if "/" in part:
            part, raw_step = part.split("/", 1)
            if not raw_step.isdigit() or int(raw_step) < 1:
                raise CronError(f"un pas invalide : « {raw_step} »")
            step = int(raw_step)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _value(a, field), _value(b, field)
        else:
            start = _value(part, field)
            end = high if step > 1 else start
        if not (low <= start <= high and low <= end <= high) or start > end:
            raise CronError(f"« {part} » sort de {low}-{high}")
        found.update(range(start, end + 1, step))
    if field == 4 and 7 in found:
        found.discard(7)
        found.add(0)
    return frozenset(found)


@dataclass(frozen=True)
class Cron:
    line: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    any_day: bool
    any_weekday: bool

    @classmethod
    def parse(cls, line: str) -> Cron:
        parts = line.split()
        if len(parts) != 5:
            raise CronError("cinq champs : minute heure jour mois jour-de-semaine")
        fields = [_field(text, i) for i, text in enumerate(parts)]
        return cls(
            " ".join(parts),
            fields[0],
            fields[1],
            fields[2],
            fields[3],
            fields[4],
            parts[2] == "*",
            parts[4] == "*",
        )

    def fits(self, moment: datetime) -> bool:
        """Whether this minute of `moment` (in its own zone) is one of the line's."""
        if (
            moment.minute not in self.minutes
            or moment.hour not in self.hours
            or moment.month not in self.months
        ):
            return False
        day = moment.day in self.days
        weekday = (moment.isoweekday() % 7) in self.weekdays
        if self.any_day or self.any_weekday:
            return day and weekday
        return day or weekday

    def next_after(self, moment: datetime) -> datetime | None:
        """The first minute strictly after `moment` that fits, in moment's zone; None if
        none comes within four years."""
        at = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        end = moment + SEARCH
        while at <= end:
            if at.month not in self.months:
                at = (at.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_fits(at):
                at = at.replace(hour=0, minute=0) + timedelta(days=1)
                continue
            if at.hour not in self.hours:
                at = at.replace(minute=0) + timedelta(hours=1)
                continue
            if at.minute in self.minutes:
                return at
            at += timedelta(minutes=1)
        return None

    def _day_fits(self, at: datetime) -> bool:
        day = at.day in self.days
        weekday = (at.isoweekday() % 7) in self.weekdays
        if self.any_day or self.any_weekday:
            return day and weekday
        return day or weekday


def readable(cron: Cron) -> str:
    """A short French reading for the common lines; the line itself otherwise."""
    m, h = sorted(cron.minutes), sorted(cron.hours)
    if len(m) == 1 and len(h) == 1 and cron.any_day and cron.months == frozenset(range(1, 13)):
        at = f"{h[0]:02d}:{m[0]:02d}"
        if cron.any_weekday:
            return f"tous les jours à {at}"
        names = ["dim", "lun", "mar", "mer", "jeu", "ven", "sam"]
        days = ", ".join(names[d] for d in sorted(cron.weekdays, key=lambda d: (d + 6) % 7))
        return f"{days} à {at}"
    return cron.line
