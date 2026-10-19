from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from orion.core.events import Trust, derive_trust
from orion.core.hlc import HybridClock, Timestamp
from orion.core.journal import REDACTED_KIND, SqliteJournal
from tests.conftest import make_event


async def test_append_assigns_increasing_seq_and_hlc(journal: SqliteJournal) -> None:
    stored = await journal.append([make_event(n=i) for i in range(5)])
    assert [e.seq for e in stored] == [1, 2, 3, 4, 5]
    hlcs = [Timestamp.decode(e.hlc) for e in stored]
    assert hlcs == sorted(hlcs)
    assert await journal.last_seq() == 5


async def test_read_round_trips_events(journal: SqliteJournal) -> None:
    [stored] = await journal.append([make_event(text="salut", nested={"a": [1, 2]})])
    [read] = await journal.read()
    assert read == stored
    assert read.payload == {"text": "salut", "nested": {"a": [1, 2]}}
    assert await journal.get(stored.id) == stored
    assert await journal.get("missing") is None


async def test_read_paginates_after_seq(journal: SqliteJournal) -> None:
    await journal.append([make_event(n=i) for i in range(10)])
    page = await journal.read(after_seq=3, limit=4)
    assert [e.seq for e in page] == [4, 5, 6, 7]


async def test_append_is_atomic(journal: SqliteJournal) -> None:
    first = make_event()
    await journal.append([first])
    with pytest.raises(Exception, match="UNIQUE"):
        # The duplicate id makes the second insert fail: the whole batch must roll back.
        await journal.append([make_event(n=1), first])
    assert await journal.last_seq() == 1


async def test_redaction_erases_payload_and_records_marker(journal: SqliteJournal) -> None:
    keep, forget = await journal.append(
        [make_event(source="mail:1", body="ok"), make_event(source="mail:2", body="secret")]
    )
    marker = await journal.redact(
        [forget.id], reason="owner asked to forget mail:2", actor="owner:main"
    )

    assert marker.kind == REDACTED_KIND
    assert marker.payload == {"event_ids": [forget.id], "reason": "owner asked to forget mail:2"}
    redacted = await journal.get(forget.id)
    assert redacted is not None
    assert redacted.redacted and redacted.payload == {}
    assert redacted.seq == forget.seq  # position and links survive
    assert await journal.get(keep.id) == keep


async def test_redaction_of_unknown_id_changes_nothing(journal: SqliteJournal) -> None:
    [event] = await journal.append([make_event(body="x")])
    with pytest.raises(KeyError):
        await journal.redact([event.id, "nope"], reason="r", actor="owner:main")
    assert (await journal.get(event.id)) == event
    assert await journal.last_seq() == 1


async def test_journal_persists_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "j.sqlite3"
    async with SqliteJournal(path, HybridClock("a")) as j:
        await j.append([make_event(n=1)])
    async with SqliteJournal(path, HybridClock("a")) as j:
        assert [e.payload for e in await j.read()] == [{"n": 1}]


async def test_follow_yields_backlog_then_new_events(journal: SqliteJournal) -> None:
    await journal.append([make_event(n=0)])
    seen: list[int] = []

    async def consume() -> None:
        async for event in journal.follow():
            seen.append(event.payload["n"])
            if len(seen) == 3:
                return

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    await journal.append([make_event(n=1)])
    await journal.append([make_event(n=2)])
    await asyncio.wait_for(consumer, timeout=2)
    assert seen == [0, 1, 2]


async def test_concurrent_appends_are_serialized(journal: SqliteJournal) -> None:
    await asyncio.gather(*(journal.append([make_event(n=i)]) for i in range(50)))
    events = await journal.read(limit=100)
    assert [e.seq for e in events] == list(range(1, 51))
    assert sorted(e.payload["n"] for e in events) == list(range(50))


def test_derived_trust_is_the_minimum_of_inputs() -> None:
    assert derive_trust([Trust.OWNER, Trust.WEB, Trust.TRUSTED_PEER]) == Trust.WEB
    assert derive_trust([]) == Trust.UNKNOWN
