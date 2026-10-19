"""Initiative engine v0: Orion speaks first, at the right moment, for a stated reason.

Every tick, deterministic rules turn Orion's state into candidates:

- reminders the owner asked for (`remind_me`), delivered at their time, quiet hours or not;
- digests at fixed times of day, built from the commitments ledger, sent only when there is
  something new to say;
- a heads-up shortly before a commitment that has a precise due time.

Each candidate goes through the same gate: quiet hours, a daily attention budget, and a ledger
of what was already said, so nothing is repeated and a restart never re-sends. The model only
writes the message (or answers `SILENCE`); it does not decide whether to interrupt. Every
decision, including staying silent, is journaled with its reason.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import structlog

from orion.cognition.loop import StopReason
from orion.cognition.verifier import Verifier
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.initiative.timers import Timer, Timers
from orion.initiative.tools import describe_moment
from orion.memory.model import CommitmentStatus, SettingKey
from orion.memory.store import Commitment, MemoryStore, SettingChange
from orion.runtime import Orion, is_silence

log = structlog.get_logger(__name__)

DELIVERED = "initiative.delivered"
SUPPRESSED = "initiative.suppressed"
_OWNER_SIDE = {"owner", "orion"}
_IDS = re.compile(r"\([a-z]_[0-9A-Z]+\) ")
CHASE_EVERY_DAYS = 3


class Notifier(Protocol):
    """Where proactive messages go (the owner's main channel)."""

    async def available(self) -> bool: ...

    async def notify(self, text: str, correlation_id: str) -> None: ...


@dataclass(frozen=True)
class Report:
    """The outcome of something the owner asked for (a delegated task, a call), to pass on."""

    key: str
    notice: str
    why: str
    fallback: str
    trust: Trust = Trust.SYSTEM


class Caller(Protocol):
    async def call_owner(self, reason: str, correlation_id: str | None = None) -> str: ...


class ReportSource(Protocol):
    async def reports(self) -> list[Report]:
        """Every finished outcome; the engine's ledger skips those already reported."""
        ...


@dataclass(frozen=True)
class InitiativeSettings:
    timezone: str = "Europe/Paris"
    thread_id: str = "owner"
    digests: tuple[time, ...] = (time(8, 30), time(13, 0), time(19, 0))
    digest_window: timedelta = timedelta(hours=2)
    quiet_start: time = time(22, 30)
    quiet_end: time = time(8, 0)
    daily_budget: int = 6
    heads_up: timedelta = timedelta(minutes=60)

    def is_quiet(self, moment: time) -> bool:
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= moment < self.quiet_end
        return moment >= self.quiet_start or moment < self.quiet_end


def effective_settings(
    base: InitiativeSettings, changes: dict[SettingKey, SettingChange]
) -> InitiativeSettings:
    """The config file gives defaults; settings the owner changed (or Orion learned) win."""
    updates: dict[str, Any] = {}
    for key, change in changes.items():
        value = change.value
        if key is SettingKey.DIGESTS and isinstance(value, list):
            updates["digests"] = tuple(time.fromisoformat(v) for v in value)
        elif key is SettingKey.QUIET_START and isinstance(value, str):
            updates["quiet_start"] = time.fromisoformat(value)
        elif key is SettingKey.QUIET_END and isinstance(value, str):
            updates["quiet_end"] = time.fromisoformat(value)
        elif key is SettingKey.DAILY_BUDGET and isinstance(value, int):
            updates["daily_budget"] = value
        else:
            log.error("initiative.bad_setting", key=key, value=value)
    return replace(base, **updates)


@dataclass(frozen=True)
class Candidate:
    key: str
    notice: str
    why: str
    fallback: str
    trust: Trust = Trust.SYSTEM
    explicit: bool = False
    """Explicitly requested by the owner: bypasses quiet hours and the budget, never dropped."""
    timer: Timer | None = None
    mentions: tuple[str, ...] = ()
    expires_at: datetime | None = None


@dataclass
class Ledger:
    """What Orion already said or decided not to say. A projection of the journal."""

    name: str = "initiative-ledger"
    handled: set[str] = field(default_factory=set[str])
    last_mention: dict[str, date] = field(default_factory=dict[str, date])
    spent: dict[date, int] = field(default_factory=dict[date, int])
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.handled.clear()
        self.last_mention.clear()
        self.spent.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        if event.redacted:  # forgotten: as if it never happened
            self._seq = event.seq
            return
        payload: dict[str, Any] = event.payload
        if event.kind in (DELIVERED, SUPPRESSED):
            self.handled.add(str(payload["key"]))
        if event.kind == DELIVERED:
            day = date.fromisoformat(str(payload["day"]))
            for item in payload.get("mentions", []):
                self.last_mention[str(item)] = day
            if not payload.get("explicit"):
                self.spent[day] = self.spent.get(day, 0) + 1
        self._seq = event.seq


def _due_date(c: Commitment) -> date | None:
    if not c.due:
        return None
    try:
        return date.fromisoformat(c.due[:10])
    except ValueError:
        return None


def _who(name: str) -> str:
    return {"owner": "toi", "orion": "moi (Orion)"}.get(name, name)


def digest_lines(
    commitments: list[Commitment],
    today: date,
    last_slot: bool,
    last_mention: dict[str, date],
) -> list[tuple[str, str]]:
    """Commitment lines worth mentioning now, with their ids. Pure: easy to test and explain.

    Owner-side items (overdue, due today, or due tomorrow in the last digest of the day) are
    mentioned at most once a day. Things others owe the owner are chased once their date has
    passed, at most every few days.
    """
    lines: list[tuple[str, str]] = []
    for c in commitments:
        due = _due_date(c)
        if c.status is not CommitmentStatus.OPEN or due is None:
            continue
        seen = last_mention.get(c.id)
        if c.debtor in _OWNER_SIDE:
            if seen == today:
                continue
            if due < today:
                when = f"en retard (c'était pour le {due.isoformat()})"
            elif due == today:
                when = "pour aujourd'hui"
            elif due == today + timedelta(days=1) and last_slot:
                when = "pour demain"
            else:
                continue
            lines.append(
                (c.id, f"- ({c.id}) {_who(c.debtor)} → {_who(c.creditor)} : {c.what}, {when}")
            )
        elif c.creditor in _OWNER_SIDE and due <= today:
            if seen is not None and (today - seen).days < CHASE_EVERY_DAYS:
                continue
            lines.append(
                (
                    c.id,
                    f"- ({c.id}) à vérifier : {c.debtor} devait « {c.what} » pour le "
                    f"{due.isoformat()}, toujours pas noté comme fait",
                )
            )
    return lines


class InitiativeEngine:
    def __init__(
        self,
        orion: Orion,
        journal: EventStore,
        timers: Timers,
        notifier: Notifier,
        settings: InitiativeSettings,
        memory: MemoryStore | None = None,
        clock: Callable[[], datetime] | None = None,
        verifier: Verifier | None = None,
        reports: Sequence[ReportSource] = (),
        caller: Caller | None = None,
    ) -> None:
        self._caller = caller
        self._orion = orion
        self._reports = list(reports)
        self._verifier = verifier
        self._journal = journal
        self._timers = timers
        self._notifier = notifier
        self._base = settings
        self._settings = settings
        self._memory = memory
        self._zone = ZoneInfo(settings.timezone)
        self._clock = clock or (lambda: datetime.now(self._zone))
        self.ledger = Ledger()
        self._runner = ProjectionRunner(journal, [self.ledger])
        self._unavailable_logged = False

    async def run(self, interval: float = 30.0) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(interval)

    async def tick(self) -> list[str]:
        """One pass. Returns the keys delivered (for tests and logs)."""
        await self._runner.catch_up()
        await self._timers.sync()
        if self._memory is not None:
            self._settings = effective_settings(self._base, await self._memory.settings())
        now = self._clock().astimezone(self._zone)
        delivered: list[str] = []
        for candidate in await self._candidates(now):
            if await self._process(candidate, now):
                delivered.append(candidate.key)
        return delivered

    async def _candidates(self, now: datetime) -> list[Candidate]:
        found = [self._reminder(t, now) for t in self._timers.projection.due(now)]
        for source in self._reports:
            found.extend(
                Candidate(
                    key=r.key,
                    notice=r.notice,
                    why=r.why,
                    fallback=r.fallback,
                    trust=r.trust,
                    explicit=True,
                )
                for r in await source.reports()
            )
        commitments = await self._memory.commitments() if self._memory else []
        found.extend(self._heads_up(commitments, now))
        digest = self._digest(commitments, now)
        if digest is not None:
            found.append(digest)
        return [c for c in found if c.key not in self.ledger.handled]

    def _reminder(self, timer: Timer, now: datetime) -> Candidate:
        moment = describe_moment(timer.due_at, self._settings.timezone)
        late = now - timer.due_at > timedelta(minutes=15)
        why = f"tu m'as demandé de te le rappeler {moment}" + (
            " (je le transmets en retard, j'étais arrêté)" if late else ""
        )
        return Candidate(
            key=f"timer:{timer.id}",
            notice=f"Rappel demandé par ton propriétaire, prévu {moment} : {timer.text}",
            why=why,
            fallback=f"Rappel : {timer.text}",
            trust=timer.trust,
            explicit=True,
            timer=timer,
        )

    def _heads_up(self, commitments: list[Commitment], now: datetime) -> list[Candidate]:
        found: list[Candidate] = []
        for c in commitments:
            if c.status is not CommitmentStatus.OPEN or c.debtor not in _OWNER_SIDE:
                continue
            if not c.due or len(c.due) <= 10:
                continue  # no precise time: digests cover it
            try:
                due = datetime.fromisoformat(c.due)
            except ValueError:
                continue
            if due.tzinfo is None:
                due = due.replace(tzinfo=self._zone)
            if not timedelta(0) < due - now <= self._settings.heads_up:
                continue
            minutes = int((due - now).total_seconds() // 60)
            found.append(
                Candidate(
                    key=f"heads-up:{c.id}",
                    notice=f"Échéance proche : {_who(c.debtor)} → {_who(c.creditor)} : {c.what}, "
                    f"prévu {describe_moment(due, self._settings.timezone)} ({c.id})",
                    why=f"c'est dans {minutes} min",
                    fallback=f"Dans {minutes} min : {c.what}",
                    trust=c.trust,
                    mentions=(c.id,),
                    expires_at=due,
                )
            )
        return found

    def _digest(self, commitments: list[Commitment], now: datetime) -> Candidate | None:
        slots = sorted(self._settings.digests)
        for index, slot in reversed(list(enumerate(slots))):
            start = datetime.combine(now.date(), slot, self._zone)
            if now < start:
                continue
            key = f"digest:{now.date().isoformat()}:{slot:%H:%M}"
            if key in self.ledger.handled:
                return None
            end = start + self._settings.digest_window
            last_slot = index == len(slots) - 1
            lines = digest_lines(commitments, now.date(), last_slot, self.ledger.last_mention)
            body = "\n".join(line for _, line in lines)
            return Candidate(
                key=key,
                notice=f"Points à signaler dans ce résumé :\n{body}" if lines else "",
                why=f"c'est le point de {slot:%H:%M}",
                fallback=f"Point de {slot:%H:%M} :\n" + _IDS.sub("", body),
                trust=min(
                    (c.trust for c in commitments if c.id in {i for i, _ in lines}),
                    default=Trust.SYSTEM,
                ),
                mentions=tuple(i for i, _ in lines),
                expires_at=end,
            )
        return None

    async def _process(self, candidate: Candidate, now: datetime) -> bool:
        if not candidate.explicit:
            reason = self._gate(candidate, now)
            if reason is not None:
                if reason != "quiet hours":
                    await self._record(SUPPRESSED, candidate, now, reason=reason)
                return False
        if not await self._notifier.available():
            if not self._unavailable_logged:
                log.warning("initiative.no_channel", key=candidate.key)
                self._unavailable_logged = True
            return False
        self._unavailable_logged = False

        if candidate.timer is not None and candidate.timer.by_call and await self._ring(candidate):
            return True
        text, how = await self._compose(candidate, now)
        if text is None:
            await self._record(SUPPRESSED, candidate, now, reason="model chose silence")
            return False
        correlation_id = new_id()
        await self._notifier.notify(text, correlation_id)
        await self._record(DELIVERED, candidate, now, correlation_id=correlation_id, how=how)
        if candidate.timer is not None:
            await self._timers.fired(candidate.timer.id, correlation_id)
        return True

    async def _ring(self, candidate: Candidate) -> bool:
        """A reminder the owner wanted by phone: the voice says it. If the call cannot be
        placed, the reminder falls back to a message (the caller decides, it returns False)."""
        assert candidate.timer is not None
        if self._caller is None:
            return False
        correlation_id = new_id()
        try:
            await self._caller.call_owner(
                f"Rappel qu'il t'a demandé de lui faire par téléphone : {candidate.timer.text}",
                correlation_id,
            )
        except Exception:
            log.exception("initiative.call_failed", key=candidate.key)
            return False
        await self._record(
            DELIVERED, candidate, now=self._clock(), correlation_id=correlation_id, how="call"
        )
        await self._timers.fired(candidate.timer.id, correlation_id)
        return True

    def _gate(self, candidate: Candidate, now: datetime) -> str | None:
        if not candidate.notice:
            return "nothing to say"
        if candidate.expires_at is not None and now > candidate.expires_at:
            return "missed its window"
        if self._settings.is_quiet(now.time()):
            return "quiet hours"
        if self.ledger.spent.get(now.date(), 0) >= self._settings.daily_budget:
            return "daily attention budget spent"
        return None

    async def _compose(self, candidate: Candidate, now: datetime) -> tuple[str | None, str]:
        """Let Orion write the message in its own voice, then have a different model check it
        against the facts. Any failure falls back to a plain, correct text. Explicit reminders
        are always delivered, even if the model says SILENCE. Returns (text, how)."""
        try:
            turn = await self._orion.handle_initiative(
                self._settings.thread_id, candidate.notice, candidate.why, candidate.trust, now
            )
        except Exception:
            log.exception("initiative.compose_failed", key=candidate.key)
            return candidate.fallback, "fallback: compose failed"
        if turn.reason is not StopReason.COMPLETED or not turn.text.strip():
            log.warning("initiative.compose_incomplete", key=candidate.key, reason=turn.reason)
            return candidate.fallback, f"fallback: {turn.reason.value}"
        if is_silence(turn.text):
            if candidate.explicit:
                return candidate.fallback, "fallback: model chose silence"
            return None, "silence"
        draft = turn.text.strip()
        if self._verifier is None:
            return draft, "unverified"
        facts = (
            f"Now: {describe_moment(now, self._settings.timezone)}\n"
            f"Why Orion writes: {candidate.why}\n{candidate.notice}"
        )
        verification = await self._verifier.check(draft, facts)
        if not verification.ok:
            log.warning("initiative.draft_rejected", key=candidate.key, issues=verification.issues)
            return candidate.fallback, "fallback: " + "; ".join(verification.issues)[:300]
        return draft, "verified"

    async def _record(
        self,
        kind: str,
        candidate: Candidate,
        now: datetime,
        reason: str | None = None,
        correlation_id: str | None = None,
        how: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "key": candidate.key,
            "why": candidate.why,
            "day": now.date().isoformat(),
            "explicit": candidate.explicit,
            "mentions": list(candidate.mentions),
        }
        if reason is not None:
            payload["reason"] = reason
        if how is not None:
            payload["how"] = how
        log.info(kind, key=candidate.key, reason=reason)
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=Provenance(source="orion:initiative", actor="orion:initiative"),
                    trust=Trust.SYSTEM,
                    correlation_id=correlation_id or new_id(),
                )
            ]
        )
        await self._runner.catch_up()
