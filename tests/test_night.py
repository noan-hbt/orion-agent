from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.memory.night import CONFLICT, DONE, Consolidator, NightSettings
from orion.memory.semantic import EmbeddingCache, SemanticIndex
from orion.memory.service import MemoryService, Origin
from orion.memory.store import MemoryStore
from tests.fakes import ScriptedProvider, reply
from tests.test_semantic import Concepts

PARIS = ZoneInfo("Europe/Paris")
OWNER = Origin(actor="orion:core", source="terminal:owner", trust=Trust.OWNER)
MAIL = Origin(actor="orion:core", source="thread:owner", trust=Trust.THIRD_PARTY)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Places(Concepts):
    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        vectors = await super().embed(model, texts)
        return [
            [*v, float(any(w in t.lower() for w in ("habite", "lyon", "paris")))]
            for v, t in zip(vectors, texts, strict=True)
        ]


@pytest.fixture
async def memory(journal: SqliteJournal, tmp_path: Path) -> AsyncIterator[MemoryService]:
    store = MemoryStore(":memory:")
    await store.open()
    cache = EmbeddingCache(tmp_path / "e.sqlite3")
    await cache.open()
    service = MemoryService(journal, store, SemanticIndex(store, Places(), cache, "emb"))
    yield service
    await cache.close()
    await store.close()


def judge(answer: dict[str, Any]) -> ScriptedProvider:
    return ScriptedProvider(reply(json.dumps(answer)))


def night(
    journal: SqliteJournal, memory: MemoryService, provider: ScriptedProvider, clock: Clock
) -> Consolidator:
    return Consolidator(
        journal,
        memory,
        provider,
        "judge",
        NightSettings(at=time(3, 30), similarity=0.9),
        "Europe/Paris",
        ["Noan"],
        clock,
    )


async def test_the_night_tidies_memory_but_never_against_trust(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    allergy = await memory.remember("Paul est allergique aux noix.", OWNER, about=["Paul"])
    nuts = await memory.remember("Paul ne peut pas manger de noix.", OWNER, about=["Paul Martin"])
    lyon = await memory.remember("Paul habite à Lyon.", OWNER, about=["Paul"])
    paris = await memory.remember("Paul habite à Paris.", MAIL, about=["Paul"])
    provider = judge(
        {
            "pairs": [
                {"pair": 1, "relation": "duplicate", "keep": "older"},
                {"pair": 2, "relation": "update"},
            ],
            "same_people": [
                {"name": "Paul", "same_as": "Paul Martin"},
                {"name": "Léa", "same_as": "Paul Martin"},  # not a candidate: ignored
            ],
        }
    )
    clock = Clock(datetime(2026, 10, 21, 3, 0, tzinfo=PARIS))
    consolidator = night(journal, memory, provider, clock)
    await consolidator.start()
    assert await consolidator.tick() is None  # not yet 03:30

    clock.now = datetime(2026, 10, 21, 3, 31, tzinfo=PARIS)
    report = await consolidator.tick()
    assert report is not None
    prompt = provider.requests[0].messages[1]["content"]
    assert "older: Paul est allergique aux noix. [dit par lui]" in prompt
    assert "newer: Paul habite à Paris. [venu d'ailleurs]" in prompt
    assert "« Paul » / « Paul Martin »" in prompt

    current = {f.id for f in await memory.store.facts()}
    assert allergy in current and nuts not in current  # duplicate: the more precise stays
    assert {lyon, paris} <= current  # a mail cannot overrule the owner: asked instead
    assert report.conflicts == 1 and report.merged == [("Paul", "Paul Martin")]
    assert (await memory.store.aliases())["paul"] == "Paul Martin"

    [line] = await consolidator.digest(clock.now, clock.now, first_slot=True, last_slot=False)
    assert line.text.startswith("- À clarifier avec lui : « Paul habite à Lyon. » ou")
    assert line.trust is Trust.THIRD_PARTY
    assert await consolidator.tick() is None  # once a night

    await memory.invalidate(paris, "il a confirmé Lyon", OWNER)
    assert await consolidator.digest(clock.now, clock.now, True, False) == []
    kinds = [e.kind for e in await journal.read()]
    assert kinds.count(DONE) == 1 and kinds.count(CONFLICT) == 1


async def test_a_failed_judge_changes_nothing(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    await memory.remember("Paul est allergique aux noix.", OWNER, about=["Paul"])
    await memory.remember("Paul ne peut pas manger de noix.", OWNER, about=["Paul"])
    consolidator = night(
        journal,
        memory,
        ScriptedProvider(reply("pas du json")),
        Clock(datetime(2026, 10, 21, 4, 0, tzinfo=PARIS)),
    )
    await consolidator.start()
    report = await consolidator.tick()
    assert report is not None and report.ended == [] and report.pairs == 1
    assert len(await memory.store.facts()) == 2
