"""Orion owns its advice: it asks how it went, and remembers the answer (D65)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest

from orion.advice.module import AdviceKeeper, AdviceSettings, advice_tools
from orion.core.journal import SqliteJournal
from orion.memory.service import MemoryService
from orion.memory.store import MemoryStore
from orion.tools.base import ToolContext
from tests.test_initiative import Clock, at


@pytest.fixture
async def memory(journal: SqliteJournal) -> AsyncIterator[MemoryService]:
    store = MemoryStore(":memory:")
    await store.open()
    yield MemoryService(journal, store)
    await store.close()


async def test_a_recommendation_is_followed_up_and_its_verdict_kept(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    clock = Clock(at(20, 12))
    keeper = AdviceKeeper(journal, memory, AdviceSettings(), "Europe/Paris", clock)
    give, verdict, record = advice_tools(keeper)
    ctx = ToolContext("owner", "c")
    said = await give.handler(
        give.parse(
            '{"what": "Le Bouchon des Filles, rue Sergent Blandan", "domain": "resto", '
            '"try_on": "2026-10-23"}'
        ),
        ctx,
    )
    assert "samedi 24/10" in said.content  # the day after the dinner
    assert await keeper.alerts(at(24, 18)) == []  # not before the evening
    (ask,) = await keeper.alerts(at(24, 19, 30))
    assert "Le Bouchon des Filles" in ask.notice and ask.importance < 0.5
    assert await keeper.alerts(at(27, 19, 30)) == []  # asked once, not forever

    (advice,) = keeper.book.items.values()
    await verdict.handler(
        verdict.parse(f'{{"advice": "{advice.id}", "verdict": "pas aimé", "why": "trop cher"}}'),
        ctx,
    )
    assert await keeper.alerts(at(24, 19, 30)) == []  # answered: nothing to ask
    facts = [f.statement for f in await memory.store.facts()]
    assert any("pas aimé Le Bouchon des Filles" in f and "trop cher" in f for f in facts)

    shown = await record.handler(record.parse('{"domain": "resto"}'), ctx)
    assert shown.content.startswith("1 conseil(s), 1 avec un avis, dont 0 aimé(s).")
    assert "pas aimé (trop cher)" in shown.content
    nothing = await record.handler(record.parse('{"domain": "livre"}'), ctx)
    assert nothing.content == "Aucun conseil noté en « livre »."


async def test_without_a_date_it_asks_a_few_days_later(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    keeper = AdviceKeeper(journal, memory, AdviceSettings(), "Europe/Paris", Clock(at(20, 12)))
    give, _, _ = advice_tools(keeper)
    await give.handler(
        give.parse('{"what": "Le Problème à trois corps", "domain": "livre"}'),
        ToolContext("owner", "c"),
    )
    (advice,) = keeper.book.items.values()
    assert advice.follow_up == (at(20, 12) + timedelta(days=3)).date()
    assert isinstance(advice.given, datetime)
