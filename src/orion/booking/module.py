"""Booking: `[modules.booking]` in orion.toml (on with the computer).

« Trouve-moi un dentiste cette semaine après 18 h. » Orion looks for slots (Doctolib, TheFork,
SNCF Connect show them without an account), offers up to three, and on the owner's yes books
the one he picked in its own browser (`browser` send, signed in with `browser` login if it
needs his account, D113). This module keeps the chain whole:

- `booking` (book) records the slot before the booking is sent;
- once the site answered, Orion says how it went (`booking` done: the page it saw); code then
  settles the booking, not the model: done, the appointment goes to the calendar (the owner
  approved the booking, the entry follows from it; when the guardian holds the calendar it adds
  it only if `calendar_add` is autonomous, otherwise Orion offers to) and reminders are set
  (the evening before, and before it starts); failed, the booking is marked failed and Orion
  tells him;
- cancelling drops the reminders; the site is told with another `browser` send.

    booking.tracked  booking.sent  booking.confirmed  booking.failed  booking.cancelled
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import structlog
from pydantic import BaseModel, ConfigDict, Field

from orion.calendar.write import CalendarWriteError, NewCalendarEvent
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.guardian.client import GuardianUnavailableError
from orion.initiative.engine import Report
from orion.modules.base import Daemon, Module, ModuleSettings, Plate
from orion.tools.actions import NO_ARGS, Action, action_tool
from orion.tools.base import Effect, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.calendar.service import Calendar
    from orion.config import OrionConfig
    from orion.initiative.timers import Timers

log = structlog.get_logger(__name__)

TRACKED = "booking.tracked"
CONFIRMED = "booking.confirmed"
FAILED = "booking.failed"
CANCELLED = "booking.cancelled"
SENT = "booking.sent"
EVENING = time(19, 0)
DAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


class BookingSettings(ModuleSettings):
    enabled: bool = True
    remind_before_minutes: int = Field(
        default=90, ge=0, le=24 * 60, description="A reminder this long before; 0: none"
    )
    evening_before: bool = Field(default=True, description="A reminder at 19:00 the day before")
    settle_seconds: float = Field(default=20.0, ge=1.0)


@dataclass(frozen=True)
class Booking:
    id: str
    what: str
    where: str
    start: datetime
    minutes: int
    site: str
    correlation_id: str
    finished: bool = False
    ok: bool = False
    error: str = ""
    status: str = "pending"
    """pending → confirmed | failed | cancelled"""
    calendar: str = ""
    reminders: tuple[str, ...] = ()

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.minutes)

    def describe(self, zone: ZoneInfo) -> str:
        at = self.start.astimezone(zone)
        where = f", {self.where}" if self.where else ""
        return (
            f"({self.id}) {self.what}, {DAYS[at.weekday()]} {at:%d/%m %H:%M}{where} [{self.status}]"
        )


class BookingState:
    name = "booking"

    def __init__(self) -> None:
        self.bookings: dict[str, Booking] = {}
        self._seq = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.bookings.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind == TRACKED:
            booking = Booking(
                id=str(payload["booking_id"]),
                what=str(payload["what"]),
                where=str(payload.get("where", "")),
                start=datetime.fromisoformat(str(payload["start"])),
                minutes=int(payload["minutes"]),
                site=str(payload.get("site", "")),
                correlation_id=str(event.correlation_id or ""),
            )
            self.bookings[booking.id] = booking
        elif event.kind == SENT:
            found = self.bookings.get(str(payload["booking_id"]))
            if found is not None and found.status == "pending":
                self.bookings[found.id] = replace(
                    found,
                    finished=True,
                    ok=bool(payload["ok"]),
                    error=str(payload.get("error") or ""),
                )
        elif event.kind in (CONFIRMED, FAILED, CANCELLED):
            found = self.bookings.get(str(payload["booking_id"]))
            if found is None:
                return
            status = {CONFIRMED: "confirmed", FAILED: "failed", CANCELLED: "cancelled"}
            self.bookings[found.id] = replace(
                found,
                status=status[event.kind],
                calendar=str(payload.get("calendar", found.calendar)),
                reminders=tuple(str(r) for r in payload.get("reminders", found.reminders)),
            )

    def unsettled(self) -> list[Booking]:
        return [b for b in self.bookings.values() if b.finished and b.status == "pending"]


class Bookings:
    def __init__(
        self,
        journal: EventStore,
        timers: Timers,
        settings: BookingSettings,
        timezone: str,
        thread_id: str = "owner",
        calendar: Calendar | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._journal = journal
        self._timers = timers
        self.settings = settings
        self.zone = ZoneInfo(timezone)
        self._thread = thread_id
        self._calendar = calendar
        self.clock = clock or (lambda: datetime.now(self.zone))
        self.state = BookingState()
        self._runner = ProjectionRunner(journal, [self.state])
        self._lock = asyncio.Lock()

    async def sync(self) -> None:
        await self._runner.catch_up()

    async def track(
        self,
        what: str,
        where: str,
        start: datetime,
        minutes: int,
        site: str,
        origin: Provenance,
        correlation_id: str,
    ) -> Booking:
        booking_id = f"bk_{new_id()}"
        await self._append(
            TRACKED,
            {
                "booking_id": booking_id,
                "what": what,
                "where": where,
                "start": start.isoformat(),
                "minutes": minutes,
                "site": site,
            },
            origin,
            correlation_id,
        )
        return self.state.bookings[booking_id]

    async def settle(self) -> list[Booking]:
        """Settles the bookings whose sending task ended: calendar and reminders, or failure."""
        async with self._lock:
            await self.sync()
            settled: list[Booking] = []
            for booking in self.state.unsettled():
                if booking.ok:
                    await self._confirm(booking)
                else:
                    await self._append(
                        FAILED,
                        {"booking_id": booking.id, "error": booking.error},
                        _ORIGIN,
                        booking.correlation_id,
                    )
                settled.append(self.state.bookings[booking.id])
            return settled

    async def run(self) -> None:
        while True:
            await self.settle()
            await asyncio.sleep(self.settings.settle_seconds)

    async def _confirm(self, booking: Booking) -> None:
        now = self.clock()
        reminders: list[str] = []
        for due, text in self._reminder_times(booking):
            if due > now:
                reminders.append(
                    await self._timers.schedule(
                        due, text, self._thread, _ORIGIN, Trust.SYSTEM, booking.correlation_id
                    )
                )
        await self._append(
            CONFIRMED,
            {
                "booking_id": booking.id,
                "calendar": await self._to_calendar(booking),
                "reminders": reminders,
            },
            _ORIGIN,
            booking.correlation_id,
        )

    def _reminder_times(self, booking: Booking) -> list[tuple[datetime, str]]:
        start = booking.start.astimezone(self.zone)
        place = f" ({booking.where})" if booking.where else ""
        found: list[tuple[datetime, str]] = []
        if self.settings.evening_before:
            evening = datetime.combine(start.date() - timedelta(days=1), EVENING, self.zone)
            found.append((evening, f"Demain {start:%H:%M} : {booking.what}{place}"))
        if self.settings.remind_before_minutes:
            before = start - timedelta(minutes=self.settings.remind_before_minutes)
            found.append((before, f"À {start:%H:%M} : {booking.what}{place}"))
        return found

    async def _to_calendar(self, booking: Booking) -> str:
        """« added », or why not (Orion offers `calendar_add` then)."""
        if self._calendar is None or self._calendar.settings.writable() is None:
            return "no writable calendar"
        event = NewCalendarEvent(
            title=booking.what,
            start=booking.start,
            end=booking.end,
            location=booking.where,
            notes=f"Réservé par Orion sur {booking.site}" if booking.site else "",
        )
        try:
            await self._calendar.add(event, None, booking.correlation_id)
        except (CalendarWriteError, GuardianUnavailableError) as exc:
            log.info("booking.calendar_refused", booking=booking.id, error=str(exc))
            return f"not added: {exc}"
        return "added"

    async def sent(self, booking: Booking, ok: bool, error: str, origin: Provenance) -> None:
        """How the site answered, as Orion saw it: settled at once."""
        await self._append(
            SENT,
            {"booking_id": booking.id, "ok": ok, "error": error},
            origin,
            booking.correlation_id,
        )
        await self.settle()

    async def cancel(self, booking: Booking, why: str, origin: Provenance) -> None:
        for timer_id in booking.reminders:
            await self._timers.cancel(timer_id, "booking cancelled", origin)
        await self._append(
            CANCELLED, {"booking_id": booking.id, "why": why}, origin, booking.correlation_id
        )

    async def reports(self) -> list[Report]:
        await self.settle()
        return [
            _report(b, self.zone)
            for b in self.state.bookings.values()
            if b.status in ("confirmed", "failed")
        ]

    async def _append(
        self, kind: str, payload: dict[str, Any], origin: Provenance, correlation_id: str
    ) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=origin,
                    trust=Trust.OWNER if kind in (TRACKED, CANCELLED) else Trust.SYSTEM,
                    # SENT is what a page showed Orion: Trust.SYSTEM like the job result it
                    # replaces; the owner approved the sending itself.
                    correlation_id=correlation_id or new_id(),
                )
            ]
        )
        await self.sync()


_ORIGIN = Provenance(source="orion:booking", actor="orion:booking")


def _report(booking: Booking, zone: ZoneInfo) -> Report:
    at = booking.start.astimezone(zone)
    what = f"{booking.what}, {DAYS[at.weekday()]} {at:%d/%m à %H:%M}"
    if booking.status == "failed":
        return Report(
            key=f"booking:{booking.id}",
            notice=f"La réservation n'a pas abouti : {what} "
            f"({booking.error or 'voir le compte rendu'}). Rien n'est dans son agenda.",
            why="la réservation qu'il t'a demandée a échoué",
            fallback=f"Réservation échouée : {what}.",
        )
    if booking.calendar == "added":
        calendar = "Ajouté à son agenda."
    else:
        calendar = (
            f"Pas dans son agenda ({booking.calendar}) : propose de l'ajouter (`calendar_add`)."
        )
    reminders = (
        f"{len(booking.reminders)} rappel(s) programmé(s)."
        if booking.reminders
        else "Aucun rappel (trop proche)."
    )
    return Report(
        key=f"booking:{booking.id}",
        notice=f"Rendez-vous réservé : {what}. {calendar} {reminders}",
        why="la réservation qu'il t'a demandée est faite",
        fallback=f"C'est réservé : {what}.",
    )


class BookInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what: str = Field(
        min_length=2, max_length=120, description="« Dentiste, Dr Martin », « Dîner chez Mori »"
    )
    start: datetime = Field(description="The slot he chose, local time, ISO")
    minutes: int = Field(default=30, ge=5, le=24 * 60, description="How long it lasts")
    where: str = Field(default="", max_length=200, description="The address")
    site: str = Field(default="", max_length=80, description="Where it is booked: doctolib.fr…")


class SentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    booking: str = Field(description="The booking id (bk_…)")
    ok: bool = Field(description="The site confirmed it")
    error: str = Field(default="", max_length=300, description="If not: what the site said")


class CancelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    booking: str = Field(description="The booking id (bk_…)")
    why: str = Field(default="", max_length=200)


def booking_tools(bookings: Bookings) -> list[Tool]:
    def origin(ctx: ToolContext) -> Provenance:
        return Provenance(
            source=f"thread:{ctx.thread_id}",
            actor="orion:core",
            derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
        )

    async def book(args: Any, ctx: ToolContext) -> ToolResult:
        start = args.start if args.start.tzinfo else args.start.replace(tzinfo=bookings.zone)
        if start <= bookings.clock():
            return ToolResult("That slot is already past.", is_error=True)
        booking = await bookings.track(
            args.what, args.where, start, args.minutes, args.site, origin(ctx), ctx.correlation_id
        )
        return ToolResult(
            f"Réservation notée ({booking.id}). Réserve maintenant, dans ce même tour, avec ton "
            "navigateur (`browser` send, après `browser` login s'il faut son compte), puis dis "
            f"ce que le site a répondu avec `booking` done ({booking.id}) : réussie, elle ira "
            "dans son agenda avec ses rappels."
        )

    async def done(args: Any, ctx: ToolContext) -> ToolResult:
        await bookings.sync()
        found = bookings.state.bookings.get(args.booking)
        if found is None or found.status != "pending":
            return ToolResult(f"No pending booking {args.booking}.", is_error=True)
        await bookings.sent(found, args.ok, args.error, origin(ctx))
        settled = bookings.state.bookings[found.id]
        if settled.status == "failed":
            return ToolResult("Notée comme échouée. Dis-le-lui, et propose un autre créneau.")
        return ToolResult(_report(settled, bookings.zone).notice)

    async def cancel(args: Any, ctx: ToolContext) -> ToolResult:
        await bookings.sync()
        found = bookings.state.bookings.get(args.booking)
        if found is None:
            return ToolResult(f"No booking {args.booking}.", is_error=True)
        await bookings.cancel(found, args.why, origin(ctx))
        return ToolResult(
            "Annulée de mon côté (rappels retirés). Le site, lui, ne le sait pas encore : "
            "annule-la là-bas avec ton navigateur (`browser` send), et retire-la de son agenda "
            "s'il le faut."
        )

    async def listing(_args: Any, _ctx: ToolContext) -> ToolResult:
        await bookings.settle()
        now = bookings.clock()
        found = [
            b.describe(bookings.zone)
            for b in sorted(bookings.state.bookings.values(), key=lambda b: b.start)
            if b.end >= now
        ]
        return ToolResult("\n".join(found) if found else "Aucune réservation à venir.")

    return [
        action_tool(
            "booking",
            "Appointments you book for the owner (doctor, restaurant, train…).",
            [
                Action(
                    "book",
                    "Record the slot he chose, just before you book it with `browser` send (same "
                    "turn).",
                    BookInput,
                    book,
                    Effect.INTERNAL,
                ),
                Action(
                    "done",
                    "What the site answered to your booking: then it goes to his calendar with "
                    "reminders, or is marked failed.",
                    SentInput,
                    done,
                    Effect.INTERNAL,
                    accepts_untrusted_context=True,
                ),
                Action(
                    "cancel",
                    "He cancels: reminders go; tell the site with `browser` send.",
                    CancelInput,
                    cancel,
                    Effect.INTERNAL,
                ),
                Action(
                    "list",
                    "Upcoming bookings and their state.",
                    NO_ARGS,
                    listing,
                    Effect.NONE,
                    accepts_untrusted_context=True,
                ),
            ],
        )
    ]


class BookingModule(Module):
    name = "booking"
    requires = ("computer",)
    instructions = "booking.md"
    settings_model = BookingSettings
    offer = (
        "Prendre ses rendez-vous (médecin sur Doctolib, resto, train) de la recherche du "
        "créneau jusqu'à l'agenda : `[modules.computer] enabled = true`."
    )

    @property
    def enabled(self) -> bool:
        computer = self.config.modules.get("computer", {})
        return bool(computer.get("enabled")) and self.settings.enabled

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.bookings: Bookings | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        from orion.calendar.module import CalendarModule

        calendar = plate.module(CalendarModule)
        self.bookings = Bookings(
            plate.journal,
            plate.timers,
            self.settings,
            self.config.agent.timezone,
            calendar=calendar.calendar if calendar is not None else None,
        )
        await self.bookings.sync()
        return booking_tools(self.bookings)

    async def serve(self, daemon: Daemon) -> None:
        if self.bookings is None:
            return
        daemon.add_service("booking.settle", self.bookings.run)
        daemon.reports.append(self.bookings)
