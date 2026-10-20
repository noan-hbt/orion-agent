"""Event versions: old events are read in today's shape, the journal is never rewritten (D80)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from orion.core import upcast
from orion.core.hlc import HybridClock
from orion.core.journal import SqliteJournal
from tests.conftest import make_event


@pytest.fixture
def registry() -> Iterator[None]:
    saved = dict(upcast.UPCASTERS), dict(upcast.RENAMED)
    yield
    upcast.UPCASTERS.clear()
    upcast.UPCASTERS.update(saved[0])
    upcast.RENAMED.clear()
    upcast.RENAMED.update(saved[1])


async def test_an_old_payload_is_read_in_todays_shape(tmp_path: Path, registry: None) -> None:
    path = tmp_path / "j.sqlite3"
    async with SqliteJournal(path, HybridClock("t")) as journal:
        await journal.append([make_event("place.seen", name="Boulangerie")])

    # Later, `name` becomes `place`, and the time it was seen gets a default.
    upcast.UPCASTERS["place.seen"] = [
        lambda p: {"place": p.pop("name"), **p},
        lambda p: {"at": None, **p},
    ]
    async with SqliteJournal(path, HybridClock("t")) as journal:
        await journal.append([make_event("place.seen", place="Gare", at="2026-10-20")])
        old, new = await journal.read()
        assert old.payload == {"place": "Boulangerie", "at": None}
        assert new.payload == {"place": "Gare", "at": "2026-10-20"}  # written at v3: untouched

    db = sqlite3.connect(path)  # what is on disk did not change
    rows = db.execute("SELECT payload, version FROM events ORDER BY seq").fetchall()
    db.close()
    assert rows == [('{"name":"Boulangerie"}', 1), ('{"at":"2026-10-20","place":"Gare"}', 3)]


async def test_a_renamed_kind_is_read_under_its_new_name(tmp_path: Path, registry: None) -> None:
    path = tmp_path / "j.sqlite3"
    async with SqliteJournal(path, HybridClock("t")) as journal:
        await journal.append([make_event("photo.looked", id="ph_1")])
    upcast.RENAMED["photo.looked"] = "attachment.looked"
    async with SqliteJournal(path, HybridClock("t")) as journal:
        (event,) = await journal.read()
        assert event.kind == "attachment.looked" and event.payload == {"id": "ph_1"}


async def test_an_event_from_a_newer_orion_is_refused(tmp_path: Path, registry: None) -> None:
    path = tmp_path / "j.sqlite3"
    upcast.UPCASTERS["x.y"] = [lambda p: p]
    async with SqliteJournal(path, HybridClock("t")) as journal:
        await journal.append([make_event("x.y")])
    upcast.UPCASTERS.pop("x.y")
    async with SqliteJournal(path, HybridClock("t")) as journal:
        with pytest.raises(ValueError, match="newer than this Orion knows"):
            await journal.read()


async def test_a_journal_from_before_versions_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(
        """CREATE TABLE events (seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE,
        hlc TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, source TEXT NOT NULL,
        actor TEXT NOT NULL, derived_from TEXT NOT NULL, trust INTEGER NOT NULL,
        causation_id TEXT, correlation_id TEXT, redacted INTEGER NOT NULL DEFAULT 0);
        INSERT INTO events (id, hlc, kind, payload, source, actor, derived_from, trust)
        VALUES ('e1', '0', 'test.happened', '{"a":1}', 's', 'a', '[]', 60);"""
    )
    db.close()
    async with SqliteJournal(path, HybridClock("t")) as journal:
        (event,) = await journal.read()
        assert event.payload == {"a": 1}
        await journal.append([make_event()])
        assert len(await journal.read()) == 2


async def test_a_redacted_event_is_not_upcast(tmp_path: Path, registry: None) -> None:
    path = tmp_path / "j.sqlite3"
    async with SqliteJournal(path, HybridClock("t")) as journal:
        (stored,) = await journal.append([make_event("place.seen", name="Maison")])
        await journal.redact([stored.id], "forget", "owner:main")
    upcast.UPCASTERS["place.seen"] = [lambda p: {"place": p.pop("name")}]
    async with SqliteJournal(path, HybridClock("t")) as journal:
        erased, _ = await journal.read()
        assert erased.redacted and erased.payload == {}
