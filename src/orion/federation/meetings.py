"""Meetings arranged between two Orions (D29, first use of M7).

    meeting.proposed        (we asked: the slots we offered)
    peer.message.received   (what the guardian took from another Orion, with its envelope)
    meeting.answered        (we were asked: accepted for a slot, or declined)
    meeting.agreed          (both sides signed the same slot: a commitment for the owner)
    meeting.declined        (the other side said no)
    meeting.failed          (a message could not leave)
    peer.message.handled    (anything else read, and set aside)

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
from collections.abc import Callable
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
    CONFIRM,
    DECLINE,
    INTRODUCE,
    PROPOSE,
    Accept,
    Confirm,
    Decline,
    Envelope,
    Propose,
    WireError,
    clean_title,
    parse_body,
)
from orion.guardian.client import GuardianClient, GuardianUnavailableError
from orion.guardian.policy import ActionProposal, Approver
from orion.initiative.engine import Report
from orion.initiative.tools import describe_moment
from orion.memory.service import MemoryService, Origin
from orion.runtime import OWNER_THREAD
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

log = structlog.get_logger(__name__)

PROPOSED = "meeting.proposed"
PEER_MESSAGE = "peer.message.received"
ANSWERED = "meeting.answered"
AGREED = "meeting.agreed"
DECLINED = "meeting.declined"
FAILED = "meeting.failed"
HANDLED = "peer.message.handled"
_OUTCOMES = (ANSWERED, AGREED, DECLINED, FAILED, HANDLED)


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

    @property
    def end(self) -> datetime | None:
        return self.slot + timedelta(minutes=self.duration_minutes) if self.slot else None


@dataclass(frozen=True)
class PeerMessage:
    seq: int
    event_id: str
    envelope: Envelope


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
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.cursor = 0
        self.meetings.clear()
        self.messages.clear()
        self.handled.clear()
        self.reports.clear()
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
        if kind == PROPOSED:
            self.meetings[str(p["meeting_id"])] = _meeting(p, "asked", "proposed")
            return
        if kind not in _OUTCOMES:
            return
        if p.get("envelope_id"):
            self.handled.add(str(p["envelope_id"]))
        meeting_id = str(p.get("meeting_id", ""))
        known = self.meetings.get(meeting_id)
        if kind == ANSWERED:
            invited = _meeting(p, "invited", "accepted" if p["accepted"] else "refused")
            self.meetings[meeting_id] = replace(
                invited, slot=_at(p.get("slot")) if p["accepted"] else None
            )
            if p.get("reason") == "no_slot":
                self._report(
                    event,
                    f"L'Orion de {p['label']} a proposé « {p['title']} », mais aucun des "
                    "créneaux proposés n'est libre pour lui : j'ai répondu que ça ne marchait "
                    "pas. Dis-le-lui, il peut proposer d'autres dates.",
                    f"l'Orion de {p['label']} a proposé un rendez-vous",
                )
        elif kind == AGREED and known is not None:
            start = _at(p["start"])
            self.meetings[meeting_id] = replace(known, status="agreed", slot=start)
            when = describe_moment(start, self.timezone)
            self._report(
                event,
                f"C'est calé avec l'Orion de {known.label} : « {known.title} », {when} "
                f"({known.duration_minutes} min). Noté dans ses engagements ; son agenda est en "
                "lecture seule, rappelle-lui de l'y ajouter.",
                f"l'Orion de {known.label} a confirmé le rendez-vous",
            )
        elif kind == DECLINED and known is not None:
            self.meetings[meeting_id] = replace(known, status="declined")
            why = (
                "aucun des créneaux ne lui va"
                if p.get("reason") == "no_slot"
                else "son propriétaire a décliné"
            )
            self._report(
                event,
                f"L'Orion de {known.label} a répondu pour « {known.title} » : {why}. "
                "Propose-lui d'essayer une autre fenêtre.",
                f"l'Orion de {known.label} a répondu",
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
                "des rendez-vous ensemble.",
                f"l'Orion de {p['label']} vient de se présenter avec ton invitation",
            )

    def _report(self, event: Event, notice: str, why: str) -> None:
        self.reports.append(
            Report(
                key=f"federation:{event.id}",
                notice=notice,
                why=why,
                fallback=notice,
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
    )


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
    ) -> tuple[str, list[datetime]]:
        peer = await self.find(peer_name)
        if peer is None:
            raise LookupError(f"no Orion paired as {peer_name!r}")
        slots = free_slots(
            start, end, duration, await self.busy(start, end), self.hours, self.clock()
        )
        if not slots:
            raise LookupError("no free slot in that window")
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
            await self._answer(message, base, DECLINE, {"reason": "declined"}, accepted=False)

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
        try:
            sent = await self.guardian.federation_send(
                envelope.sender, CONFIRM, envelope.context, {"slot": body.slot.isoformat()}
            )
        except GuardianUnavailableError as exc:
            await self._failed(message, _summary(meeting), str(exc))
            return
        await self._agree(message, meeting, body.slot, theirs=envelope.signature, ours=sent)

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
        await self._to_calendar(meeting, slot, end)
        await self._memory.add_commitment(
            f"{meeting.title} avec {meeting.label}, {describe_moment(slot, self.hours.timezone)}"
            f" ({meeting.duration_minutes} min), calé avec son Orion",
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
