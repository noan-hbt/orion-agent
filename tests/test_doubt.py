"""A memory that doubts: what changes often goes stale, and Orion checks lightly (D83)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import SqliteJournal
from orion.initiative.engine import DELIVERED
from orion.memory.doubt import Doubt, DoubtSettings, doubt_tools, shelf_life
from orion.memory.model import Category
from orion.memory.service import MemoryService, Origin
from orion.memory.store import MemoryStore
from orion.tools.base import ToolContext

PARIS = ZoneInfo("Europe/Paris")
OWNER = Origin(actor="owner:main", source="imessage:owner", trust=Trust.OWNER)
CTX = ToolContext("owner", "c")


@pytest.fixture
async def memory(journal: SqliteJournal) -> AsyncIterator[MemoryService]:
    store = MemoryStore(":memory:")
    await store.open()
    yield MemoryService(journal, store)
    await store.close()


def test_what_changes_and_what_does_not() -> None:
    assert shelf_life("Cherche un appartement à Dreux", "profile") == 120
    assert shelf_life("Prépare un marathon", "project") == 60
    assert shelf_life("Paul habite à Lyon", "person") == 180
    assert shelf_life("Est né le 3 mars", "profile") is None
    assert shelf_life("Aime le café serré", "preference") is None
    assert shelf_life("Cherche toujours le meilleur café", "preference") is None


def evening(days: int) -> datetime:
    return (datetime.now(PARIS) + timedelta(days=days)).replace(
        hour=18, minute=45, second=0, microsecond=0
    )


async def test_a_stale_fact_is_asked_once_in_passing_then_confirmed(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    flat = await memory.remember(
        "Cherche un appartement à Dreux", OWNER, about=["owner"], category=Category.PROFILE
    )
    await memory.remember("Est né le 3 mars", OWNER, about=["owner"], category=Category.PROFILE)
    doubt = Doubt(journal, DoubtSettings(), "Europe/Paris", clock=lambda: evening(200))
    assert await doubt.alerts(evening(30)) == []  # still fresh
    assert await doubt.alerts(evening(200).replace(hour=12)) == []  # not at this hour
    (ask,) = await doubt.alerts(evening(200))
    assert "Cherche un appartement à Dreux" in ask.notice and ask.importance < 0.3
    assert f"`doubt` (confirm, {flat})" in ask.notice

    await journal.append(
        [
            NewEvent(
                kind=DELIVERED,
                payload={"key": ask.key, "why": ask.why, "day": evening(200).date().isoformat()},
                provenance=Provenance(source="orion:initiative", actor="orion:initiative"),
                trust=Trust.SYSTEM,
            )
        ]
    )
    assert await doubt.alerts(evening(201)) == []  # once a week at most
    assert await doubt.alerts(evening(215)) == []  # and not the same fact for three months

    confirm, stale = doubt_tools(doubt)
    listed = await stale.handler(stale.parse("{}"), CTX)
    assert flat in listed.content and "né le 3 mars" not in listed.content
    await confirm.handler(confirm.parse(json.dumps({"fact": flat})), CTX)
    assert doubt.stale(datetime.now(PARIS) + timedelta(days=30)) == []  # fresh again


async def test_a_corrected_fact_is_not_doubted_any_more(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    old = await memory.remember("Travaille chez Decathlon", OWNER, category=Category.PROFILE)
    await memory.remember("Travaille chez Nike", OWNER, category=Category.PROFILE, supersedes=[old])
    doubt = Doubt(journal, DoubtSettings(), "Europe/Paris")
    await doubt.sync()
    assert [k.statement for k in doubt.state.facts.values()] == ["Travaille chez Nike"]
    await memory.invalidate(next(iter(doubt.state.facts)), "a démissionné", OWNER)
    await doubt.sync()
    assert doubt.state.facts == {}
