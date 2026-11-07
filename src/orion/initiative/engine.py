"""Initiative engine v0: Orion speaks first, at the right moment, for a stated reason.

Every tick, deterministic rules turn Orion's state into candidates:

- reminders the owner asked for (`remind_me`, through `later`), delivered at their time,
  quiet hours or not;
- digests at fixed times of day, built from the commitments ledger, sent only when there is
  something new to say;
- a heads-up shortly before a commitment that has a precise due time.

Each candidate goes through the same gate: quiet hours, a daily attention budget, and a ledger
of what was already said, so nothing is repeated and a restart never re-sends. Then an arbiter
(`initiative.arbiter`) weighs urgency against the cost of interrupting now: a message that can
wait waits for a break (the end of a meeting). On Sunday evening, Orion tells the owner what it
held back that week.

The rhythm (D144, D146): what Orion has to say of its own accord goes as soon as the arbiter
lets it (`GOES_NOW`); only small things (a find, a question to get to know him, a memory to
check) wait for the next point, unless they would be stale by then, and go in that one
message. A kind of message the owner has not
answered for a while (`IGNORED_AFTER` in a row) is no longer sent: it shows in the Sunday
« ce que j'ai failli te dire », where he can say it should not have stopped. The model only
writes the message (or answers `SILENCE`); it does not decide whether to interrupt. Every
decision, including staying silent, is journaled with its reason.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

import structlog

from orion.cognition.loop import StopReason
from orion.cognition.verifier import Verifier
from orion.context.recent import day_since
from orion.context.threads import MESSAGE_KIND
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.hlc import Timestamp
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.initiative.arbiter import Moment, PresenceSource, judge, merge
from orion.initiative.timers import Timer, Timers
from orion.initiative.tools import describe_moment
from orion.memory.model import CommitmentStatus, SettingKey
from orion.memory.store import Commitment, MemoryStore, SettingChange
from orion.providers.base import Message
from orion.runtime import OWNER_THREAD, Orion, is_silence

log = structlog.get_logger(__name__)

GOES_NOW = 0.45
"""From this importance, what Orion has to say goes as soon as the arbiter lets it; below, a
small thing waits for the next point. 0.75 in D144 held back too much: he found Orion passive."""
IGNORED_AFTER = 4
"""Deliveries of a kind, in a row and within `IGNORED_FOR`, he never engaged with: it stops."""
IGNORED_FOR = timedelta(days=30)
ENGAGED_WITHIN = timedelta(hours=3)
"""A message or a tapback this soon after Orion wrote counts as engaging with it."""
DIGEST_KEY = "digest:"
FEEDBACK_REACTION = "feedback.reaction"
WEIGHED = "initiative.weight"
"""A source's learned importance changed (D269): its new weight and the feedback behind it."""
LEARN_FOR = timedelta(days=30)
"""The feedback a source's weight is learned from: the last 30 days."""
LEARN_FROM = 3
"""Deliveries of a source needed before its weight moves."""
MAX_WEIGHT = 0.2
"""How far feedback moves a source's importance, either way."""
_SCORES = {"thanks": 1.0, "answered": 0.5, "ignored": -0.5, "not_now": -1.0}
_THANKS = re.compile(r"\b(merci|top|g[ée]nial|super|parfait|nickel|excellent|bien vu)\b", re.I)
_NOT_NOW = re.compile(
    r"(pas maintenant|plus tard|pas le moment|pas besoin|laisse tomber|osef|on s'en fout|"
    r"[çc]a m'int[ée]resse pas|arr[êe]te)",
    re.I,
)
_REACTIONS = {"love": "thanks", "like": "thanks", "laugh": "thanks", "emphasize": "thanks"}
_REACTIONS["dislike"] = "not_now"


def feedback_of(event: Event) -> str:
    """What the owner's message or tapback says of what Orion just sent: thanks, not now, or
    simply an answer. A « merci, mais pas maintenant » is a not now."""
    if event.kind == FEEDBACK_REACTION:
        return _REACTIONS.get(str(event.payload.get("reaction")), "answered")
    text = str(event.payload.get("text", ""))
    if _NOT_NOW.search(text):
        return "not_now"
    return "thanks" if _THANKS.search(text) else "answered"


POINT_HEAD = (
    "C'est ton point de {slot}. Un seul message, du plus utile au moins utile, une ou deux "
    "lignes par sujet ; laisse tomber ce qui ne vaut pas sa lecture."
)

JOIN_BEFORE = timedelta(minutes=10)
"""A reminder or a report due this close before a point waits for it: one message, not two."""

AWAKE = timedelta(minutes=20)
"""The owner wrote less than this ago: he is up, quiet hours do not hold Orion back."""

TALKING = timedelta(minutes=10)
"""The owner wrote less than this ago: a conversation is under way (D205)."""

RESTART_KEY = "restart:"
WOKEN_WITHIN = timedelta(minutes=15)
"""Orion spoke in the owner's thread less than this before a restart: he was busy, often with a
plan for after it (a setting to check, work to pick up). The restart wakes him (D165)."""
WOKEN_AT_MOST = 3
"""Restarts in a row that wake him with no owner message between: past this, a restart loop."""
TAIL = 500

DELIVERED = "initiative.delivered"
SUPPRESSED = "initiative.suppressed"
DETECTED = "opportunity.detected"
"""Something worth doing for him was seen, once per thing (D276): its key, type and reason."""
DECIDED = "opportunity.decided"
"""What became of it (D276): said, kept quiet, and why. The model proposes, the rules decide."""
ACTS = ("FAIRE", "PRÉPARER", "DEMANDER", "INFORMER", "SILENCE")
"""What the model may propose for an opportunity (D276)."""
GAVE_WAY = "gave way to him"
"""He wrote while Orion was about to speak, or speaking, first: the initiative waits."""
TRY_AGAIN = "wording failed, tried again later"
"""Orion could not write it (a call given up, a turn cut short): something he did not ask for
comes back at a later tick once, rather than going out as its raw fallback (D231)."""
_OWNER_SIDE = {"owner", "orion"}
_IDS = re.compile(r"\([a-z]_[0-9A-Z]+\) ")
CHASE_EVERY_DAYS = 3
VERIFIER_LOOKBACK = timedelta(hours=24)
"""What the verifier sees of what happened before the draft: the last day, every thread, tool
results included. Eight clipped lines were not enough: on 01/11 the 19:00 point recalled the
afternoon (M. Hernandez, a Gmail draft Orion tried to read) and was rejected twice as invented."""
VERIFIER_JOURNAL_TAIL = 20_000
VERIFIER_DAY_CHARS = 150_000
VERIFIER_LOG_CHARS = 1500
VERIFIER_READ_CHARS = 200_000  # ~50k tokens: a long turn of pages, not a whole thread
VERIFIER_PIECE_CHARS = 40_000


class Notifier(Protocol):
    """Where proactive messages go (the owner's main channel)."""

    async def available(self) -> bool: ...

    async def notify(self, text: str, correlation_id: str) -> None: ...


@runtime_checkable
class SortingNotifier(Notifier, Protocol):
    """A notifier that knows his choices for each kind of notification (D225): whether it is
    sent at all, how it rings, where it goes."""

    def level_of(self, kind: str) -> str:
        """`off`, `muette`, `normale` or `urgente`."""
        ...

    async def notify_as(
        self, text: str, correlation_id: str, kind: str, level: str = ""
    ) -> bool: ...


NOTIFICATION_KINDS = {
    "digest": "point",
    "almost": "point",
    "place": "rappel",
    "birthday": "rappel",
    "task": "compte_rendu",
    "job": "compte_rendu",
    "call": "compte_rendu",
    "booking": "compte_rendu",
    "mission": "compte_rendu",
    "handover": "compte_rendu",
    "kept": "compte_rendu",
    "secretary": "proches",
    "federation": "proches",
    "meeting": "proches",
    "spending": "depense",
    "hook": "hook",
}
"""A candidate's key family → the kind of notification it is; anything else is Orion speaking
of his own accord (`initiative`)."""
OFF = "off"
URGENT = "urgente"


@dataclass(frozen=True)
class Report:
    """The outcome of something the owner asked for (a delegated task, a call), to pass on."""

    key: str
    notice: str
    why: str
    fallback: str
    trust: Trust = Trust.SYSTEM
    notify_kind: str = ""
    """The kind of notification it is (`orion.notifications`); empty: read from its key."""
    level: str = ""
    """How it rings, when its source says (a hook's own level); empty: his choice for its kind."""
    skippable: bool = False
    """Orion may find it pointless by now (done, already said): SILENCE then means nothing goes."""
    settle: Settle | None = None
    """See `Alert.settle`: on a SILENCE, what it returns goes out instead (a hook whose tool
    failed, D264)."""


def together_notice(parts: Sequence[str]) -> str:
    """Several things due at once, as Orion reads them. Seen for real: a morning point carrying
    a warning came out as one unrelated question, and all of it counted as said."""
    return (
        "Plusieurs choses l'attendaient. Un seul message, court, de la plus importante à la "
        "moins importante. Chacune y est en quelques mots, quitte à dépasser ta longueur "
        "habituelle : tout ce qui suit comptera comme dit. Laisse seulement ce qui est déjà "
        "réglé ou ne vaut pas sa lecture ; une alerte ne se laisse pas, et rien d'autre ne prend "
        "sa place :\n" + "\n".join(f"- {part}" for part in parts)
    )


def reminder_notice(moment: str, text: str) -> str:
    """A reminder he asked for, at its time. Orion can read the conversation, so it knows what
    the clock does not: the thing may be done already (D246)."""
    return (
        f"Il t'a demandé de lui rappeler ça, prévu {moment} : {text}\n"
        "C'est le moment. Dis-lui la chose en une phrase, directement, sans le mot « rappel » et "
        "sans parler de ce que tu as programmé. Si la conversation montre que c'est déjà fait, "
        "ou que tu viens de le lui dire, réponds SILENCE."
    )


ALMOST_KEY = "almost:"
_NOT_WORTH_REPORTING = {"nothing to say", "quiet hours", "paused"}


class Caller(Protocol):
    async def call_owner(self, reason: str, correlation_id: str | None = None) -> str: ...


class ReportSource(Protocol):
    async def reports(self) -> list[Report]:
        """Every finished outcome; the engine's ledger skips those already reported."""
        ...


Settle = Callable[[str | None, str], Awaitable[str | None]]


def _settle_all(settles: Sequence[Settle]) -> Settle | None:
    """Alerts sent as one message are each settled (review of #197): what their SILENCE leaves
    to tell him, together."""
    if not settles:
        return None

    async def settle(spoken: str | None, read: str) -> str | None:
        said = [await s(spoken, read) for s in settles]
        return "\n".join(t for t in said if t) or None

    return settle


@dataclass(frozen=True)
class Opportunity:
    """Something the model saw worth doing for him (D276): what it proposes, and why. It does
    not decide: the gate and the arbiter do, and `opportunity.decided` says what came of it."""

    key: str
    """Its source and its object: the same thing seen twice is one opportunity."""
    act: str
    """One of `ACTS`."""
    what: str
    reason: str


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
    notify_kind: str = ""
    level: str = ""
    settle: Settle | None = None
    """Told how it ended: what was said (None: SILENCE) and what Orion read while writing. On
    a SILENCE, what it returns goes out instead: the silence was not borne out (ORION-26)."""
    opportunities: tuple[Opportunity, ...] = ()
    """What the message carries (D276): each is told decided when it goes out or is dropped."""


class AlertSource(Protocol):
    async def alerts(self, now: datetime) -> list[Alert]:
        """Alerts due now; the engine's ledger skips those already handled."""
        ...


@dataclass(frozen=True)
class DigestLine:
    text: str
    """For Orion, who writes the point: it may hold a note to itself."""
    trust: Trust = Trust.SYSTEM
    plain: str = ""
    """What the owner may read as it is, if Orion's own wording fails; empty: nothing. Seen
    for real: the evening recap's note to Orion (« ouvre le point du soir par là ») went out
    as the message."""


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
    paused_until: datetime | None = None
    """He paused Orion (from the app): nothing on its own until then, but the reminders he
    asked for."""
    verifier: bool = True
    """A second model checks what Orion says of its own accord before it goes out; off (from
    the app or the config), drafts go as Orion wrote them."""

    def is_paused(self, now: datetime) -> bool:
        return self.paused_until is not None and now < self.paused_until

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
        elif key is SettingKey.PAUSED_UNTIL and isinstance(value, str):
            updates["paused_until"] = datetime.fromisoformat(value) if value else None
        elif key is SettingKey.VERIFIER and isinstance(value, bool):
            updates["verifier"] = value
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
    skippable: bool = False
    """See `Report.skippable`."""
    """Explicitly requested by the owner: bypasses quiet hours and the budget, never dropped."""
    timer: Timer | None = None
    mentions: tuple[str, ...] = ()
    expires_at: datetime | None = None
    importance: float = 0.5
    budgeted: bool = True
    """Spends the day's attention budget: what Orion says of its own accord (D104)."""
    raw_last: bool = False
    """An alert: its fallback is only a last resort, after a second failed wording (D231)."""
    carries: tuple[Candidate, ...] = ()
    """What waited for this point and goes in the same message (D144)."""
    notify_kind: str = ""
    level: str = ""
    settle: Settle | None = None
    """See `Alert.settle`."""
    opportunities: tuple[Opportunity, ...] = ()
    """See `Alert.opportunities`."""

    @property
    def kind(self) -> str:
        return self.key.split(":", 1)[0]

    @property
    def notification(self) -> str:
        """The kind of notification it is, for his choices of what rings and where (D225)."""
        if self.notify_kind:
            return self.notify_kind
        if self.timer is not None:
            return "initiative" if self.timer.own else "rappel"
        return NOTIFICATION_KINDS.get(self.kind, "initiative")


@dataclass(frozen=True)
class Unsaid:
    """Something Orion decided not to say, for the weekly « ce que j'ai failli te dire »."""

    day: date
    text: str
    reason: str
    trust: Trust = Trust.SYSTEM
    """Of what it was drawn from (a mail's subject is a stranger's words)."""


@dataclass
class Ledger:
    """What Orion already said or decided not to say. A projection of the journal."""

    name: str = "initiative-ledger"
    handled: set[str] = field(default_factory=set[str])
    last_mention: dict[str, date] = field(default_factory=dict[str, date])
    spent: dict[date, int] = field(default_factory=dict[date, int])
    unsaid: list[Unsaid] = field(default_factory=list[Unsaid])
    sent: list[list[Any]] = field(default_factory=list[list[Any]])
    """[when, kind, engaged, feedback] for each delivery of what Orion said of its own accord;
    feedback is « thanks », « not_now » or « answered », empty while none came."""
    weights: dict[str, float] = field(default_factory=dict[str, float])
    """Each source's weight as last journaled (`initiative.weight`)."""
    _seq: int = 0

    def ignored(self, kind: str, now: datetime) -> bool:
        """He has not engaged with this kind of message the last `IGNORED_AFTER` times."""
        recent = [s for s in self.sent if s[1] == kind and now - s[0] <= IGNORED_FOR]
        last = recent[-IGNORED_AFTER:]
        return len(last) == IGNORED_AFTER and not any(s[2] for s in last)

    def feedback(self, kind: str, now: datetime) -> dict[str, int]:
        """What he made of this source's messages over the last 30 days: thanked, answered,
        ignored (no word within `ENGAGED_WITHIN`), not now. Too recent to tell: left out."""
        counts = dict.fromkeys(_SCORES, 0)
        for at, sent_kind, engaged, said in self.sent:
            if sent_kind != kind or not timedelta(0) <= now - at <= LEARN_FOR:
                continue
            if said:
                counts[said] += 1
            elif engaged:  # he answered what came after it, soon after
                counts["answered"] += 1
            elif now - at > ENGAGED_WITHIN:
                counts["ignored"] += 1
        return counts

    def weight(self, kind: str, now: datetime) -> float:
        """How much feedback raises or lowers this source's importance (D269): the mean of
        its scores, scaled to ±`MAX_WEIGHT`, once there are `LEARN_FROM` of them."""
        counts = self.feedback(kind, now)
        total = sum(counts.values())
        if total < LEARN_FROM:
            return 0.0
        mean = sum(_SCORES[k] * n for k, n in counts.items()) / total
        return round(MAX_WEIGHT * mean, 2)

    def kinds(self) -> list[str]:
        return sorted({str(s[1]) for s in self.sent} | set(self.weights))

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.handled.clear()
        self.last_mention.clear()
        self.spent.clear()
        self.unsaid.clear()
        self.sent.clear()
        self.weights.clear()
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
                    event.trust,
                )
            )
        at = datetime.fromtimestamp(Timestamp.decode(event.hlc).wall_ms / 1000, UTC)
        if event.kind == FEEDBACK_REACTION or (
            event.kind == "message.received" and event.provenance.actor == "owner:main"
        ):
            within = [s for s in self.sent if timedelta(0) <= at - s[0] <= ENGAGED_WITHIN]
            for sent in within:
                sent[2] = True
            if within:  # what he says is about the last thing Orion sent
                said = feedback_of(event)
                if said != "answered" or not within[-1][3]:
                    within[-1][3] = said
        if event.kind == DELIVERED and not payload.get("explicit"):
            self.sent.append([at, str(payload["key"]).split(":", 1)[0], False, ""])
        if event.kind == WEIGHED:
            self.weights[str(payload["kind"])] = float(payload["weight"])
        if event.kind == DELIVERED:
            day = date.fromisoformat(str(payload["day"]))
            for item in payload.get("mentions", []):
                self.last_mention[str(item)] = day
            if not (payload.get("explicit") or payload.get("bundled") or payload.get("unbudgeted")):
                self.spent[day] = self.spent.get(day, 0) + 1
        self._seq = event.seq


def weights_lines(ledger: Ledger, now: datetime) -> list[str]:
    """Each source Orion spoke from these 30 days: its learned weight, and what he made of it."""
    lines: list[str] = []
    for kind in ledger.kinds():
        counts = ledger.feedback(kind, now)
        if not sum(counts.values()):
            continue
        weight = ledger.weight(kind, now)
        lines.append(
            f"- {kind} : importance {weight:+.2f} ({counts['thanks']} merci, "
            f"{counts['answered']} répondu, {counts['ignored']} ignoré, "
            f"{counts['not_now']} pas maintenant)"
        )
    return lines


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

    Owner-side items due today, or due tomorrow in the last digest of the day, are mentioned
    at most once a day. Past due, they are asked about once (done, put off, dropped?) and then
    left to his answer: on 05/11 « à faire pour le 2026-11-03 » was still repeated (D250).
    Things others owe the owner are chased once their date has passed, at most every few days.
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
                if seen is not None and seen > due:
                    continue  # already asked since it fell due
                when = (
                    f"en retard (c'était pour le {due.isoformat()}) : demande-lui une fois si "
                    "c'est fait, reporté (nouvelle date) ou abandonné"
                )
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
    """The memory and tool results of the turn that wrote a draft: what it may rest on.

    What Orion looked up comes first, then the context, which a rewrite repeats but goes in
    once. On 30/10 a cut at 12 000 characters dropped the timetable page Orion had read, and
    « salle NAMEK » was rejected as invented twice."""
    looked_up: list[str] = []
    context: list[str] = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, str) or not content:
            continue
        if m.get("role") == "tool":
            looked_up.append(content[:VERIFIER_PIECE_CHARS])
        elif m.get("role") == "user":
            context.append(content[:VERIFIER_PIECE_CHARS])
    return "\n---\n".join(dict.fromkeys(looked_up + context))[:VERIFIER_READ_CHARS]


def _after_review(fallback: str) -> str:
    """What he asked for, once the review blocked Orion's wording: said as such. The raw text
    alone read as Orion's own instruction (30/10, « Rappel (Ecole) : Te dire dans quelle
    salle… » three times) and hid that the asked thing was not done."""
    if not fallback:
        return ""
    return "Ma relecture a bloqué mon message, voici ce que tu m'avais demandé :\n" + fallback


def _loudest(levels: Iterable[str]) -> str:
    """Several messages in one: it rings as the loudest of them."""
    order = ["", "muette", "normale", URGENT]
    known = [level for level in levels if level in order]
    return max(known, key=order.index, default="")


def _point(lines: str, extra: list[str]) -> str:
    """The point as the owner reads it when Orion's own wording failed: what is left to do,
    then what the sources wrote for him (`DigestLine.plain`)."""
    parts = [*extra, f"Pour la suite :\n{lines}" if lines else ""]
    return "\n\n".join(part for part in parts if part)


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
        self._failed_once: set[str] = set()
        """Candidates whose wording failed once (`TRY_AGAIN`)."""
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
        self._woken: Candidate | None = None
        self._booted = False

    async def run(self, interval: float = 30.0) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(interval)

    async def tick(self) -> list[str]:
        """One pass. Returns the keys delivered (for tests and logs)."""
        await self._runner.catch_up()
        await self._timers.sync()
        if not self._booted:
            self._booted = True
            self._woken = await self._restarted(self._clock())
        if self._memory is not None:
            self._settings = effective_settings(self._base, await self._memory.settings())
        now = self._clock().astimezone(self._zone)
        await self._learn(now)
        delivered: list[str] = []
        ready: list[tuple[Candidate, str]] = []
        candidates = await self._candidates(now)
        point = next((c for c in candidates if c.key.startswith(DIGEST_KEY)), None)
        held: list[Candidate] = []
        owed: list[Candidate] = []
        point_soon = self._point_soon(now)
        for candidate in candidates:
            if candidate is point:
                continue
            if candidate.explicit:
                if not self._joins(candidate):
                    if await self._deliver(candidate, now, None):
                        delivered.append(candidate.key)
                elif not point_soon:
                    owed.append(candidate)
                continue
            if self._ignored(candidate, now):
                await self._record(
                    SUPPRESSED, candidate, now, reason="il ne répond plus à ce genre de message"
                )
                continue
            if self._holds(candidate, now, point):
                if candidate.expires_at is not None and now > candidate.expires_at:
                    await self._record(SUPPRESSED, candidate, now, reason="missed its window")
                elif point is not None:
                    held.append(candidate)
                continue
            why_now = await self._admit(candidate, now)
            if why_now is not None:
                ready.append((candidate, why_now))
        if point is not None:
            point = self._with_held(point, held)
            why_now = await self._admit(point, now)
            if why_now is not None:
                ready.append((point, why_now))
        if owed and (ready or len(owed) > 1):
            # What he asked for goes now, quiet hours or not, but in the same message as what
            # else is due: seen for real, two reminders and the 8:30 point in four minutes.
            ready += [(c, "il me l'a demandé") for c in owed]
        elif owed and await self._deliver(owed[0], now, None):
            delivered.append(owed[0].key)
        if len(ready) == 1:
            candidate, why_now = ready[0]
            if await self._deliver(candidate, now, why_now):
                delivered += [candidate.key, *(c.key for c in candidate.carries)]
        elif ready:
            delivered += await self._deliver_together(ready, now)
        return delivered

    async def _learn(self, now: datetime) -> None:
        """Journal each source whose learned weight moved (D269), with the feedback behind it:
        the weight itself is read from the ledger, so it rebuilds from the journal."""
        moved = [
            (kind, self.ledger.weight(kind, now))
            for kind in self.ledger.kinds()
            if self.ledger.weight(kind, now) != self.ledger.weights.get(kind, 0.0)
        ]
        if not moved:
            return
        await self._journal.append(
            [
                NewEvent(
                    kind=WEIGHED,
                    payload={
                        "kind": kind,
                        "weight": weight,
                        "was": self.ledger.weights.get(kind, 0.0),
                        **self.ledger.feedback(kind, now),
                    },
                    provenance=Provenance(source="orion:initiative", actor="orion:initiative"),
                    trust=Trust.SYSTEM,
                )
                for kind, weight in moved
            ]
        )
        await self._runner.catch_up()
        log.info("initiative.weights", moved=dict(moved))

    def learned(self, importance: float, kind: str, now: datetime) -> float:
        """An alert's importance, raised or lowered by what he made of its source lately."""
        return round(min(max(importance + self.ledger.weight(kind, now), 0.0), 1.0), 2)

    def weights_view(self, now: datetime) -> list[str]:
        """Each source, its learned weight and why: for `orion initiative` and the console."""
        return weights_lines(self.ledger, now)

    def _ignored(self, candidate: Candidate, now: datetime) -> bool:
        return (
            candidate.budgeted
            and candidate.importance < GOES_NOW
            and not candidate.key.startswith(DIGEST_KEY)
            and self.ledger.ignored(candidate.kind, now)
        )

    def _holds(self, candidate: Candidate, now: datetime, point: Candidate | None) -> bool:
        """What Orion says of its own accord and can wait waits for the point: now if one is
        open, else the next, unless it would be stale by then."""
        if not candidate.budgeted or candidate.importance >= GOES_NOW or not self._settings.digests:
            return False
        if point is not None:
            return True
        return candidate.expires_at is None or candidate.expires_at > self._next_point(now)

    @staticmethod
    def _joins(candidate: Candidate) -> bool:
        """What he asked for that may share a message: not a call, not a restart, not Orion's
        own note (that is work for Orion, which mostly ends in silence)."""
        if candidate.key.startswith(RESTART_KEY):
            return False
        return candidate.timer is None or not (candidate.timer.by_call or candidate.timer.own)

    def _point_soon(self, now: datetime) -> bool:
        """A point is due in a few minutes: what he asked for waits to go with it."""
        if not self._settings.digests:
            return False
        return self._next_point(now) - now <= JOIN_BEFORE

    def _next_point(self, now: datetime) -> datetime:
        today = [datetime.combine(now.date(), slot, self._zone) for slot in self._settings.digests]
        later = [moment for moment in today if moment > now]
        return min(later) if later else min(today) + timedelta(days=1)

    @staticmethod
    def _with_held(point: Candidate, held: list[Candidate]) -> Candidate:
        """The point, with what waited for it: one message."""
        if not held:
            return point
        held = sorted(held, key=lambda c: -c.importance)
        slot = point.key.rsplit(":", 2)[-2:]
        lines = "\n".join(f"- {c.notice} (pourquoi : {c.why})" for c in held)
        notice = "\n".join(
            part
            for part in (POINT_HEAD.format(slot=":".join(slot)), point.notice, f"Aussi :\n{lines}")
            if part
        )
        return replace(
            point,
            notice=notice,
            fallback="\n\n".join(f for f in (point.fallback, *(c.fallback for c in held)) if f),
            trust=min(point.trust, *(c.trust for c in held)),
            mentions=(*point.mentions, *(m for c in held for m in c.mentions)),
            carries=tuple(held),
        )

    async def _restarted(self, now: datetime) -> Candidate | None:
        """On boot: Orion was talking in the owner's thread just before it stopped, so it is
        woken to carry on. A turn like any initiative: it may answer SILENCE."""
        last = await self._journal.last_seq()
        stopped: datetime | None = None
        woken = 0
        for event in reversed(await self._journal.read(max(0, last - TAIL), TAIL)):
            if event.kind == "message.received":
                break
            if event.kind in (DELIVERED, SUPPRESSED) and str(
                event.payload.get("key", "")
            ).startswith(RESTART_KEY):
                woken += 1
            if (
                stopped is None
                and event.kind == MESSAGE_KIND
                and event.payload.get("thread_id") == self._settings.thread_id
            ):
                stopped = datetime.fromtimestamp(Timestamp.decode(event.hlc).wall_ms / 1000, UTC)
        if stopped is None or not timedelta(0) <= now - stopped <= WOKEN_WITHIN:
            return None
        if woken >= WOKEN_AT_MOST:
            log.warning("initiative.restart_loop", woken=woken)
            return None
        return Candidate(
            key=f"{RESTART_KEY}{last}",
            notice="Tu viens de redémarrer. Si tu avais écrit que tu ferais quelque chose après "
            "ce redémarrage (vérifier qu'un réglage a pris, reprendre une tâche qu'il t'a "
            "demandée, lui dire où tu en es), fais-le maintenant. Un travail laissé en plan "
            "n'est pas une demande de sa part : ne lui prête rien. Sinon, réponds SILENCE.",
            why="je viens de redémarrer, et je travaillais juste avant",
            fallback="",
            explicit=True,
        )

    async def _candidates(self, now: datetime) -> list[Candidate]:
        found = [self._reminder(t, now) for t in self._timers.projection.due(now)]
        if self._woken is not None:
            found.append(self._woken)
        for source in self._reports:
            found.extend(
                Candidate(
                    key=r.key,
                    notice=r.notice,
                    why=r.why,
                    fallback=r.fallback,
                    trust=r.trust,
                    explicit=True,
                    skippable=r.skippable,
                    notify_kind=r.notify_kind,
                    level=r.level,
                    settle=r.settle,
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
                    importance=a.importance
                    if a.spoken_to
                    else self.learned(a.importance, a.key.split(":", 1)[0], now),
                    budgeted=not a.spoken_to,
                    raw_last=True,
                    notify_kind=a.notify_kind,
                    level=a.level,
                    settle=a.settle,
                    opportunities=a.opportunities,
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
            trust=min(u.trust for u in unsaid),
            expires_at=datetime.combine(now.date(), time(22, 0), self._zone),
        )

    def _reminder(self, timer: Timer, now: datetime) -> Candidate:
        moment = describe_moment(timer.due_at, self._settings.timezone)
        if timer.own:  # his own note: nothing goes out unless he writes it (no fallback)
            return Candidate(
                key=f"timer:{timer.id}",
                notice=f"Note que tu t'étais laissée pour {moment} : {timer.text}\nFais ce "
                "qu'elle dit. Écris-lui seulement si ça le concerne vraiment ; sinon, réponds "
                "SILENCE. Ce n'est pas un rappel qu'il t'a demandé : ne lui recopie pas la note.",
                why=f"je m'étais noté de m'en occuper {moment}",
                fallback="",
                trust=timer.trust,
                explicit=True,
                timer=timer,
            )
        late = now - timer.due_at > timedelta(minutes=15)
        why = f"tu m'as demandé de te le rappeler {moment}" + (
            " (je le transmets en retard, j'étais arrêté)" if late else ""
        )
        return Candidate(
            key=f"timer:{timer.id}",
            notice=reminder_notice(moment, timer.text),
            why=why,
            fallback=f"Rappel : {timer.text}",
            trust=timer.trust,
            explicit=True,
            skippable=True,
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
            ahead = due.astimezone(UTC) - now.astimezone(UTC)  # real time, across a change of hour
            if not timedelta(0) < ahead <= self._settings.heads_up:
                continue
            minutes = int(ahead.total_seconds() // 60)
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
                fallback=_point(readable([line for _, line in lines]), [e.plain for e in extra]),
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
        moment = merge([await source.presence(now) for source in self._presence])
        wrote = getattr(self._orion, "owner_wrote_at", None)
        talking = isinstance(wrote, datetime) and timedelta(0) <= now - wrote < TALKING
        return replace(moment, talking=talking)

    async def _admit(self, candidate: Candidate, now: datetime) -> str | None:
        """The gate, then the arbiter: why it should go now, or None (dropped or waiting)."""
        reason = self._gate(candidate, now)
        if reason is not None:
            if reason not in ("quiet hours", "paused"):  # it waits, it is not dropped
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
        ready = sorted(ready, key=lambda pair: (not pair[0].explicit, -pair[0].importance))
        parts = [c for c, _ in ready]
        carried = [item for c in parts for item in c.carries]
        asked = [c for c in parts if c.explicit]
        joined = Candidate(
            key=parts[0].key,
            notice=together_notice(tuple(f"{c.notice} (pourquoi : {c.why})" for c in parts)),
            why="plusieurs choses t'attendaient",
            # What he asked for goes out even if Orion's wording fails; the rest does not.
            fallback="\n".join(c.fallback for c in (asked or parts)),
            trust=min(c.trust for c in parts),
            explicit=bool(asked),
            skippable=all(c.skippable for c in asked),
            timer=parts[0].timer,
            mentions=tuple(m for c in parts for m in c.mentions),
            importance=parts[0].importance,
            notify_kind=parts[0].notification,
            level=_loudest(self._level(c) for c in parts),
            settle=_settle_all([c.settle for c in (*parts, *carried) if c.settle is not None]),
            opportunities=parts[0].opportunities,
        )
        if not await self._deliver(joined, now, ready[0][1], together=[*parts[1:], *carried]):
            return []
        return [c.key for c in (*parts, *carried)]

    async def _deliver(
        self,
        candidate: Candidate,
        now: datetime,
        why_now: str | None,
        together: Sequence[Candidate] = (),
    ) -> bool:
        together = (*together, *candidate.carries)
        if not await self._notifier.available():
            if not self._unavailable_logged:
                log.warning("initiative.no_channel", key=candidate.key)
                self._unavailable_logged = True
            return False
        self._unavailable_logged = False

        if candidate.timer is not None and candidate.timer.by_call and await self._ring(candidate):
            return True
        text, how = await self._compose(candidate, now)
        if how in (GAVE_WAY, TRY_AGAIN):
            return False  # neither said nor dropped: it comes back at a later tick
        if not text:  # silence, or a failure with nothing the owner could read as it is
            for item in (candidate, *together):
                await self._record(
                    SUPPRESSED, item, now, reason="model chose silence" if how == "silence" else how
                )
            await self._fired((candidate, *together), None, now)
            return False
        correlation_id = new_id()
        if isinstance(self._notifier, SortingNotifier):
            await self._notifier.notify_as(
                text, correlation_id, candidate.notification, candidate.level
            )
        else:
            await self._notifier.notify(text, correlation_id)
        await self._record(
            DELIVERED, candidate, now, correlation_id=correlation_id, how=how, arbiter=why_now
        )
        if candidate.settle is not None:
            # Gone out: Orion's reply, or the line a SILENCE asked for, told only now (#197).
            await candidate.settle(text, "")
        for item in together:
            await self._record(
                DELIVERED, item, now, correlation_id=correlation_id, how="together", bundled=True
            )
        await self._fired((candidate, *together), correlation_id, now)
        return True

    async def _fired(
        self, items: Sequence[Candidate], correlation_id: str | None, now: datetime
    ) -> None:
        """A reminder handled, said or not: a series schedules its next occurrence."""
        for item in items:
            if item.timer is not None:
                await self._timers.fired(item.timer.id, correlation_id, now)

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
        await self._timers.fired(candidate.timer.id, correlation_id, self._clock())
        return True

    def _owner_is_up(self, now: datetime) -> bool:
        """He wrote a moment ago: quiet hours are for his sleep, not for a conversation."""
        wrote = getattr(self._orion, "owner_wrote_at", None)
        return isinstance(wrote, datetime) and timedelta(0) <= now - wrote < AWAKE

    def _level(self, candidate: Candidate) -> str:
        """How it rings, by his choice for its kind (D225); "" without such choices."""
        if candidate.level:
            return candidate.level
        if isinstance(self._notifier, SortingNotifier):
            return self._notifier.level_of(candidate.notification)
        return ""

    def _gate(self, candidate: Candidate, now: datetime) -> str | None:
        if not candidate.notice:
            return "nothing to say"
        if candidate.expires_at is not None and now > candidate.expires_at:
            return "missed its window"
        level = self._level(candidate)
        if level == OFF:
            return "he turned this kind of message off"
        if self._settings.is_paused(now):
            return "paused"
        if self._settings.is_quiet(now.time()) and not self._owner_is_up(now) and level != URGENT:
            return "quiet hours"
        if (
            candidate.budgeted
            and self.ledger.spent.get(self._settings.attention_day(now), 0)
            >= self._settings.daily_budget
        ):
            return "daily attention budget spent"
        return None

    async def _day_before(self, now: datetime) -> str:
        """The last day as the verifier reads it: what he said, what Orion said and read, in
        every thread, newest kept when it is too long."""
        last = await self._journal.last_seq()
        said, _ = await day_since(
            self._journal,
            self._settings.thread_id,
            max(0, last - VERIFIER_JOURNAL_TAIL),
            frozenset(),
        )
        since = now - VERIFIER_LOOKBACK
        lines: list[str] = []
        recent = False
        for at, line in said:
            if at is not None:
                recent = at >= since
            if recent:
                lines.append(line)
        return "\n".join(lines)[-VERIFIER_DAY_CHARS:]

    def _failed(self, candidate: Candidate, how: str) -> tuple[str | None, str]:
        """Orion's wording failed: an alert (a task's report, a contact's message) is tried
        again once at a later tick before its raw text goes out; the rest falls back (D231)."""
        if candidate.raw_last and candidate.key not in self._failed_once:
            self._failed_once.add(candidate.key)
            return None, TRY_AGAIN
        self._failed_once.discard(candidate.key)
        return candidate.fallback, how

    async def _compose(self, candidate: Candidate, now: datetime) -> tuple[str | None, str]:
        """Let Orion write the message in its own voice, then have a different model check it
        against the facts. A failure falls back to a plain, correct text, but a draft rejected
        twice is dropped unless he asked for it. What he asked for goes out even if the model
        says SILENCE, unless Orion may judge it pointless by now (a reminder, D246). Returns
        (text, how)."""
        waiting = self._orion.owner_waiting
        if self._settings.thread_id == OWNER_THREAD and waiting is not None and waiting():
            return None, GAVE_WAY
        earlier = len(self._orion.threads.get(self._settings.thread_id).messages)
        verifying = self._verifier is not None and self._settings.verifier
        # Read before Orion writes: the draft itself must not count as known.
        conversation = await self._day_before(now) if verifying else ""
        try:
            turn = await self._orion.handle_initiative(
                self._settings.thread_id, candidate.notice, candidate.why, candidate.trust, now
            )
        except Exception:
            log.exception("initiative.compose_failed", key=candidate.key)
            return self._failed(candidate, "fallback: compose failed")
        if turn.reason is StopReason.HALTED:
            return None, GAVE_WAY
        if turn.reason is not StopReason.COMPLETED or not turn.text.strip():
            log.warning("initiative.compose_incomplete", key=candidate.key, reason=turn.reason)
            return self._failed(candidate, f"fallback: {turn.reason.value}")
        if is_silence(turn.text):
            if candidate.settle is not None:
                read = _read_while_writing(
                    self._orion.threads.get(self._settings.thread_id).messages[earlier:]
                )
                instead = await candidate.settle(None, read)
                if instead:
                    return instead, "silence not borne out"
            if candidate.explicit and not candidate.skippable:
                return candidate.fallback, "fallback: model chose silence"
            return None, "silence"
        draft = turn.text.strip()
        if self._verifier is None or not verifying:
            return draft, "unverified"
        facts = (
            f"Now: {describe_moment(now, self._settings.timezone)}\n"
            f"Why Orion writes: {candidate.why}\n{candidate.notice}"
        )
        for attempt in (1, 2):
            read = _read_while_writing(
                self._orion.threads.get(self._settings.thread_id).messages[earlier:]
            )
            known = facts + (
                f"\nWhat Orion read while writing (its memory, its searches):\n{read}"
                if read
                else ""
            )
            verification = await self._verifier.check(draft, known, conversation)
            if verification.ok:
                return draft, "verified" if attempt == 1 else "verified on a second draft"
            # The draft and its facts too: from the reasons alone, nobody could tell on 01/11
            # whether a rejection was right.
            log.warning(
                "initiative.draft_rejected",
                key=candidate.key,
                issues=verification.issues,
                draft=draft[:VERIFIER_LOG_CHARS],
                facts=known[:VERIFIER_LOG_CHARS],
            )
            if attempt == 2:
                break
            # Orion rewrites it in its own words rather than a canned text going out (D103).
            try:
                again = await self._orion.handle_initiative(
                    self._settings.thread_id,
                    "Ton message n'est pas parti : une relecture y a vu un problème ("
                    + "; ".join(verification.issues)[:500]
                    + "). Réécris-le en t'en tenant à ce que tu sais : corrige ce point et garde "
                    "tout le reste de ce qu'il devait dire, sans passer à autre chose ; ou "
                    "réponds SILENCE.",
                    candidate.why,
                    candidate.trust,
                    now,
                )
            except Exception:
                log.exception("initiative.rewrite_failed", key=candidate.key)
                break
            if again.reason is StopReason.HALTED:
                return None, GAVE_WAY
            if again.reason is not StopReason.COMPLETED or not again.text.strip():
                break
            if is_silence(again.text):
                return (
                    (_after_review(candidate.fallback), "fallback: silence after a rejection")
                    if candidate.explicit and not candidate.skippable
                    else (None, "silence")
                )
            draft = again.text.strip()
        issues = "; ".join(verification.issues)[:300]
        if not candidate.explicit:
            # Twice wrong on something he never asked for: Orion keeps quiet rather than send
            # the raw text (seen for real, « Pour la suite : … » after two rejected points).
            return None, "rejected: " + issues
        return _after_review(candidate.fallback), "fallback: " + issues

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
        provenance = Provenance(source="orion:initiative", actor="orion:initiative")
        correlation_id = correlation_id or new_id()
        decided = [
            NewEvent(
                kind=DECIDED,
                payload={
                    "key": o.key,
                    "act": o.act,
                    "outcome": "said" if kind == DELIVERED else "quiet",
                    "reason": reason or arbiter or how or "",
                },
                provenance=provenance,
                trust=min(Trust.SYSTEM, candidate.trust),
                correlation_id=correlation_id,
            )
            for o in candidate.opportunities
        ]
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=provenance,
                    trust=min(Trust.SYSTEM, candidate.trust),  # its fallback is its words
                    correlation_id=correlation_id,
                ),
                *decided,
            ]
        )
        await self._runner.catch_up()
