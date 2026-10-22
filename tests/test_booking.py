"""A booking goes from the slot he chose to his calendar and reminders, settled by code (D73)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from orion.booking.module import Bookings, BookingSettings, booking_tools
from orion.calendar.write import CalendarWriteError, NewCalendarEvent
from orion.core.journal import SqliteJournal
from orion.initiative.timers import TimerProjection, Timers
from orion.tools.base import ToolContext

PARIS = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=PARIS)


class FakeCalendar:
    def __init__(self, refuse: bool = False) -> None:
        self.added: list[NewCalendarEvent] = []
        self.refuse = refuse
        self.settings = self

    def writable(self) -> object:
        return object()

    async def add(self, event: NewCalendarEvent, grant: str | None, correlation: str | None) -> Any:
        if self.refuse:
            raise CalendarWriteError("calendar_add needs the owner's approval")
        self.added.append(event)
        return ("perso", None)


def bookings(journal: SqliteJournal, calendar: FakeCalendar) -> tuple[Bookings, Timers]:
    timers = Timers(journal, TimerProjection())
    found = Bookings(
        journal, timers, BookingSettings(), "Europe/Paris", calendar=calendar,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )  # fmt: skip
    return found, timers


async def book(tool: Any, correlation: str, **slot: Any) -> str:
    args = tool.parse(json.dumps({"action": "book", **slot}))
    return (await tool.handler(args, ToolContext("owner", correlation))).content


async def done(tool: Any, said: str, ok: bool, error: str = "") -> str:
    """Orion, having seen the site's answer in his own browser (D113)."""
    booking = said[said.index("(bk_") + 1 : said.index(")")]
    args = tool.parse(json.dumps({"action": "done", "booking": booking, "ok": ok, "error": error}))
    return (await tool.handler(args, ToolContext("owner", "c5"))).content


async def test_a_done_booking_lands_in_the_calendar_with_reminders(journal: SqliteJournal) -> None:
    calendar = FakeCalendar()
    keeper, timers = bookings(journal, calendar)
    (tool,) = booking_tools(keeper)
    said = await book(tool, "c1", what="Dentiste, Dr Martin", start="2026-10-22T18:30",
                      minutes=30, where="12 rue Oberkampf", site="doctolib.fr")  # fmt: skip
    assert "navigateur" in said and "`booking` done" in said
    assert "Rendez-vous réservé" in await done(tool, said, ok=True)

    (report,) = await keeper.reports()
    assert "Rendez-vous réservé" in report.notice and "Ajouté à son agenda" in report.notice
    (event,) = calendar.added
    assert event.title == "Dentiste, Dr Martin" and event.start.hour == 18
    await timers.sync()
    due = sorted(t.due_at.astimezone(PARIS) for t in timers.projection.pending())
    assert [f"{d:%d %H:%M}" for d in due] == ["21 19:00", "22 17:00"]
    assert await keeper.settle() == []  # settled once

    listing = await tool.handler(tool.parse('{"action": "list"}'), ToolContext("owner", "c9"))
    assert "[confirmed]" in listing.content


async def test_a_refused_calendar_or_a_failed_task_is_said(journal: SqliteJournal) -> None:
    keeper, timers = bookings(journal, FakeCalendar(refuse=True))
    (tool,) = booking_tools(keeper)
    dinner = await book(tool, "c1", what="Dîner chez Mori", start="2026-10-23T20:00", minutes=90)
    await done(tool, dinner, ok=True)
    kine = await book(tool, "c2", what="Kiné", start="2026-10-24T09:00")
    assert "échouée" in await done(tool, kine, ok=False, error="slot taken")

    reports = {r.key: r.notice for r in await keeper.reports()}
    kine, dinner = sorted(reports.values())
    assert "propose de l'ajouter" in dinner
    assert "pas abouti" in kine and "slot taken" in kine

    (confirmed,) = [b for b in keeper.state.bookings.values() if b.status == "confirmed"]
    cancel = tool.parse(json.dumps({"action": "cancel", "booking": confirmed.id}))
    await tool.handler(cancel, ToolContext("owner", "c3"))
    await timers.sync()
    assert timers.projection.pending() == []


async def test_a_past_slot_is_refused(journal: SqliteJournal) -> None:
    keeper, _ = bookings(journal, FakeCalendar())
    (tool,) = booking_tools(keeper)
    args = tool.parse(json.dumps({"action": "book", "what": "rdv", "start": "2026-10-19T10:00"}))
    assert (await tool.handler(args, ToolContext("owner", "c"))).is_error
