"""Intentions: what the owner wants to become, kept in view, noted, and taken stock of."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.intentions.module import Intentions, IntentionSettings, intention_tools
from orion.memory.model import Category
from orion.memory.service import MemoryService
from orion.memory.store import MemoryStore
from orion.tools.base import Tool, ToolContext

PARIS = ZoneInfo("Europe/Paris")
MONDAY = datetime(2026, 10, 19, 9, 0, tzinfo=PARIS)
CTX = ToolContext("owner", "c", context_trust=Trust.OWNER)


@pytest.fixture
async def memory(journal: SqliteJournal) -> AsyncIterator[MemoryService]:
    store = MemoryStore(":memory:")
    await store.open()
    yield MemoryService(journal, store)
    await store.close()


async def call(tools: dict[str, Tool], name: str, **args: object) -> str:
    tool = tools[name]
    result = await tool.handler(tool.parse(json.dumps(args)), CTX)
    assert not result.is_error, result.content
    return result.content


async def test_an_intention_is_kept_in_view_noted_and_taken_stock_of(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    now = [MONDAY]
    intentions = Intentions(journal, memory, IntentionSettings(), "Europe/Paris", lambda: now[0])
    tools = {t.name: t for t in intention_tools(intentions)}
    said = await call(tools, "intention_set", what="courir", why="se sentir mieux", per_week=3)
    assert "7 jours" in said
    (intention,) = intentions.state.active.values()

    # In memory, as part of who the owner is: shown when a conversation starts.
    await memory.sync()
    (fact,) = await memory.store.facts()
    assert fact.category == Category.PROFILE and intention.id in fact.statement
    assert "courir (3 fois par semaine) (parce que : se sentir mieux)" in fact.statement

    now[0] = MONDAY + timedelta(days=1)
    noted = await call(
        tools, "intention_note", intention=intention.id, happened="5 km au parc", kept=True
    )
    assert "1 fois tenu" in noted
    now[0] = MONDAY + timedelta(days=4)
    await call(tools, "intention_note", intention=intention.id, happened="flemme", kept=False)

    assert await intentions.alerts(MONDAY + timedelta(days=6)) == []  # not yet a week
    check = MONDAY.replace(hour=18, minute=45) + timedelta(days=7)
    (alert,) = await intentions.alerts(check)
    assert "« courir »" in alert.notice and "se sentir mieux" in alert.notice
    assert "1 fois tenu, 1 écart(s), objectif 3" in alert.notice
    assert "5 km au parc" in alert.notice and "sans morale" in alert.notice
    assert await intentions.alerts(check + timedelta(days=1)) == []  # the moment passed
    (second,) = await intentions.alerts(check + timedelta(days=7))
    assert second.key != alert.key and "rien noté sur cette période" in second.notice

    listing = await call(tools, "intentions")
    assert intention.id in listing and "dernière fois le mardi 20/10" in listing
    await call(tools, "intention_drop", intention=intention.id, why="objectif atteint")
    assert await intentions.alerts(check + timedelta(days=7)) == []
    assert await memory.store.facts() == []  # no longer part of who he is


async def test_orion_disagrees_once_and_the_check_in_remembers(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    """« Je sors jusqu'à 4 h » the night before the exam he said mattered: said once (D66)."""
    now = [MONDAY]
    intentions = Intentions(journal, memory, IntentionSettings(), "Europe/Paris", lambda: now[0])
    tools = {t.name: t for t in intention_tools(intentions)}
    await call(tools, "intention_set", what="réussir son partiel de vendredi", why="son master")
    (intention,) = intentions.state.active.values()

    now[0] = MONDAY + timedelta(days=3, hours=10)  # Thursday evening
    said = await call(
        tools, "intention_challenge", intention=intention.id, plan="sortir jusqu'à 4 h jeudi"
    )
    assert "une fois" in said and "son master" in said
    again = await call(
        tools, "intention_challenge", intention=intention.id, plan="un dernier verre"
    )
    assert "déjà" in again  # never twice the same day
    assert len(intentions.state.active[intention.id].challenges) == 1

    (check_in,) = await intentions.alerts(MONDAY + timedelta(days=7, hours=10))
    assert "sortir jusqu'à 4 h jeudi" in check_in.notice  # the week's stock knows
