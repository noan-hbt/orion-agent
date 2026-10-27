"""What is going on in his life right now: situations (D149).

A fact is meant to stay true: « il est allergique aux noix ». Much of what matters in a life
is not like that: he looks for a flat in Lyon until January, Léa is ill this week, the exam
is on the 12th and he is anxious about it. Kept as facts, these never died and piled up;
skipped as « not worth knowing in a month », the important was missed. A situation has a
beginning, what is known of it, the people it involves, an horizon, and an end: settled,
dropped, or past its horizon.

Situations are opened, completed and closed by Orion in conversation (`situation`), by the
memory extractor after a conversation, and closed by the night once their horizon is past.
They are shown in `<maintenant>`, they feed his portrait, and the day's reflection (D150)
reads them first. Events, so rebuilt from the journal; a forgotten source takes its
situations with it.

    situation.opened  situation.updated  situation.closed
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import Event, Trust
from orion.core.hlc import Timestamp

OPENED = "situation.opened"
UPDATED = "situation.updated"
CLOSED = "situation.closed"
SITUATION_KINDS = frozenset({OPENED, UPDATED, CLOSED})

MAX_NOTES = 6
"""The latest notes kept on a situation: the summary carries the rest."""
STALE = timedelta(days=21)
"""Without news this long and without an horizon, a situation is asked about, not assumed."""
EXPIRY_GRACE = timedelta(days=2)
"""A situation closes on its own this long after its horizon."""


class Ending(StrEnum):
    SETTLED = "settled"
    DROPPED = "dropped"
    EXPIRED = "expired"


class SituationOpened(BaseModel):
    model_config = ConfigDict(extra="forbid")

    situation_id: str
    title: str = Field(min_length=3, max_length=120)
    summary: str = Field(min_length=3, max_length=600)
    about: list[str] = Field(default_factory=list[str])
    until: str | None = Field(default=None, description="ISO date it should be over by")


class SituationUpdated(BaseModel):
    model_config = ConfigDict(extra="forbid")

    situation_id: str
    note: str = Field(min_length=3, max_length=400)
    summary: str | None = Field(default=None, max_length=600)
    until: str | None = None


class SituationClosed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    situation_id: str
    ending: Ending = Ending.SETTLED
    outcome: str = Field(default="", max_length=300)


@dataclass
class Situation:
    id: str
    title: str
    summary: str
    about: list[str]
    until: date | None
    opened: datetime
    updated: datetime
    trust: Trust
    notes: list[str] = field(default_factory=list[str])
    ending: Ending | None = None
    outcome: str = ""

    @property
    def open(self) -> bool:
        return self.ending is None

    def stale(self, now: datetime) -> bool:
        return self.open and self.until is None and now - self.updated > STALE

    def expired(self, today: date) -> bool:
        return self.open and self.until is not None and today > self.until + EXPIRY_GRACE

    def line(self) -> str:
        until = f", jusqu'au {self.until:%d/%m}" if self.until else ""
        news = f" Dernier point : {self.notes[-1]}" if self.notes else ""
        return f"- ({self.id}) {self.title}{until} : {self.summary}{news}"


def _date(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


@dataclass
class Situations:
    """Every situation, open or closed. A projection of the journal."""

    name: str = "situations"
    by_id: dict[str, Situation] = field(default_factory=dict[str, Situation])
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.by_id.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.kind not in SITUATION_KINDS or event.redacted:
            return
        payload: dict[str, Any] = event.payload
        at = datetime.fromtimestamp(Timestamp.decode(event.hlc).wall_ms / 1000, UTC)
        sid = str(payload["situation_id"])
        if event.kind == OPENED:
            self.by_id[sid] = Situation(
                id=sid,
                title=str(payload["title"]),
                summary=str(payload["summary"]),
                about=[str(a) for a in cast(list[object], payload.get("about") or [])],
                until=_date(payload.get("until")),
                opened=at,
                updated=at,
                trust=event.trust,
            )
            return
        situation = self.by_id.get(sid)
        if situation is None:
            return
        situation.updated = at
        situation.trust = min(situation.trust, event.trust)
        if event.kind == UPDATED:
            situation.notes = [*situation.notes, str(payload["note"])][-MAX_NOTES:]
            if payload.get("summary"):
                situation.summary = str(payload["summary"])
            if payload.get("until"):
                situation.until = _date(payload["until"])
        else:
            situation.ending = Ending(str(payload.get("ending") or Ending.SETTLED))
            situation.outcome = str(payload.get("outcome") or "")

    def current(self) -> list[Situation]:
        """Open situations, the most recently touched first."""
        return sorted(
            (s for s in self.by_id.values() if s.open), key=lambda s: s.updated, reverse=True
        )

    def recent_ends(self, now: datetime, within: timedelta = timedelta(days=7)) -> list[Situation]:
        return [s for s in self.by_id.values() if not s.open and now - s.updated <= within]


def situations_block(situations: list[Situation], limit: int = 8) -> list[str]:
    """Lines for `<maintenant>`: what is going on in his life."""
    return [s.line() for s in situations[:limit]]
