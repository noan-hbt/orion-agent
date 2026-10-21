"""Initiative engine v0: Orion speaks first, at the right moment, for a stated reason.

Every tick, deterministic rules turn Orion's state into candidates:

- reminders the owner asked for (`remind_me`), delivered at their time, quiet hours or not;
- digests at fixed times of day, built from the commitments ledger, sent only when there is
  something new to say;
- a heads-up shortly before a commitment that has a precise due time.

Each candidate goes through the same gate: quiet hours, a daily attention budget, and a ledger
of what was already said, so nothing is repeated and a restart never re-sends. Then an arbiter
(`initiative.arbiter`) weighs urgency against the cost of interrupting now: a message that can
wait waits for a break (the end of a meeting). On Sunday evening, Orion tells the owner what it
held back that week. The model only
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
from orion.context.recent import recent_lines
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.initiative.arbiter import Moment, PresenceSource, judge, merge
from orion.initiative.timers import Timer, Timers
from orion.initiative.tools import describe_moment
from orion.memory.model import CommitmentStatus, SettingKey
from orion.memory.store import Commitment, MemoryStore, SettingChange
from orion.providers.base import Message
from orion.runtime import Orion, is_silence

log = structlog.get_logger(__name__)

AWAKE = timedelta(minutes=20)
"""The owner wrote less than this ago: he is up, quiet hours do not hold Orion back."""

DELIVERED = "initiative.delivered"
SUPPRESSED = "initiative.suppressed"
_OWNER_SIDE = {"owner", "orion"}
_IDS = re.compile(r"\([a-z]_[0-9A-Z]+\) ")
CHASE_EVERY_DAYS = 3
VERIFIER_CONTEXT_MESSAGES = 8
VERIFIER_READ_CHARS = 12_000


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


ALMOST_KEY = "almost:"
_NOT_WORTH_REPORTING = {"nothing to say", "quiet hours"}


class Caller(Protocol):
    async def call_owner(self, reason: str, correlation_id: str | None = None) -> str: ...


class ReportSource(Protocol):
    async def reports(self) -> list[Report]:
        """Every finished outcome; the engine's ledger skips those already reported."""
        ...


@dataclass(frozen=True)
class Alert:
    """Something worth an unrequested message (an event about to start…). Unlike a report, it
    is subject to quiet hours and the daily budget, and it is dropped once `expires_at` is
    past."""

    key: str
    notice: str
    why: str
    fallback: str
    trust: Trust = Trust.SYSTEM
    expires_at: datetime | None = None
    importance: float = 0.5
    """0 to 1: how much the owner would mind missing it (the arbiter weighs it)."""
    spoken_to: bool = False
    """Someone wrote to him (one of his contacts, another Orion): passed on, not Orion's own
    idea, so it does not spend the day's budget (D104). Quiet hours and the arbiter still
    apply."""


class AlertSource(Protocol):
    async def alerts(self, now: datetime) -> list[Alert]:
        """Alerts due now; the engine's ledger skips those already handled."""
        ...


@dataclass(frozen=True)
class DigestLine:
    text: str
    trust: Trust = Trust.SYSTEM


class DigestSource(Protocol):
    async def digest(
        self, now: datetime, since: datetime, first_slot: bool, last_slot: bool
    ) -> list[DigestLine]:
        """Lines for the digest due now. `since` is when the previous digest was due."""
        ...


@dataclass(frozen=True)
class InitiativeSettings:
    timezone: str = "Europe/Paris"
    thread_id: str = "owner"
    digests: tuple[time, ...] = (time(8, 30), time(13, 0), time(19, 0))
    digest_window: timedelta = timedelta(hours=2)
    quiet_start: time = time(22, 30)
    quiet_end: time = time(8, 0)
    daily_budget: int = 12
    heads_up: timedelta = timedelta(minutes=60)

    def is_quiet(self, moment: time) -> bool:
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= moment < self.quiet_end
        return moment >= self.quiet_start or moment < self.quiet_end

    def attention_day(self, moment: datetime) -> date:
        """The day a message counts against: from waking to waking. What he reads at 1 a.m.
        belongs to the evening still going on, not to the day he has not started yet."""
        if self.quiet_start > self.quiet_end and moment.time() < self.quiet_end:
            return moment.date() - timedelta(days=1)
        return moment.date()


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
    importance: float = 0.5
    budgeted: bool = True
    """Spends the day's attention budget: what Orion says of its own accord (D104)."""


@dataclass(frozen=True)
class Unsaid:
    """Something Orion decided not to say, for the weekly « ce que j'ai failli te dire »."""

    day: date
    text: str
    reason: str


@dataclass
class Ledger:
    """What Orion already said or decided not to say. A projection of the journal."""

    name: str = "initiative-ledger"
    handled: set[str] = field(default_factory=set[str])
    last_mention: dict[str, date] = field(default_factory=dict[str, date])
    spent: dict[date, int] = field(default_factory=dict[date, int])
    unsaid: list[Unsaid] = field(default_factory=list[Unsaid])
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.handled.clear()
        self.last_mention.clear()
        self.spent.clear()
        self.unsaid.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        if event.redacted:  # forgotten: as if it never happened
            self._seq = event.seq
            return
        payload: dict[str, Any] = event.payload
        if event.kind in (DELIVERED, SUPPRESSED):
            self.handled.add(str(payload["key"]))
        if (
            event.kind == SUPPRESSED
            and payload.get("fallback")
            and payload.get("reason") not in _NOT_WORTH_REPORTING
        ):
            self.unsaid.append(
                Unsaid(
                    date.fromisoformat(str(payload["day"])),
                    str(payload["fallback"]),
                    str(payload.get("reason", "")),
                )
            )
        if event.kind == DELIVERED:
            day = date.fromisoformat(str(payload["day"]))
            for item in payload.get("mentions", []):
                self.last_mention[str(item)] = day
            if not (payload.get("explicit") or payload.get("bundled") or payload.get("unbudgeted")):
                self.spent[day] = self.spent.get(day, 0) + 1
        self._seq = event.seq


def _due_date(c: Commitment) -> date | None:
    if not c.due:
        return None
    try:
        return date.fromisoformat(c.due[:10])
    except ValueError:
        return None


def _at(c: Commitment) -> str:
    """« à 09:00 » when the commitment has an hour."""
    hour = (c.due or "")[11:16]
    return f" à {hour}" if len(hour) == 5 and hour != "00:00" else ""


def readable(lines: Sequence[str]) -> str:
    """Digest lines as the owner reads them when Orion's own wording was not kept: no ids, no
    arrows, what Orion owes itself left out (it is Orion's to do, not his to read), each thing
    once."""
    kept: list[str] = []
    for line in lines:
        text = _IDS.sub("", line)
        if text.startswith("- moi (Orion) → "):
            continue
        text = text.replace("- toi → toi : ", "- ").replace("- toi → ", "- pour ")
        text = text.replace("- moi (Orion)", "- moi")
        if text not in kept:
            kept.append(text)
    return "\n".join(kept)


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
                when = "pour aujourd'hui" + _at(c)
            elif due == today + timedelta(days=1) and last_slot:
                when = "pour demain" + _at(c)
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


def _read_while_writing(messages: Sequence[Message]) -> str:
    """The memory and tool results of the turn that wrote a draft: what it may rest on."""
    parts = [
        str(m.get("content") or "")
        for m in messages
        if m.get("role") in ("user", "tool") and isinstance(m.get("content"), str)
    ]
    read = "\n---\n".join(p for p in parts if p)
    if len(read) <= VERIFIER_READ_CHARS:
        return read
    # The memory comes first in the turn, the searches after: keep both ends.
    head = VERIFIER_READ_CHARS // 3
    return read[:head] + "\n[…]\n" + read[-(VERIFIER_READ_CHARS - head) :]


def _point(lines: str, extra: list[str]) -> str:
    body = "\n".join(part for part in (lines, *extra) if part)
    return f"Pour la suite :\n{body}" if body else ""


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
        alerts: Sequence[AlertSource] = (),
        digests: Sequence[DigestSource] = (),
        presence: Sequence[PresenceSource] = (),
    ) -> None:
        self._presence = list(presence)
        self._waiting: dict[str, datetime] = {}
        """Messages the arbiter held back, since when (asked again each tick)."""
        self._caller = caller
        self._alerts = list(alerts)
        self._digest_sources = list(digests)
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
        ready: list[tuple[Candidate, str]] = []
        for candidate in await self._candidates(now):
            if candidate.explicit:
                if await self._deliver(candidate, now, None):
                    delivered.append(candidate.key)
                continue
            why_now = await self._admit(candidate, now)
            if why_now is not None:
                ready.append((candidate, why_now))
        if len(ready) == 1:
            candidate, why_now = ready[0]
            if await self._deliver(candidate, now, why_now):
                delivered.append(candidate.key)
        elif ready:
            delivered += await self._deliver_together(ready, now)
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
        for alerts in self._alerts:
            found.extend(
                Candidate(
                    key=a.key,
                    notice=a.notice,
                    why=a.why,
                    fallback=a.fallback,
                    trust=a.trust,
                    expires_at=a.expires_at,
                    importance=a.importance,
                    budgeted=not a.spoken_to,
                )
                for a in await alerts.alerts(now)
            )
        commitments = await self._memory.commitments() if self._memory else []
        found.extend(self._heads_up(commitments, now))
        digest = await self._digest(commitments, now)
        if digest is not None:
            found.append(digest)
        almost = self._almost(now)
        if almost is not None:
            found.append(almost)
        return [c for c in found if c.key not in self.ledger.handled]

    def _almost(self, now: datetime) -> Candidate | None:
        """Sunday evening: what Orion held back this week, so the owner can say what it
        should not have kept to itself."""
        if now.weekday() != 6 or now.time() < time(18, 0):
            return None
        week = now.date() - timedelta(days=6)
        unsaid = [u for u in self.ledger.unsaid if u.day >= week][-8:]
        if not unsaid:
            return None
        lines = "\n".join(f"- {u.text} ({u.reason})" for u in unsaid)
        year, number, _ = now.isocalendar()
        return Candidate(
            key=f"{ALMOST_KEY}{year}-W{number:02d}",
            notice="Ce que tu as failli lui dire cette semaine, et pourquoi tu t'es tu. "
            "Résume-le en quelques lignes et demande-lui si quelque chose aurait dû passer :\n"
            + lines,
            why="c'est le point de la semaine sur ce que j'ai gardé pour moi",
            fallback="Cette semaine, j'ai failli te dire :\n" + lines,
            importance=0.4,
            expires_at=datetime.combine(now.date(), time(22, 0), self._zone),
        )

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
                    importance=0.8,
                )
            )
        return found

    async def _digest(self, commitments: list[Commitment], now: datetime) -> Candidate | None:
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
            previous = (
                datetime.combine(now.date(), slots[index - 1], self._zone)
                if index
                else datetime.combine(now.date() - timedelta(days=1), slots[-1], self._zone)
            )
            extra = [
                line
                for source in self._digest_sources
                for line in await source.digest(now, previous, index == 0, last_slot)
            ]
            body = "\n".join([*(line for _, line in lines), *(e.text for e in extra)])
            return Candidate(
                key=key,
                notice=f"Points à signaler dans ce résumé :\n{body}" if body else "",
                why=f"c'est le point de {slot:%H:%M}",
                fallback=_point(readable([line for _, line in lines]), [e.text for e in extra]),
                trust=min(
                    [
                        *(c.trust for c in commitments if c.id in {i for i, _ in lines}),
                        *(e.trust for e in extra),
                    ],
                    default=Trust.SYSTEM,
                ),
                mentions=tuple(i for i, _ in lines),
                expires_at=end,
                importance=0.6,
            )
        return None

    async def _moment(self, now: datetime) -> Moment:
        return merge([await source.presence(now) for source in self._presence])

    async def _admit(self, candidate: Candidate, now: datetime) -> str | None:
        """The gate, then the arbiter: why it should go now, or None (dropped or waiting)."""
        reason = self._gate(candidate, now)
        if reason is not None:
            if reason != "quiet hours":
                self._waiting.pop(candidate.key, None)
                await self._record(SUPPRESSED, candidate, now, reason=reason)
            return None
        judgement = judge(
            candidate.importance,
            now,
            await self._moment(now),
            self.ledger.spent.get(self._settings.attention_day(now), 0),
            self._settings.daily_budget,
            candidate.expires_at,
            self._waiting.get(candidate.key),
        )
        if not judgement.now:
            self._waiting.setdefault(candidate.key, now)
            return None
        self._waiting.pop(candidate.key, None)
        return judgement.reason

    async def _deliver_together(
        self, ready: list[tuple[Candidate, str]], now: datetime
    ) -> list[str]:
        """Several messages due at once (the owner's focus just ended, a break after a long
        meeting): one message, most important first, that counts once against the budget."""
        ready = sorted(ready, key=lambda pair: -pair[0].importance)
        parts = [c for c, _ in ready]
        joined = Candidate(
            key=parts[0].key,
            notice="Plusieurs choses l'attendaient. Un seul message, court, de la plus "
            "importante à la moins importante :\n"
            + "\n".join(f"- {c.notice} (pourquoi : {c.why})" for c in parts),
            why="plusieurs choses t'attendaient",
            fallback="\n".join(c.fallback for c in parts),
            trust=min(c.trust for c in parts),
            mentions=tuple(m for c in parts for m in c.mentions),
            importance=parts[0].importance,
        )
        if not await self._deliver(joined, now, ready[0][1], together=parts[1:]):
            return []
        return [c.key for c in parts]

    async def _deliver(
        self,
        candidate: Candidate,
        now: datetime,
        why_now: str | None,
        together: Sequence[Candidate] = (),
    ) -> bool:
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
            for item in (candidate, *together):
                await self._record(SUPPRESSED, item, now, reason="model chose silence")
            return False
        correlation_id = new_id()
        await self._notifier.notify(text, correlation_id)
        await self._record(
            DELIVERED, candidate, now, correlation_id=correlation_id, how=how, arbiter=why_now
        )
        for item in together:
            await self._record(
                DELIVERED, item, now, correlation_id=correlation_id, how="together", bundled=True
            )
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
                f"Rappel demandé par téléphone : {candidate.timer.text}",
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

    def _owner_is_up(self, now: datetime) -> bool:
        """He wrote a moment ago: quiet hours are for his sleep, not for a conversation."""
        wrote = getattr(self._orion, "owner_wrote_at", None)
        return isinstance(wrote, datetime) and timedelta(0) <= now - wrote < AWAKE

    def _gate(self, candidate: Candidate, now: datetime) -> str | None:
        if not candidate.notice:
            return "nothing to say"
        if candidate.expires_at is not None and now > candidate.expires_at:
            return "missed its window"
        if self._settings.is_quiet(now.time()) and not self._owner_is_up(now):
            return "quiet hours"
        if (
            candidate.budgeted
            and self.ledger.spent.get(self._settings.attention_day(now), 0)
            >= self._settings.daily_budget
        ):
            return "daily attention budget spent"
        return None

    async def _compose(self, candidate: Candidate, now: datetime) -> tuple[str | None, str]:
        """Let Orion write the message in its own voice, then have a different model check it
        against the facts. Any failure falls back to a plain, correct text. Explicit reminders
        are always delivered, even if the model says SILENCE. Returns (text, how)."""
        earlier = list(self._orion.threads.get(self._settings.thread_id).messages)
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
        conversation = "\n".join(recent_lines(earlier, VERIFIER_CONTEXT_MESSAGES))
        for attempt in (1, 2):
            read = _read_while_writing(
                self._orion.threads.get(self._settings.thread_id).messages[len(earlier) :]
            )
            known = facts + (
                f"\nWhat Orion read while writing (its memory, its searches):\n{read}"
                if read
                else ""
            )
            verification = await self._verifier.check(draft, known, conversation)
            if verification.ok:
                return draft, "verified" if attempt == 1 else "verified on a second draft"
            log.warning("initiative.draft_rejected", key=candidate.key, issues=verification.issues)
            if attempt == 2:
                break
            # Orion rewrites it in its own words rather than a canned text going out (D103).
            try:
                again = await self._orion.handle_initiative(
                    self._settings.thread_id,
                    "Ton message n'est pas parti : une relecture y a vu un problème ("
                    + "; ".join(verification.issues)[:500]
                    + "). Réécris-le en t'en tenant à ce que tu sais, ou réponds SILENCE.",
                    candidate.why,
                    candidate.trust,
                    now,
                )
            except Exception:
                log.exception("initiative.rewrite_failed", key=candidate.key)
                break
            if again.reason is not StopReason.COMPLETED or not again.text.strip():
                break
            if is_silence(again.text):
                return (
                    (candidate.fallback, "fallback: silence after a rejection")
                    if (candidate.explicit)
                    else (None, "silence")
                )
            draft = again.text.strip()
        return candidate.fallback, "fallback: " + "; ".join(verification.issues)[:300]

    async def _record(
        self,
        kind: str,
        candidate: Candidate,
        now: datetime,
        reason: str | None = None,
        correlation_id: str | None = None,
        how: str | None = None,
        arbiter: str | None = None,
        bundled: bool = False,
    ) -> None:
        payload: dict[str, object] = {
            "key": candidate.key,
            "why": candidate.why,
            "day": self._settings.attention_day(now).isoformat(),
            "explicit": candidate.explicit,
            "mentions": list(candidate.mentions),
        }
        if reason is not None:
            payload["reason"] = reason
        if how is not None:
            payload["how"] = how
        if arbiter is not None:
            payload["arbiter"] = arbiter
        if bundled:
            payload["bundled"] = True
        if not candidate.budgeted:
            payload["unbudgeted"] = True
        if kind == SUPPRESSED and candidate.fallback:
            payload["fallback"] = candidate.fallback[:300]
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
