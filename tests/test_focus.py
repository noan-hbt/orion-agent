"""« Je bosse jusqu'à 18 h »: what can wait waits, then comes as one message."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest

from orion.core.events import Provenance
from orion.core.journal import SqliteJournal
from orion.focus.module import FocusKeeper, focus_tools, parse_until
from orion.initiative.arbiter import Moment, judge
from orion.initiative.engine import DELIVERED, Alert
from orion.memory.service import MemoryService
from orion.memory.store import MemoryStore
from orion.tools.base import ToolContext
from tests.fakes import reply
from tests.test_arbiter import engine
from tests.test_initiative import PARIS, Clock, World, at, kinds

NOON = at(20, 12)
OWNER = Provenance(source="thread:owner", actor="orion:core")


@pytest.fixture
async def world(journal: SqliteJournal) -> AsyncIterator[World]:
    store = MemoryStore(":memory:")
    await store.open()
    world = World(journal, MemoryService(journal, store), Clock(NOON))
    await world.orion.start()
    yield world
    await store.close()


class Things:
    """Two things Orion would say unasked, neither urgent."""

    async def alerts(self, now: datetime) -> list[Alert]:
        return [
            Alert(
                "a:1",
                "Paul a répondu pour samedi.",
                "Paul a répondu",
                "Paul a répondu.",
                importance=0.6,
            ),
            Alert(
                "b:1", "Le colis est arrivé.", "le colis", "Le colis est arrivé.", importance=0.4
            ),
        ]


def test_while_focused_only_what_cannot_wait_goes() -> None:
    focused = Moment(focus_until=NOON + timedelta(hours=6))
    assert not judge(0.8, NOON, focused, 0, 6).now  # important, but it can wait
    long_ago = NOON - timedelta(hours=5)
    assert not judge(0.5, NOON, focused, 0, 6, waiting_since=long_ago).now  # waiting: no bonus
    soon = NOON + timedelta(minutes=5)
    assert judge(0.8, NOON, focused, 0, 6, expires_at=soon).now  # a meeting in five minutes
    assert judge(0.5, NOON, Moment(focus_until=NOON), 0, 6).now  # the focus is over


def test_until_reads_like_the_owner_says_it() -> None:
    assert parse_until("18:00", NOON) == at(20, 18)
    assert parse_until("18h", NOON) == at(20, 18)
    assert parse_until("9h30", NOON) == at(21, 9, 30)  # already past: tomorrow
    assert parse_until("2026-10-21T10:00", NOON) == at(21, 10)


async def test_what_piled_up_comes_as_one_message(world: World) -> None:
    keeper = FocusKeeper(world.journal, "Europe/Paris", world.clock)
    (start, _stop) = focus_tools(keeper)
    result = await start.handler(
        start.parse('{"until": "18:00", "why": "son mémoire"}'), ToolContext("owner", "c")
    )
    assert "18:00" in result.content
    initiative = engine(world, alerts=[Things()], presence=[keeper])

    world.clock.now = at(20, 15)
    assert await initiative.tick() == []  # both wait
    world.clock.now = at(20, 18, 1)
    world.provider.extend(reply("Pendant que tu bossais : Paul a répondu, et ton colis est là."))
    assert sorted(await initiative.tick()) == ["a:1", "b:1"]

    assert world.inbox.messages == [
        "Pendant que tu bossais : Paul a répondu, et ton colis est là."
    ]  # one message, not two
    notice = world.provider.requests[-1].messages[-1]["content"]
    assert notice.index("Paul a répondu") < notice.index("Le colis")  # most important first
    delivered = await kinds(world.journal, DELIVERED)
    assert len({str(d["key"]) for d in delivered}) == 2
    assert initiative.ledger.spent[NOON.date()] == 1  # one interruption, counted once


async def test_ending_early_and_what_contacts_are_told(world: World) -> None:
    keeper = FocusKeeper(world.journal, "Europe/Paris", world.clock)
    await keeper.start(at(20, 18), "son mémoire", OWNER)
    told = await keeper.for_contacts(at(20, 14))
    assert told == "Il est concentré jusqu'à 18:00 et ne lira ses messages qu'ensuite."
    assert "mémoire" not in told  # when, never what
    world.clock.now = at(20, 15)
    await keeper.stop(OWNER)
    assert await keeper.for_contacts(at(20, 15, 1)) is None
    moment = await keeper.presence(at(20, 15, 5))
    assert moment.free_since == at(20, 15) and moment.focus_until is None  # a break: it all goes
    assert PARIS is not None
