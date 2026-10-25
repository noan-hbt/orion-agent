"""Memory is read after a conversation, not after every message (D86)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler
from orion.core.journal import SqliteJournal
from orion.guardian.policy import Guardian
from orion.memory.batch import EXTRACTED, MemoryBatcher
from orion.memory.extractor import MemoryExtractor
from orion.memory.service import MemoryService
from orion.memory.store import MemoryStore
from orion.runtime import Orion
from orion.tools.base import ToolRegistry
from tests.fakes import ScriptedProvider, fail, reply
from tests.test_adaptation import json_reply

NOTHING: dict[str, list[object]] = {"operations": []}


class Clock:
    def __init__(self) -> None:
        self.later = timedelta()

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.later


@pytest.fixture
async def memory(journal: SqliteJournal) -> AsyncIterator[MemoryService]:
    store = MemoryStore(":memory:")
    await store.open()
    yield MemoryService(journal, store)
    await store.close()


def orion_on(journal: SqliteJournal, provider: ScriptedProvider) -> Orion:
    return Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry([]),
        guardian=Guardian(),
        compiler=ContextCompiler("C"),
        agent=AgentConfig(),
    )


def batcher(
    journal: SqliteJournal, memory: MemoryService, reader: ScriptedProvider, clock: Clock
) -> MemoryBatcher:
    return MemoryBatcher(
        journal,
        memory,
        MemoryExtractor(reader, memory, "reader"),
        "owner",
        "Europe/Paris",
        idle=timedelta(minutes=5),
        min_messages=3,
        max_wait=timedelta(minutes=60),
        clock=clock,
    )


async def say(orion: Orion, provider: ScriptedProvider, *texts: str) -> None:
    for text in texts:
        provider.extend(reply(f"(réponse à : {text})"))
        await orion.handle_owner_message("owner", text, "imessage")


async def test_a_conversation_is_read_once_after_a_pause(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    provider, reader, clock = ScriptedProvider(), ScriptedProvider(), Clock()
    orion = orion_on(journal, provider)
    batch = batcher(journal, memory, reader, clock)
    await batch.start()
    await say(orion, provider, "Je cherche un appart", "à Dreux", "non, à Nonancourt plutôt")
    assert await batch.tick() is None  # still talking
    clock.later = timedelta(minutes=6)
    reader.extend(
        json_reply(
            {
                "operations": [
                    {
                        "op": "add_fact",
                        "statement": "Cherche un appartement à Nonancourt",
                        "about": ["owner"],
                        "category": "profile",
                        "valid_from": None,
                    }
                ]
            }
        )
    )
    report = await batch.tick()
    assert report is not None and len(report.applied) == 1
    [request] = reader.requests
    read = request.messages[-1]["content"]
    assert "Owner: Je cherche un appart" in read and "Owner: non, à Nonancourt plutôt" in read
    assert "Orion: (réponse à : à Dreux)" in read
    assert await batch.tick() is None  # read once
    assert len(reader.requests) == 1


async def test_a_lone_message_waits_for_more_but_not_forever(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    provider, reader, clock = ScriptedProvider(), ScriptedProvider(), Clock()
    orion = orion_on(journal, provider)
    batch = batcher(journal, memory, reader, clock)
    await batch.start()
    await say(orion, provider, "Angèle habite à Nonancourt")
    clock.later = timedelta(minutes=10)
    assert await batch.tick() is None and reader.requests == []
    clock.later = timedelta(minutes=61)
    reader.extend(json_reply(NOTHING))
    assert await batch.tick() is not None
    assert "Angèle habite à Nonancourt" in reader.requests[0].messages[-1]["content"]


async def test_the_past_is_taken_as_read_and_a_restart_resumes(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    provider, reader, clock = ScriptedProvider(), ScriptedProvider(), Clock()
    orion = orion_on(journal, provider)
    await say(orion, provider, "avant", "la mise à jour", "déjà lu")
    await batcher(journal, memory, reader, clock).start()  # first start: a baseline
    await say(orion, provider, "un", "deux", "trois")
    clock.later = timedelta(minutes=6)
    after_restart = batcher(journal, memory, reader, clock)
    await after_restart.start()  # not a second baseline: what was said waits
    reader.extend(json_reply(NOTHING))
    assert await after_restart.tick() is not None
    read = reader.requests[0].messages[-1]["content"]
    assert "Owner: un" in read and "avant" not in read
    passes = [e for e in await journal.read() if e.kind == EXTRACTED]
    assert [p.payload["exchanges"] for p in passes] == [0, 3]
    assert json.dumps(passes[-1].payload)  # journaled as plain data


async def test_a_reader_out_of_reach_reads_the_same_conversation_later(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    provider, reader, clock = ScriptedProvider(), ScriptedProvider(), Clock()
    orion = orion_on(journal, provider)
    batch = batcher(journal, memory, reader, clock)
    await batch.start()
    await say(orion, provider, "un", "deux", "trois")
    clock.later = timedelta(minutes=6)
    reader.extend(fail("down", retryable=True))
    report = await batch.tick()
    assert report is not None and report.unavailable
    reader.extend(json_reply(NOTHING))
    assert await batch.tick() is not None
    assert "Owner: un" in reader.requests[1].messages[-1]["content"]
    passes = [e for e in await journal.read() if e.kind == EXTRACTED]
    assert [p.payload["exchanges"] for p in passes] == [0, 3]


async def test_a_turn_that_never_ended_does_not_block_reading(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    from orion.core.events import NewEvent, Provenance, Trust
    from orion.memory.batch import RECEIVED

    provider, reader, clock = ScriptedProvider(), ScriptedProvider(), Clock()
    orion = orion_on(journal, provider)
    batch = batcher(journal, memory, reader, clock)
    await batch.start()
    crashed = NewEvent(
        kind=RECEIVED,
        payload={"thread_id": "owner", "text": "perdu"},
        provenance=Provenance(source="imessage:owner", actor="owner:main"),
        trust=Trust.OWNER,
        correlation_id="crashed-turn",
    )
    await journal.append([crashed])  # the process died before the turn finished
    await say(orion, provider, "un", "deux", "trois")
    clock.later = timedelta(minutes=6)
    assert await batch.tick() is None  # it may still be running
    clock.later = timedelta(minutes=61)
    reader.extend(json_reply(NOTHING))
    assert await batch.tick() is not None
    read = reader.requests[0].messages[-1]["content"]
    assert "Owner: un" in read and "perdu" not in read


async def test_what_is_forgotten_before_reading_is_never_read(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    provider, reader, clock = ScriptedProvider(), ScriptedProvider(), Clock()
    orion = orion_on(journal, provider)
    batch = batcher(journal, memory, reader, clock)
    await batch.start()
    await say(orion, provider, "mon code de carte est 1234", "deux", "trois")
    await say(orion, provider, "quatre")
    assert await batch.tick() is None  # read into the pending batch, not yet extracted
    await memory.forget_source("imessage:owner", reason="test", actor="owner:main")
    await say(orion, provider, "cinq")
    clock.later = timedelta(minutes=61)
    reader.extend(json_reply(NOTHING))
    await batch.tick()
    [read] = reader.requests
    assert "cinq" in str(read.messages) and "1234" not in str(read.messages)
