"""Proactivity v1: worth interrupting now? and what Orion kept to itself this week."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest

from orion.core.journal import SqliteJournal
from orion.initiative.arbiter import Moment, judge, merge
from orion.initiative.engine import DELIVERED, SUPPRESSED, Alert, InitiativeEngine
from orion.memory.service import MemoryService
from orion.memory.store import MemoryStore
from tests.fakes import reply
from tests.test_initiative import SETTINGS, Clock, World, at, kinds

NOON = at(20, 12)


@pytest.fixture
async def world(journal: SqliteJournal) -> AsyncIterator[World]:
    store = MemoryStore(":memory:")
    await store.open()
    world = World(journal, MemoryService(journal, store), Clock(NOON))
    await world.orion.start()
    yield world
    await store.close()


def test_urgency_against_the_cost_of_interrupting() -> None:
    free, busy = Moment(), Moment(busy=True)
    assert judge(0.5, NOON, free, 0, 6).now
    assert not judge(0.5, NOON, busy, 0, 6).now  # in a meeting: it can wait
    assert judge(0.9, NOON, busy, 0, 6).now  # unless it really matters
    assert not judge(0.2, NOON, free, 0, 6).now  # a small thing waits for a break…
    just_freed = Moment(free_since=NOON - timedelta(minutes=5))
    assert judge(0.2, NOON, just_freed, 0, 6).now  # …which is right after an event
    assert judge(0.2, NOON, free, 0, 6, waiting_since=NOON - timedelta(hours=2)).now
    soon = NOON + timedelta(minutes=5)
    assert judge(0.5, NOON, busy, 0, 6, expires_at=soon).now  # its deadline is near
    assert not judge(0.45, NOON, free, 5, 6).now  # the last of the budget is for more
    assert merge([Moment(), Moment(busy=True)]).busy
    assert judge(0.5, NOON, free, 0, 6).reason.startswith("maintenant")


class Source:
    def __init__(self, importance: float) -> None:
        self.importance = importance

    async def alerts(self, now: datetime) -> list[Alert]:
        return [
            Alert(
                key="x:1",
                notice="Un détail sans urgence.",
                why="pour info",
                fallback="Un détail sans urgence.",
                importance=self.importance,
                expires_at=at(20, 20),
            )
        ]


class Agenda:
    def __init__(self) -> None:
        self.moment = Moment(busy=True)

    async def presence(self, now: datetime) -> Moment:
        return self.moment


def engine(w: World, **kwargs: object) -> InitiativeEngine:
    return InitiativeEngine(
        w.orion,
        w.journal,
        w.timers,
        w.inbox,
        SETTINGS,
        memory=w.memory.store,
        clock=w.clock,
        **kwargs,  # type: ignore[arg-type]
    )


async def test_a_message_waits_for_the_meeting_to_end(world: World) -> None:
    agenda = Agenda()
    initiative = engine(world, alerts=[Source(0.5)], presence=[agenda])
    world.clock.now = NOON
    assert await initiative.tick() == []
    held = [p for p in await kinds(world.journal, SUPPRESSED) if p["key"] == "x:1"]
    assert held == []  # not dropped: waiting
    agenda.moment = Moment(free_since=NOON + timedelta(minutes=50))
    world.clock.now = NOON + timedelta(minutes=55)
    world.provider.extend(reply("Petit détail sans urgence, pour info."))
    assert await initiative.tick() == ["x:1"]
    [delivered] = await kinds(world.journal, DELIVERED)
    assert str(delivered["arbiter"]).startswith("maintenant")


async def test_what_was_held_back_comes_back_on_sunday(world: World) -> None:
    initiative = engine(world, alerts=[Source(0.2)])
    world.clock.now = at(20, 19, 0)
    await initiative.tick()  # the small thing waits…
    world.clock.now = at(20, 21, 0)
    await initiative.tick()  # …past its window: dropped, and remembered
    [dropped] = [p for p in await kinds(world.journal, SUPPRESSED) if p["key"] == "x:1"]
    assert dropped["reason"] == "missed its window"
    world.clock.now = at(25, 18, 30)  # Sunday evening
    world.provider.extend(reply("Cette semaine, j'ai gardé pour moi un détail sans urgence."))
    assert any(k.startswith("almost:") for k in await initiative.tick())
    world.clock.now = at(25, 19, 30)
    assert not any(k.startswith("almost:") for k in await initiative.tick())  # once a week


async def test_the_calendar_says_when_the_owner_is_busy(world: World) -> None:
    from orion.calendar.service import Calendar, CalendarAccount, CalendarSettings
    from orion.calendar.write import NewCalendarEvent, to_ics

    meeting = NewCalendarEvent(title="Réunion", start=NOON, end=NOON + timedelta(hours=1))

    async def feed(_account: CalendarAccount) -> str:
        return to_ics(meeting, "u1", NOON)

    settings = CalendarSettings.model_validate(
        {"enabled": True, "accounts": [{"name": "perso", "url": {"env": "X"}}]}
    )
    calendar = Calendar(world.journal, settings, "Europe/Paris", feed, lambda: NOON)
    await calendar.sync_once()
    assert (await calendar.presence(NOON + timedelta(minutes=30))).busy
    after = await calendar.presence(NOON + timedelta(minutes=70))
    assert not after.busy and after.free_since == NOON + timedelta(hours=1)
