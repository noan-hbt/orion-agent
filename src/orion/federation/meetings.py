"""Meetings arranged between Orions (D29, first use of M7), two or more (D99).

    meeting.proposed        (we asked: the slots we offered)
    peer.message.received   (what the guardian took from another Orion, with its envelope)
    meeting.answered        (we were asked: accepted for a slot, or declined)
    meeting.agreed          (both sides signed the same slot: a commitment for the owner)
    meeting.declined        (the other side said no)
    meeting.failed          (a message could not leave)
    peer.message.handled    (anything else read, and set aside)

A meeting of several (D99) is one meeting per other Orion, tied by a group id. The one who
asks polls them all with the same free starts; each answers by code which of them are free
(`meeting.polled` there, `meeting.availability` here). The first start free for everyone is
then proposed to each as an ordinary proposal, which each owner approves. Only when all have
accepted are they confirmed (`meeting.group.agreed`); a refusal calls the others off
(`meeting.cancel`, `meeting.cancelled` there) and the owner hears why
(`meeting.group.failed`).

The negotiator is code, not a model: another Orion's message is data that is checked, and
nothing it says can steer a decision. Each owner agrees once: the one who asks approves the
proposal (and which window of their calendar it uses), the one who is asked approves the
slot found for them. The guardian checks both approvals with the owners themselves (D28).

The other Orion learns the starts offered and the one accepted, never why the others are
busy. Titles come from the other side: they are bounded, shown as content from a peer
(TRUSTED_PEER), and never followed as instructions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from pydantic import BaseModel, ConfigDict, Field

from orion.calendar.service import Calendar
from orion.calendar.write import CalendarWriteError, NewCalendarEvent
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.federation.slots import Busy, Hours, free_slots, is_free
from orion.federation.wire import (
    ACCEPT,
    AVAILABLE,
    CANCEL,
    CONFIRM,
    DECLINE,
    INTRODUCE,
    MAX_SAY,
    POLL,
    PROPOSE,
    SAY,
    Accept,
    Available,
    Cancel,
    Confirm,
    Decline,
    Envelope,
    Propose,
    Say,
    WireError,
    clean_title,
    parse_body,
)
from orion.guardian.client import GuardianClient, GuardianUnavailableError
from orion.guardian.policy import ActionProposal, Approver
from orion.initiative.engine import Alert, Report
from orion.initiative.tools import describe_moment
from orion.memory.service import MemoryService, Origin
from orion.runtime import OWNER_THREAD, is_silence
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

log = structlog.get_logger(__name__)

PROPOSED = "meeting.proposed"
PEER_MESSAGE = "peer.message.received"
ANSWERED = "meeting.answered"
AGREED = "meeting.agreed"
DECLINED = "meeting.declined"
FAILED = "meeting.failed"
HANDLED = "peer.message.handled"
POLLED = "meeting.polled"
AVAILABILITY = "meeting.availability"
PEER_ACCEPTED = "meeting.peer_accepted"
GROUP_PROPOSED = "meeting.group.proposed"
GROUP_AGREED = "meeting.group.agreed"
GROUP_FAILED = "meeting.group.failed"
CANCELLED = "meeting.cancelled"
CHATTED = "federation.chat"
"""Another Orion wrote, and what Orion answered (D102)."""
CHAT_SENT = "federation.chat.sent"
"""Orion wrote first to another Orion, with the owner's yes."""
CHAT_TOLD = timedelta(hours=12)
_OUTCOMES = (
    ANSWERED,
    AGREED,
    DECLINED,
    FAILED,
    HANDLED,
    POLLED,
    AVAILABILITY,
    PEER_ACCEPTED,
    CANCELLED,
    CHATTED,
)
_GROUP = (GROUP_PROPOSED, GROUP_AGREED, GROUP_FAILED)
_WHY = {
    "no_slot": "aucun des créneaux ne lui va",
    "unreachable": "son Orion n'a pas pu le joindre pour lui demander. Rien n'est "
    "refusé : il pourra reproposer plus tard, ou lui demander directement",
}


class MeetingAcceptInput(BaseModel):
    """What the asked owner approves: this slot, for this proposal, from this Orion."""

    model_config = ConfigDict(extra="forbid")

    peer: str
    meeting_id: str
    slot: str = Field(description="ISO start of the slot")


async def _never_called(args: Any, ctx: ToolContext) -> ToolResult:  # pragma: no cover
    return ToolResult("meeting_accept is decided by the owner, not called", is_error=True)


ACCEPT_ACTION = Tool(
    name="meeting_accept",
    description="Accept a slot proposed by another Orion (asked of the owner, never called).",
    input_model=MeetingAcceptInput,
    handler=_never_called,
    effect=Effect.EXTERNAL,
    reversibility=Reversibility.IRREVERSIBLE,
)


@dataclass(frozen=True)
class Meeting:
    id: str
    role: str
    """'asked' (our owner proposed) or 'invited' (another Orion proposed)."""
    peer: str
    label: str
    title: str
    duration_minutes: int
    slots: tuple[datetime, ...]
    status: str
    slot: datetime | None = None
    group: str | None = None
    """The meeting of several this one belongs to, on the side that proposed it."""
    others: tuple[str, ...] = ()
    """The others invited along, by name."""
    available: tuple[datetime, ...] = ()
    """Their answer to the poll: which of our starts are free for them."""

    @property
    def end(self) -> datetime | None:
        return self.slot + timedelta(minutes=self.duration_minutes) if self.slot else None


@dataclass(frozen=True)
class PeerMessage:
    seq: int
    event_id: str
    envelope: Envelope


@dataclass(frozen=True)
class Chat:
    event_id: str
    peer: str
    label: str
    them: str
    reply: str
    at: datetime


Answer = Callable[[str, str, str, str], Awaitable[str]]
"""(thread, the other Orion's name, what it wrote, what we had written to it) → Orion's reply,
from a turn of Orion itself (`Orion.handle_peer_message`)."""


@dataclass
class MeetingState:
    name: str = "meetings"
    timezone: str = "Europe/Paris"
    cursor: int = 0
    """The guardian ledger's last message journaled here."""
    meetings: dict[str, Meeting] = field(default_factory=dict[str, Meeting])
    messages: dict[str, PeerMessage] = field(default_factory=dict[str, PeerMessage])
    handled: set[str] = field(default_factory=set[str])
    reports: list[Report] = field(default_factory=list[Report])
    chats: list[Chat] = field(default_factory=list[Chat])
    opened: dict[str, str] = field(default_factory=dict[str, str])
    """Peer → what we wrote first, until it answers."""
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.cursor = 0
        self.meetings.clear()
        self.messages.clear()
        self.handled.clear()
        self.reports.clear()
        self.chats.clear()
        self.opened.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        p: dict[str, Any] = event.payload
        kind = event.kind
        if kind == PEER_MESSAGE:
            self.cursor = max(self.cursor, int(p["seq"]))
            envelope = Envelope.model_validate(p["envelope"])
            self.messages[envelope.id] = PeerMessage(int(p["seq"]), event.id, envelope)
            return
        if kind == CHAT_SENT:
            self.opened[str(p["peer"])] = str(p["text"])
            return
        if kind == PROPOSED:
            status = "polling" if p.get("poll") else "proposed"
            self.meetings[str(p["meeting_id"])] = _meeting(p, "asked", status)
            return
        if kind in _GROUP:
            self._group(event, p)
            return
        if kind not in _OUTCOMES:
            return
        if p.get("envelope_id"):
            self.handled.add(str(p["envelope_id"]))
        meeting_id = str(p.get("meeting_id", ""))
        known = self.meetings.get(meeting_id)
        if kind == CHATTED:
            self.opened.pop(str(p["peer"]), None)
            self.chats.append(
                Chat(
                    event.id,
                    str(p["peer"]),
                    str(p["label"]),
                    str(p["them"]),
                    str(p.get("reply", "")),
                    datetime.fromisoformat(str(p["at"])),
                )
            )
        elif kind == POLLED:
            self.meetings[meeting_id] = _meeting(p, "invited", "polled")
        elif kind == AVAILABILITY and known is not None:
            self.meetings[meeting_id] = replace(
                known, status="available", available=tuple(_at(s) for s in p["slots"])
            )
        elif kind == PEER_ACCEPTED and known is not None:
            self.meetings[meeting_id] = replace(known, status="peer_accepted")
        elif kind == CANCELLED and known is not None:
            self.meetings[meeting_id] = replace(known, status="cancelled", slot=None)
            when = describe_moment(known.slot, self.timezone) if known.slot else "le créneau"
            self._report(
                event,
                f"L'Orion de {known.label} annule « {known.title} » ({when}) : quelqu'un "
                "d'autre du groupe n'a pas pu. Le créneau est libre à nouveau.",
                f"l'Orion de {known.label} a annulé un rendez-vous de groupe",
                f"« {known.title} » ({when}) est annulé : quelqu'un du groupe n'a pas pu. Ton "
                "créneau est libre.",
            )
        elif kind == ANSWERED:
            invited = _meeting(p, "invited", "accepted" if p["accepted"] else "refused")
            self.meetings[meeting_id] = replace(
                invited, slot=_at(p.get("slot")) if p["accepted"] else None
            )
            if p.get("reason") == "unreachable":
                self._report(
                    event,
                    f"L'Orion de {p['label']} a proposé « {p['title']} », mais je n'ai pas pu te "
                    "joindre pour te demander : j'ai répondu que tu n'étais pas joignable, sans "
                    "rien refuser. Dis-le-lui ; il peut redemander à son Orion de reproposer.",
                    f"l'Orion de {p['label']} a proposé un rendez-vous",
                    f"{p['label']} proposait « {p['title']} », mais je n'ai pas pu te joindre : "
                    "j'ai répondu que tu n'étais pas joignable, sans rien refuser.",
                )
            if p.get("reason") == "no_slot":
                self._report(
                    event,
                    f"L'Orion de {p['label']} a proposé « {p['title']} », mais aucun des "
                    "créneaux proposés n'est libre pour lui : j'ai répondu que ça ne marchait "
                    "pas. Dis-le-lui, il peut proposer d'autres dates.",
                    f"l'Orion de {p['label']} a proposé un rendez-vous",
                    f"{p['label']} proposait « {p['title']} », mais aucun des créneaux ne "
                    "t'allait : j'ai répondu que ça ne marchait pas.",
                )
        elif kind == AGREED and known is not None:
            start = _at(p["start"])
            self.meetings[meeting_id] = replace(known, status="agreed", slot=start)
            if known.group:  # the group says it once, when everyone is in
                return
            when = describe_moment(start, self.timezone)
            also = f" (avec aussi {_names(list(known.others))})" if known.others else ""
            self._report(
                event,
                f"C'est calé avec l'Orion de {known.label}{also} : « {known.title} », {when} "
                f"({known.duration_minutes} min). Noté dans ses engagements ; s'il ne le voit pas "
                "dans son agenda, propose-lui de l'y ajouter.",
                f"l'Orion de {known.label} a confirmé le rendez-vous",
                f"C'est calé avec {known.label}{also} : « {known.title} », {when}.",
            )
        elif kind == DECLINED and known is not None:
            self.meetings[meeting_id] = replace(known, status="declined")
            if known.group:
                return
            why = _WHY.get(str(p.get("reason")), "son propriétaire a décliné")
            self._report(
                event,
                f"L'Orion de {known.label} a répondu pour « {known.title} » : {why}. "
                "Propose-lui d'essayer une autre fenêtre.",
                f"l'Orion de {known.label} a répondu",
                f"Pour « {known.title} », l'Orion de {known.label} a répondu : {why}.",
            )
        elif kind == FAILED:
            self._report(
                event,
                f"Je n'ai pas pu envoyer ma réponse à l'Orion de {p.get('label', '?')} "
                f"(« {p.get('title', '')} ») : {p.get('error', '')}.",
                "un message vers un autre Orion n'est pas parti",
            )
        elif kind == HANDLED and p.get("paired"):
            self._report(
                event,
                f"Ton Orion est maintenant relié à celui de {p['label']} : vous pouvez caler "
                "des rendez-vous ensemble. Le lien s'est fait par une invitation créée sur ton "
                "serveur (`orion federation invite`) et acceptée par l'Orion de "
                f"{p['label']} : pas par toi, Orion, qui ne peux ni créer ni accepter une "
                "invitation. Ses empreintes : `orion federation peers` sur le serveur.",
                f"l'Orion de {p['label']} vient de se présenter avec ton invitation",
                f"Je suis maintenant relié à l'Orion de {p['label']} : on peut caler des "
                "rendez-vous et se parler.",
            )

    def group(self, group: str) -> list[Meeting]:
        return sorted((m for m in self.meetings.values() if m.group == group), key=lambda m: m.id)

    def _group(self, event: Event, p: dict[str, Any]) -> None:
        members = self.group(str(p["group"]))
        if not members:
            return
        names = _names([m.label for m in members])
        title, minutes = members[0].title, members[0].duration_minutes
        if event.kind == GROUP_PROPOSED:
            slot = _at(p["slot"])
            for m in members:
                self.meetings[m.id] = replace(m, status="proposed", slots=(slot,))
        elif event.kind == GROUP_AGREED:
            when = describe_moment(_at(p["start"]), self.timezone)
            self._report(
                event,
                f"C'est calé avec les Orion de {names} : « {title} », {when} ({minutes} min). "
                "Noté dans ses engagements ; s'il ne le voit pas dans son agenda, propose-lui de "
                "l'y ajouter.",
                "tout le groupe a confirmé le rendez-vous",
                f"C'est calé avec {names} : « {title} », {when}.",
            )
        elif event.kind == GROUP_FAILED:
            for m in members:
                if m.status not in ("declined",):
                    self.meetings[m.id] = replace(m, status="failed")
            reason = str(p.get("reason"))
            if reason == "no_common":
                text = (
                    f"Pas de créneau commun pour « {title} » avec {names} : chacun a répondu, "
                    "mais aucun de ses créneaux libres ne convient à tout le monde."
                )
            elif reason == "unsent":
                text = f"« {title} » avec {names} : un message n'est pas parti ({p.get('error')})."
            else:
                why = _WHY.get(reason, "son propriétaire a décliné")
                text = (
                    f"« {title} » avec {names} ne se fait pas : pour {p.get('who')}, {why}. "
                    "J'ai prévenu les autres Orion."
                )
            self._report(
                event,
                text + " Propose-lui d'essayer une autre fenêtre.",
                "un rendez-vous de groupe n'a pas abouti",
                text,
            )

    def _report(self, event: Event, notice: str, why: str, fallback: str = "") -> None:
        """`notice` is for Orion to write from; `fallback` is what he reads if it cannot: a
        sentence for him, never an instruction for Orion."""
        self.reports.append(
            Report(
                key=f"federation:{event.id}",
                notice=notice,
                why=why,
                fallback=fallback or notice,
                trust=Trust.TRUSTED_PEER,
            )
        )

    def pending(self) -> list[PeerMessage]:
        return sorted(
            (m for i, m in self.messages.items() if i not in self.handled), key=lambda m: m.seq
        )


def _at(value: Any) -> datetime:
    return datetime.fromisoformat(str(value))


def _meeting(p: dict[str, Any], role: str, status: str) -> Meeting:
    return Meeting(
        str(p["meeting_id"]),
        role,
        str(p["peer"]),
        str(p["label"]),
        str(p["title"]),
        int(p["duration_minutes"]),
        tuple(_at(s) for s in p.get("slots", [])),
        status,
        group=str(p["group"]) if p.get("group") else None,
        others=tuple(str(o) for o in p.get("others", [])),
    )


def _names(names: list[str]) -> str:
    """« Victor et Camille », « Victor, Camille et Léa »."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" et {names[-1]}"


class MeetingDesk:
    """Proposes, answers and confirms meetings; reports outcomes to the owner."""

    def __init__(
        self,
        journal: EventStore,
        guardian: GuardianClient,
        calendar: Calendar,
        memory: MemoryService,
        approver: Approver,
        hours: Hours,
        clock: Callable[[], datetime] | None = None,
        poll_seconds: float = 3.0,
    ) -> None:
        self._journal = journal
        self.guardian = guardian
        self._calendar = calendar
        self._memory = memory
        self._approver = approver
        self.hours = hours
        self._zone = ZoneInfo(hours.timezone)
        self.clock = clock or (lambda: datetime.now(self._zone))
        self._poll = poll_seconds
        self.state = MeetingState(timezone=hours.timezone)
        self._runner = ProjectionRunner(journal, [self.state])
        self._working: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._groups = asyncio.Lock()
        self.answer: Answer | None = None
        """Orion itself, set when serving: who answers another Orion's words (D102)."""
        """Answers from a group arrive together: one at a time decides what comes next."""

    async def start(self) -> None:
        await self._runner.catch_up()

    # -- the owner asks -----------------------------------------------------------------------

    async def peers(self) -> list[dict[str, Any]]:
        return await self.guardian.peers()

    async def find(self, name: str) -> dict[str, Any] | None:
        wanted = name.strip().casefold()
        for peer in await self.peers():
            if wanted in (str(peer["instance"]).casefold(), str(peer["label"]).casefold()):
                return peer
        return None

    async def busy(self, start: datetime, end: datetime) -> list[Busy]:
        """The owner's visible events, and meetings already agreed with other Orions."""
        await self._calendar.ensure_fresh()
        await self._runner.catch_up()
        taken: list[Busy] = [
            (o.start, o.end) for _, o in self._calendar.between(start, end) if not o.all_day
        ]
        taken += [
            (m.slot, m.end)
            for m in self.state.meetings.values()
            if m.status in ("agreed", "accepted") and m.slot is not None and m.end is not None
        ]
        return taken

    async def propose(
        self,
        peer_name: str,
        title: str,
        duration: timedelta,
        start: datetime,
        end: datetime,
        grant: str | None,
        correlation_id: str | None = None,
        also: list[str] | None = None,
    ) -> tuple[str, list[datetime]]:
        peer = await self.find(peer_name)
        if peer is None:
            raise LookupError(f"no Orion paired as {peer_name!r}")
        invited = [peer]
        for name in also or []:
            other = await self.find(name)
            if other is None:
                raise LookupError(f"no Orion paired as {name!r}")
            if other["instance"] not in [i["instance"] for i in invited]:
                invited.append(other)
        slots = free_slots(
            start, end, duration, await self.busy(start, end), self.hours, self.clock()
        )
        if not slots:
            days = "any day" if self.hours.weekends else "weekdays only"
            raise LookupError(
                f"no free slot in that window: he meets between {self.hours.start:%H:%M} and "
                f"{self.hours.end:%H:%M}, {days} (federation.meeting_start/meeting_end), and "
                "outside his busy times. Tell him before trying another window"
            )
        if len(invited) > 1:
            return await self._send_polls(
                invited, title, duration, start, end, slots, grant, correlation_id
            )
        meeting_id = f"m_{new_id()}"
        body = Propose(
            title=title,
            duration_minutes=int(duration.total_seconds() // 60),
            window_start=start,
            window_end=end,
            slots=tuple(slots),
        )
        await self.guardian.federation_send(
            str(peer["instance"]), PROPOSE, meeting_id, body.model_dump(mode="json"), grant
        )
        await self._append(
            PROPOSED,
            {
                "meeting_id": meeting_id,
                "peer": peer["instance"],
                "label": peer["label"],
                "title": body.title,
                "duration_minutes": body.duration_minutes,
                "slots": [s.isoformat() for s in slots],
            },
            Trust.SYSTEM,
            correlation_id=correlation_id,
        )
        return meeting_id, slots

    async def _send_polls(
        self,
        invited: list[dict[str, Any]],
        title: str,
        duration: timedelta,
        start: datetime,
        end: datetime,
        slots: list[datetime],
        grant: str | None,
        correlation_id: str | None,
    ) -> tuple[str, list[datetime]]:
        group = f"g_{new_id()}"
        labels = [str(p["label"]) for p in invited]
        for peer in invited:
            others = tuple(name for name in labels if name != peer["label"])
            body = Propose(
                title=title,
                duration_minutes=int(duration.total_seconds() // 60),
                window_start=start,
                window_end=end,
                slots=tuple(slots),
                group=others,
            )
            meeting_id = f"m_{new_id()}"
            await self.guardian.federation_send(
                str(peer["instance"]), POLL, meeting_id, body.model_dump(mode="json"), grant
            )
            await self._append(
                PROPOSED,
                {
                    "meeting_id": meeting_id,
                    "peer": peer["instance"],
                    "label": peer["label"],
                    "title": body.title,
                    "duration_minutes": body.duration_minutes,
                    "slots": [s.isoformat() for s in slots],
                    "group": group,
                    "others": list(others),
                    "poll": True,
                },
                Trust.SYSTEM,
                correlation_id=correlation_id,
            )
        return group, slots

    # -- what other Orions send ---------------------------------------------------------------

    async def run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self._poll)

    async def poll_once(self) -> None:
        await self._runner.catch_up()
        for item in await self.guardian.federation_inbox(self.state.cursor):
            envelope = Envelope.model_validate(item["envelope"])
            if envelope.id in self.state.messages:
                continue
            await self._journal.append(
                [
                    NewEvent(
                        kind=PEER_MESSAGE,
                        payload={"seq": int(item["seq"]), "envelope": item["envelope"]},
                        provenance=Provenance(
                            source=f"peer:{envelope.sender}", actor=f"peer:{envelope.sender}"
                        ),
                        trust=Trust.TRUSTED_PEER,
                        correlation_id=envelope.context,
                    )
                ]
            )
        await self._runner.catch_up()
        for message in self.state.pending():
            if message.envelope.id not in self._working:
                self._working.add(message.envelope.id)
                self._spawn(message)

    def _spawn(self, message: PeerMessage) -> None:
        async def guarded() -> None:
            try:
                await self.handle(message)
            except Exception:
                log.exception("federation.handle_failed", envelope=message.envelope.id)
            finally:
                self._working.discard(message.envelope.id)

        task = asyncio.create_task(guarded())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def settle(self) -> None:
        """Wait for messages being handled (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks))

    async def handle(self, message: PeerMessage) -> None:
        envelope = message.envelope
        try:
            body = parse_body(envelope.type, envelope.body)
        except WireError as exc:  # the guardian checked it; a newer peer may say more
            await self._handled(message, f"unreadable: {exc}")
            return
        peer = await self.find(envelope.sender)
        if peer is None:
            await self._handled(message, "no longer a paired Orion")
            return
        label = str(peer["label"])
        if envelope.type == INTRODUCE:
            await self._handled(message, "paired", paired=True, label=label)
        elif envelope.type == POLL and isinstance(body, Propose):
            await self._polled(message, body, label)
        elif isinstance(body, Say):
            await self._chatted(message, body, label)
        elif isinstance(body, Available):
            await self._available(message, body)
        elif isinstance(body, Cancel):
            await self._cancelled(message)
        elif isinstance(body, Propose):
            await self._invited(message, body, label)
        elif isinstance(body, Accept):
            await self._accepted(message, body)
        elif isinstance(body, Decline):
            await self._declined(message, body)
        elif isinstance(body, Confirm):
            await self._confirmed(message, body)
        else:  # pragma: no cover - parse_body knows only these
            await self._handled(message, f"nothing to do with {envelope.type}")

    async def _invited(self, message: PeerMessage, body: Propose, label: str) -> None:
        envelope = message.envelope
        duration = timedelta(minutes=body.duration_minutes)
        now = self.clock()
        busy = await self.busy(min(body.slots), max(body.slots) + duration)
        free = [s for s in body.slots if is_free(s, duration, busy, self.hours, now)]
        base: dict[str, object] = {
            "meeting_id": envelope.context,
            "peer": envelope.sender,
            "label": label,
            "title": body.title,
            "duration_minutes": body.duration_minutes,
            "slots": [s.isoformat() for s in body.slots],
            "envelope_id": envelope.id,
            "others": list(body.group),
        }
        if not free:
            await self._answer(message, base, DECLINE, {"reason": "no_slot"}, accepted=False)
            return
        slot = min(free)
        approval = await self._approver.approve(
            ActionProposal(
                ACCEPT_ACTION,
                MeetingAcceptInput(
                    peer=envelope.sender, meeting_id=envelope.context, slot=slot.isoformat()
                ),
                Trust.TRUSTED_PEER,
                OWNER_THREAD,
            )
        )
        if approval:
            await self._answer(
                message,
                {**base, "slot": slot.isoformat()},
                ACCEPT,
                {"slot": slot.isoformat()},
                accepted=True,
                grant=approval.grant,
            )
        else:
            reason = "declined" if approval.reached else "unreachable"
            await self._answer(message, base, DECLINE, {"reason": reason}, accepted=False)

    async def say(
        self, peer_name: str, text: str, grant: str | None, correlation_id: str | None = None
    ) -> str:
        """Write first to another Orion; the owner said yes to these exact words."""
        peer = await self.find(peer_name)
        if peer is None:
            raise LookupError(f"no Orion paired as {peer_name!r}")
        body = Say(text=text)
        await self.guardian.federation_send(
            str(peer["instance"]), SAY, f"chat-{new_id()}", body.model_dump(), grant
        )
        await self._append(
            CHAT_SENT,
            {"peer": peer["instance"], "label": peer["label"], "text": body.text},
            Trust.SYSTEM,
            correlation_id=correlation_id,
        )
        return str(peer["label"])

    async def _chatted(self, message: PeerMessage, body: Say, label: str) -> None:
        """Another Orion's words: Orion itself answers, and the owner hears of it."""
        envelope = message.envelope
        if self.answer is None:
            await self._handled(message, "nobody to answer: Orion is not serving")
            return
        sent = self.state.opened.get(envelope.sender, "")
        reply = (await self.answer(f"peer:{envelope.sender}", label, body.text, sent)).strip()
        if is_silence(reply):
            reply = ""
        error = ""
        if reply:
            try:
                await self.guardian.federation_send(
                    envelope.sender, SAY, envelope.context, {"text": reply[:MAX_SAY]}
                )
            except GuardianUnavailableError as exc:
                log.warning("federation.chat_unsent", peer=envelope.sender, error=str(exc))
                error = str(exc)[:200]
        await self._append(
            CHATTED,
            {
                "peer": envelope.sender,
                "label": label,
                "them": body.text,
                "reply": "" if error else reply,
                "unsent": reply if error else "",
                "error": error,
                "at": self.clock().isoformat(),
                "envelope_id": envelope.id,
            },
            Trust.TRUSTED_PEER,
            message,
        )

    async def alerts(self, now: datetime) -> list[Alert]:
        """What another Orion said, and what Orion answered: he hears of it, within reason."""
        await self._runner.catch_up()
        return [
            Alert(
                key=f"federation:chat:{c.event_id}",
                notice=f"L'Orion de {c.label} t'a écrit : « {c.them[:500]} ». "
                + (
                    f"Tu lui as répondu : « {c.reply[:500]} »."
                    if c.reply
                    else "Tu ne lui as pas répondu."
                )
                + " Dis-le-lui en une ou deux phrases, si ça l'intéresse.",
                why=f"l'Orion de {c.label} t'a écrit",
                fallback=f"L'Orion de {c.label} m'a écrit : « {c.them[:200]} »",
                trust=Trust.TRUSTED_PEER,
                expires_at=c.at + CHAT_TOLD,
                importance=0.4,
            )
            for c in self.state.chats
            if c.at <= now < c.at + CHAT_TOLD
        ]

    async def _polled(self, message: PeerMessage, body: Propose, label: str) -> None:
        """Which of the starts are free: code answers, the owner is asked for the one found."""
        envelope = message.envelope
        duration = timedelta(minutes=body.duration_minutes)
        busy = await self.busy(min(body.slots), max(body.slots) + duration)
        now = self.clock()
        free = [s for s in body.slots if is_free(s, duration, busy, self.hours, now)]
        payload: dict[str, object] = {
            "meeting_id": envelope.context,
            "peer": envelope.sender,
            "label": label,
            "title": body.title,
            "duration_minutes": body.duration_minutes,
            "slots": [s.isoformat() for s in body.slots],
            "others": list(body.group),
            "free": [s.isoformat() for s in free],
            "envelope_id": envelope.id,
        }
        try:
            await self.guardian.federation_send(
                envelope.sender,
                AVAILABLE,
                envelope.context,
                Available(slots=tuple(free)).model_dump(mode="json"),
            )
        except GuardianUnavailableError as exc:
            await self._failed(message, payload, str(exc))
            return
        await self._append(POLLED, payload, Trust.TRUSTED_PEER, message)

    async def _available(self, message: PeerMessage, body: Available) -> None:
        envelope = message.envelope
        meeting = self.state.meetings.get(envelope.context)
        if (
            meeting is None
            or meeting.group is None
            or meeting.peer != envelope.sender
            or meeting.status != "polling"
        ):
            await self._handled(message, "availability for no poll of ours")
            return
        free = [s for s in body.slots if s in meeting.slots]
        await self._append(
            AVAILABILITY,
            {
                "meeting_id": meeting.id,
                "slots": [s.isoformat() for s in free],
                "envelope_id": envelope.id,
            },
            Trust.TRUSTED_PEER,
            message,
        )
        async with self._groups:
            await self._runner.catch_up()
            await self._everyone_answered(meeting.group)

    async def _everyone_answered(self, group: str) -> None:
        members = self.state.group(group)
        if any(m.status != "available" for m in members):
            return
        first = members[0]
        duration = timedelta(minutes=first.duration_minutes)
        common = set(first.slots).intersection(*(set(m.available) for m in members))
        busy = await self.busy(min(first.slots), max(first.slots) + duration)
        now = self.clock()
        free = sorted(s for s in common if is_free(s, duration, busy, self.hours, now))
        if not free:
            await self._append(GROUP_FAILED, {"group": group, "reason": "no_common"}, Trust.SYSTEM)
            return
        slot = free[0]
        sent: list[Meeting] = []
        for m in members:
            body = Propose(
                title=m.title,
                duration_minutes=m.duration_minutes,
                window_start=slot,
                window_end=slot + duration,
                slots=(slot,),
                group=m.others,
            )
            try:
                await self.guardian.federation_send(
                    m.peer, PROPOSE, m.id, body.model_dump(mode="json")
                )
            except GuardianUnavailableError as exc:
                await self._call_off(sent)
                await self._append(
                    GROUP_FAILED,
                    {"group": group, "reason": "unsent", "error": str(exc)[:200]},
                    Trust.SYSTEM,
                )
                return
            sent.append(m)
        await self._append(
            GROUP_PROPOSED,
            {"group": group, "slot": slot.isoformat(), "meetings": [m.id for m in members]},
            Trust.SYSTEM,
        )

    async def _call_off(self, members: list[Meeting]) -> None:
        for m in members:
            try:
                await self.guardian.federation_send(
                    m.peer, CANCEL, m.id, Cancel(reason="group_failed").model_dump(mode="json")
                )
            except GuardianUnavailableError:
                log.warning("federation.cancel_unsent", meeting=m.id, exc_info=True)

    async def _cancelled(self, message: PeerMessage) -> None:
        envelope = message.envelope
        meeting = self.state.meetings.get(envelope.context)
        if (
            meeting is None
            or meeting.role != "invited"
            or meeting.peer != envelope.sender
            or meeting.status not in ("accepted", "polled")
        ):
            await self._handled(message, "a cancellation for nothing we hold")
            return
        if meeting.status == "polled":  # nothing was asked of the owner yet: nothing to say
            await self._handled(message, "poll called off")
            return
        await self._append(
            CANCELLED,
            {"meeting_id": meeting.id, "envelope_id": envelope.id},
            Trust.TRUSTED_PEER,
            message,
        )

    async def _answer(
        self,
        message: PeerMessage,
        payload: dict[str, object],
        kind: str,
        body: dict[str, Any],
        *,
        accepted: bool,
        grant: str | None = None,
    ) -> None:
        envelope = message.envelope
        try:
            await self.guardian.federation_send(
                envelope.sender, kind, envelope.context, body, grant
            )
        except GuardianUnavailableError as exc:
            await self._failed(message, payload, str(exc))
            return
        reason = body.get("reason")
        await self._append(
            ANSWERED,
            {**payload, "accepted": accepted, **({"reason": reason} if reason else {})},
            Trust.TRUSTED_PEER,
            message,
        )

    async def _accepted(self, message: PeerMessage, body: Accept) -> None:
        envelope = message.envelope
        meeting = self.state.meetings.get(envelope.context)
        if (
            meeting is None
            or meeting.role != "asked"
            or meeting.peer != envelope.sender
            or meeting.status != "proposed"
            or body.slot not in meeting.slots
        ):
            await self._handled(message, "an acceptance for no open proposal of ours")
            return
        if meeting.group is not None:
            await self._append(
                PEER_ACCEPTED,
                {
                    "meeting_id": meeting.id,
                    "slot": body.slot.isoformat(),
                    "signature": envelope.signature,
                    "envelope_id": envelope.id,
                },
                Trust.TRUSTED_PEER,
                message,
            )
            async with self._groups:
                await self._runner.catch_up()
                await self._everyone_accepted(meeting.group, body.slot, message)
            return
        try:
            sent = await self.guardian.federation_send(
                envelope.sender, CONFIRM, envelope.context, {"slot": body.slot.isoformat()}
            )
        except GuardianUnavailableError as exc:
            await self._failed(message, _summary(meeting), str(exc))
            return
        await self._agree(message, meeting, body.slot, theirs=envelope.signature, ours=sent)

    async def _everyone_accepted(self, group: str, slot: datetime, message: PeerMessage) -> None:
        members = self.state.group(group)
        if any(m.status != "peer_accepted" for m in members):
            return
        for m in members:
            try:
                sent = await self.guardian.federation_send(
                    m.peer, CONFIRM, m.id, {"slot": slot.isoformat()}
                )
            except GuardianUnavailableError as exc:
                await self._failed(message, _summary(m), str(exc))
                continue
            await self._append(
                AGREED,
                {
                    "meeting_id": m.id,
                    "peer": m.peer,
                    "start": slot.isoformat(),
                    "end": (slot + timedelta(minutes=m.duration_minutes)).isoformat(),
                    "receipts": {"ours": sent.get("signature")},
                    "group": group,
                },
                Trust.TRUSTED_PEER,
                message,
            )
        first = members[0]
        end = slot + timedelta(minutes=first.duration_minutes)
        await self._append(
            GROUP_AGREED,
            {"group": group, "start": slot.isoformat(), "end": end.isoformat()},
            Trust.TRUSTED_PEER,
            message,
        )
        group_meeting = replace(first, label=_names([m.label for m in members]))
        await self._to_calendar(group_meeting, slot, end)
        await self._commit(group_meeting, slot, message, "leurs Orion")

    async def _confirmed(self, message: PeerMessage, body: Confirm) -> None:
        envelope = message.envelope
        meeting = self.state.meetings.get(envelope.context)
        if (
            meeting is None
            or meeting.role != "invited"
            or meeting.peer != envelope.sender
            or meeting.status != "accepted"
            or meeting.slot != body.slot
        ):
            await self._handled(message, "a confirmation for no slot we accepted")
            return
        await self._agree(message, meeting, body.slot, theirs=envelope.signature, ours=None)

    async def _declined(self, message: PeerMessage, body: Decline) -> None:
        envelope = message.envelope
        meeting = self.state.meetings.get(envelope.context)
        if meeting is None or meeting.role != "asked" or meeting.peer != envelope.sender:
            await self._handled(message, "a refusal for no proposal of ours")
            return
        await self._append(
            DECLINED,
            {"meeting_id": meeting.id, "reason": body.reason, "envelope_id": envelope.id},
            Trust.TRUSTED_PEER,
            message,
        )
        if meeting.group is None:
            return
        async with self._groups:
            await self._runner.catch_up()
            members = self.state.group(meeting.group)
            if any(m.status == "failed" for m in members):
                return
            await self._call_off([m for m in members if m.status == "peer_accepted"])
            await self._append(
                GROUP_FAILED,
                {"group": meeting.group, "reason": body.reason, "who": meeting.label},
                Trust.TRUSTED_PEER,
                message,
            )

    async def _agree(
        self,
        message: PeerMessage,
        meeting: Meeting,
        slot: datetime,
        theirs: str,
        ours: dict[str, Any] | None,
    ) -> None:
        end = slot + timedelta(minutes=meeting.duration_minutes)
        await self._append(
            AGREED,
            {
                "meeting_id": meeting.id,
                "peer": meeting.peer,
                "start": slot.isoformat(),
                "end": end.isoformat(),
                "receipts": {
                    "theirs": theirs,
                    **({"ours": ours.get("signature")} if ours else {}),
                },
                "envelope_id": message.envelope.id,
            },
            Trust.TRUSTED_PEER,
            message,
        )
        if meeting.others:  # invited to a meeting of several: the others are there too
            meeting = replace(meeting, label=_names([meeting.label, *meeting.others]))
        await self._to_calendar(meeting, slot, end)
        await self._commit(meeting, slot, message, "son Orion")

    async def _commit(
        self, meeting: Meeting, slot: datetime, message: PeerMessage, via: str
    ) -> None:
        await self._memory.add_commitment(
            f"{meeting.title} avec {meeting.label}, {describe_moment(slot, self.hours.timezone)}"
            f" ({meeting.duration_minutes} min), calé avec {via}",
            Origin(
                actor="orion:federation",
                source=f"peer:{meeting.peer}",
                trust=Trust.TRUSTED_PEER,
                derived_from=(message.event_id,),
                correlation_id=meeting.id,
            ),
            debtor="owner",
            creditor=meeting.label,
            due=slot.isoformat(),
        )

    async def _to_calendar(self, meeting: Meeting, slot: datetime, end: datetime) -> None:
        """Agreed by both owners already: the guardian writes it once it sees the signed
        confirmation (no second approval)."""
        if self._calendar.settings.writable() is None:
            return
        event = NewCalendarEvent(
            title=clean_title(f"{meeting.title} avec {meeting.label}"),
            start=slot,
            end=end,
            notes="Calé avec son Orion.",
            meeting=meeting.id,
        )
        try:
            await self._calendar.add(event, correlation_id=meeting.id)
        except (CalendarWriteError, GuardianUnavailableError):
            log.warning("federation.calendar_write_failed", meeting=meeting.id, exc_info=True)

    async def _failed(self, message: PeerMessage, payload: dict[str, object], error: str) -> None:
        log.warning("federation.answer_failed", envelope=message.envelope.id, error=error)
        await self._append(
            FAILED,
            {**payload, "error": error[:300], "envelope_id": message.envelope.id},
            Trust.TRUSTED_PEER,
            message,
        )

    async def _handled(
        self, message: PeerMessage, outcome: str, paired: bool = False, label: str = ""
    ) -> None:
        payload: dict[str, object] = {"envelope_id": message.envelope.id, "outcome": outcome}
        if paired:
            payload |= {"paired": True, "label": label}
        await self._append(HANDLED, payload, Trust.TRUSTED_PEER, message)

    async def _append(
        self,
        kind: str,
        payload: dict[str, object],
        trust: Trust,
        message: PeerMessage | None = None,
        correlation_id: str | None = None,
    ) -> None:
        source = f"peer:{message.envelope.sender}" if message else "orion:federation"
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=Provenance(
                        source=source,
                        actor="orion:federation",
                        derived_from=(message.event_id,) if message else (),
                    ),
                    trust=trust,
                    causation_id=message.event_id if message else None,
                    correlation_id=correlation_id
                    or (message.envelope.context if message else None),
                )
            ]
        )
        await self._runner.catch_up()

    # -- initiative ---------------------------------------------------------------------------

    async def reports(self) -> list[Report]:
        await self._runner.catch_up()
        return list(self.state.reports)


def _summary(meeting: Meeting) -> dict[str, object]:
    return {"meeting_id": meeting.id, "label": meeting.label, "title": meeting.title}
