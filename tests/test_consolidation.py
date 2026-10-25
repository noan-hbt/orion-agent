"""The nightly consolidation (D78): a night the model was out of reach is tried again."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.memory.night import DONE, Consolidator, NightSettings
from orion.memory.service import MemoryService, Origin
from orion.memory.store import MemoryStore
from tests.fakes import ScriptedProvider, fail
from tests.test_adaptation import json_reply

OWNER = Origin("owner:main", "imessage:owner", Trust.OWNER)


@pytest.fixture
async def memory(journal: SqliteJournal) -> AsyncIterator[MemoryService]:
    store = MemoryStore(":memory:")
    await store.open()
    yield MemoryService(journal, store)
    await store.close()


async def test_a_night_the_model_was_out_of_reach_is_done_later(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    await memory.remember("Paul aime le vélo.", OWNER, about=["Paul"])
    await memory.remember("Paul Durand habite à Dreux.", OWNER, about=["Paul Durand"])
    now = datetime(2026, 10, 25, 3, 45, tzinfo=ZoneInfo("Europe/Paris"))  # after 03:30
    judge = ScriptedProvider(fail("down", retryable=True))
    consolidator = Consolidator(
        journal, memory, judge, "judge", NightSettings(), "Europe/Paris", [], clock=lambda: now
    )
    await consolidator.start()
    assert await consolidator.tick() is not None
    assert not [e for e in await journal.read() if e.kind == DONE]  # not done: tried again
    judge.extend(json_reply({"pairs": [], "same_people": []}))
    assert await consolidator.tick() is not None
    assert len([e for e in await journal.read() if e.kind == DONE]) == 1
    assert await consolidator.tick() is None  # once a night
