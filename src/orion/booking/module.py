"""Booking: `[modules.booking]` in orion.toml (on with the computer).

« Trouve-moi un dentiste cette semaine après 18 h. » Orion looks for slots (Doctolib, TheFork,
SNCF Connect show them without an account), offers up to three, and on the owner's yes books
the one he picked with `computer_task_send`, on his account with a lent session. This module
keeps the chain whole:

- `booking` (book) records the slot before the booking is sent; the sending task Orion starts
  in the same turn is linked to it (same correlation id);
- when that task ends, code settles the booking, not the model: done, the appointment goes to
  the calendar (the owner approved the booking, the entry follows from it; when the guardian
  holds the calendar it adds it only if `calendar_add` is autonomous, otherwise Orion offers to)
  and reminders are set (the evening before, and before it starts); failed, the booking is
  marked failed and Orion tells him;
- cancelling drops the reminders; the site is told with another sending task.

    booking.tracked  booking.confirmed  booking.failed  booking.cancelled
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
from orion.delegation.jobs import JOB_FINISHED, JOB_STARTED
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
SENDING_AGENT = "browser-submit"
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
    job_id: str | None = None
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
        self._jobs: dict[str, str] = {}
        """job id → booking id"""
        self._unlinked: dict[str, str] = {}
        """correlation id → a sending job started before its booking was recorded"""
        self._ended: dict[str, tuple[bool, str]] = {}
        """job id → (ok, error) of a job that ended before its booking was recorded"""
        self._seq = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.bookings.clear()
        self._jobs.clear()
        self._unlinked.clear()
        self._ended.clear()
        self._seq = 0

    def _by_correlation(self, correlation_id: str) -> Booking | None:
        waiting = [
            b
            for b in self.bookings.values()
            if b.correlation_id == correlation_id and b.job_id is None and b.status == "pending"
        ]
        return waiting[-1] if waiting else None

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
            job_id = self._unlinked.pop(booking.correlation_id, None)
            if job_id is not None:
                booking = replace(booking, job_id=job_id)
                self._jobs[job_id] = booking.id
                if job_id in self._ended:
                    ok, error = self._ended.pop(job_id)
                    booking = replace(booking, finished=True, ok=ok, error=error)
            self.bookings[booking.id] = booking
        elif event.kind == JOB_STARTED and payload.get("agent") == SENDING_AGENT:
            job_id = str(payload["job_id"])
            booking = self._by_correlation(str(event.correlation_id or ""))
            if booking is None:
                self._unlinked[str(event.correlation_id or "")] = job_id
                return
            self.bookings[booking.id] = replace(booking, job_id=job_id)
            self._jobs[job_id] = booking.id
        elif event.kind == JOB_FINISHED:
            job_id = str(payload["job_id"])
            found = self.bookings.get(self._jobs.get(job_id, ""))
            if found is None and job_id in self._unlinked.values():
                self._ended[job_id] = (bool(payload["ok"]), str(payload.get("error") or ""))
            if found is not None:
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
                "job_id": booking.job_id,
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
            f"Réservation notée ({booking.id}). Lance maintenant, dans ce même tour, "
            "`computer_task_send` qui la fait (sur `sites`, avec sa `session` s'il en faut une) : "
            "quand la tâche aura réussi, le rendez-vous ira dans son agenda avec ses rappels."
        )

    async def cancel(args: Any, ctx: ToolContext) -> ToolResult:
        await bookings.sync()
        found = bookings.state.bookings.get(args.booking)
        if found is None:
            return ToolResult(f"No booking {args.booking}.", is_error=True)
        await bookings.cancel(found, args.why, origin(ctx))
        return ToolResult(
            "Annulée de mon côté (rappels retirés). Le site, lui, ne le sait pas encore : "
            "annule-la là-bas avec `computer_task_send`, et retire-la de son agenda s'il le faut."
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
                    "Record the slot he chose, just before the `computer_task_send` that books "
                    "it (same turn): when that task succeeds, the appointment goes to his "
                    "calendar with reminders.",
                    BookInput,
                    book,
                    Effect.INTERNAL,
                ),
                Action(
                    "cancel",
                    "He cancels: reminders go; tell the site with computer_task_send.",
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
