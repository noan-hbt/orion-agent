"""The journal: Orion's append-only source of truth.

Events are never updated. The only mutation is redaction, used to honour "forget this source":
the payload is erased, the event row stays (so sequence numbers and causal links remain intact),
and a `journal.redacted` event records what was erased and why.

Each event is stored with the version of its kind's payload when it was written; reads bring
it to today's shape (`orion.core.upcast`), so changing a payload never means rewriting history.

SQLite runs in WAL mode with a single writer. All blocking calls go through a dedicated worker
thread so the asyncio loop never blocks on disk.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Protocol, cast

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.hlc import HybridClock
from orion.core.sqlite import SqliteWorker
from orion.core.upcast import upcast, version_of

REDACTED_KIND = "journal.redacted"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq            INTEGER PRIMARY KEY,
    id             TEXT NOT NULL UNIQUE,
    hlc            TEXT NOT NULL,
    kind           TEXT NOT NULL,
    payload        TEXT NOT NULL,
    source         TEXT NOT NULL,
    actor          TEXT NOT NULL,
    derived_from   TEXT NOT NULL,
    trust          INTEGER NOT NULL,
    causation_id   TEXT,
    correlation_id TEXT,
    redacted       INTEGER NOT NULL DEFAULT 0,
    version        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS events_source ON events(source);
CREATE INDEX IF NOT EXISTS events_correlation ON events(correlation_id);
"""

_COLUMNS = (
    "seq, id, hlc, kind, payload, source, actor, derived_from, trust, "
    "causation_id, correlation_id, redacted, version"
)


class EventStore(Protocol):
    async def append(self, events: Sequence[NewEvent]) -> list[Event]: ...

    async def read(self, after_seq: int = 0, limit: int = 1000) -> list[Event]: ...

    async def get(self, event_id: str) -> Event | None: ...

    async def last_seq(self) -> int: ...

    async def redact(self, event_ids: Sequence[str], reason: str, actor: str) -> Event: ...


def event_from_row(row: sqlite3.Row) -> Event:
    kind, payload = row["kind"], json.loads(row["payload"])
    if not row["redacted"]:  # an erased payload has no shape left to bring up to date
        kind, payload = upcast(kind, row["version"], payload)
    return Event(
        seq=row["seq"],
        id=row["id"],
        hlc=row["hlc"],
        kind=kind,
        payload=payload,
        provenance=Provenance(
            source=row["source"],
            actor=row["actor"],
            derived_from=tuple(json.loads(row["derived_from"])),
        ),
        trust=Trust(row["trust"]),
        causation_id=row["causation_id"],
        correlation_id=row["correlation_id"],
        redacted=bool(row["redacted"]),
    )


class SqliteJournal:
    """SQLite-backed EventStore. One instance per database file; it is the single writer."""

    def __init__(self, path: Path, clock: HybridClock) -> None:
        self._clock = clock
        self._sql = SqliteWorker(path, "journal", _SCHEMA)
        self._appended = asyncio.Condition()

    async def open(self) -> None:
        await self._sql.open()
        await self._sql.run(self._migrate_sync)

    async def close(self) -> None:
        await self._sql.close()

    async def __aenter__(self) -> SqliteJournal:
        await self.open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def append(self, events: Sequence[NewEvent]) -> list[Event]:
        if not events:
            return []
        stored = await self._sql.run(self._append_sync, list(events))
        async with self._appended:
            self._appended.notify_all()
        return stored

    async def read(self, after_seq: int = 0, limit: int = 1000) -> list[Event]:
        return await self._sql.run(self._read_sync, after_seq, limit)

    async def get(self, event_id: str) -> Event | None:
        return await self._sql.run(self._get_sync, event_id)

    async def last_seq(self) -> int:
        return await self._sql.run(self._last_seq_sync)

    async def redact(self, event_ids: Sequence[str], reason: str, actor: str) -> Event:
        stored = await self._sql.run(self._redact_sync, list(event_ids), reason, actor)
        async with self._appended:
            self._appended.notify_all()
        return stored

    async def follow(self, after_seq: int = 0, batch: int = 500) -> AsyncIterator[Event]:
        """Yield every event after `after_seq`, then keep waiting for new ones. Never ends."""
        cursor = after_seq
        while True:
            async with self._appended:
                events = await self.read(cursor, batch)
                if not events:
                    await self._appended.wait()
                    continue
            for event in events:
                cursor = event.seq
                yield event

    # --- synchronous internals, always run on the journal thread ---

    def _migrate_sync(self) -> None:
        """A journal from before event versions (D80): its events are all version 1."""
        columns = {row["name"] for row in self._sql.db.execute("PRAGMA table_info(events)")}
        if "version" not in columns:
            self._sql.db.execute("ALTER TABLE events ADD COLUMN version INTEGER NOT NULL DEFAULT 1")

    def _insert(self, db: sqlite3.Connection, new: NewEvent) -> Event:
        hlc = self._clock.now().encode()
        cur = db.execute(
            "INSERT INTO events (id, hlc, kind, payload, source, actor, derived_from, "
            "trust, causation_id, correlation_id, version) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                new.id,
                hlc,
                new.kind,
                json.dumps(new.payload, separators=(",", ":"), sort_keys=True),
                new.provenance.source,
                new.provenance.actor,
                json.dumps(list(new.provenance.derived_from)),
                int(new.trust),
                new.causation_id,
                new.correlation_id,
                version_of(new.kind),
            ),
        )
        # sqlite always sets lastrowid after an INSERT into a rowid table.
        return Event(**new.model_dump(), seq=cast(int, cur.lastrowid), hlc=hlc)

    def _append_sync(self, events: list[NewEvent]) -> list[Event]:
        with self._sql.transaction() as db:
            return [self._insert(db, new) for new in events]

    def _read_sync(self, after_seq: int, limit: int) -> list[Event]:
        rows = self._sql.db.execute(
            f"SELECT {_COLUMNS} FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
            (after_seq, limit),
        )
        return [event_from_row(r) for r in rows]

    def _get_sync(self, event_id: str) -> Event | None:
        row = self._sql.db.execute(
            f"SELECT {_COLUMNS} FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        return event_from_row(row) if row else None

    def _last_seq_sync(self) -> int:
        row = self._sql.db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
        return int(row[0])

    def _redact_sync(self, event_ids: list[str], reason: str, actor: str) -> Event:
        if not event_ids:
            raise ValueError("nothing to redact")
        with self._sql.transaction() as db:
            _require_known(db, event_ids)
            db.executemany(
                "UPDATE events SET payload = '{}', redacted = 1 WHERE id = ?",
                [(i,) for i in event_ids],
            )
            marker = NewEvent(
                kind=REDACTED_KIND,
                payload={"event_ids": sorted(event_ids), "reason": reason},
                provenance=Provenance(source="journal:redaction", actor=actor),
                trust=Trust.SYSTEM,
            )
            return self._insert(db, marker)


def _require_known(db: sqlite3.Connection, event_ids: list[str]) -> None:
    placeholders = ",".join("?" * len(event_ids))
    rows = db.execute(f"SELECT id FROM events WHERE id IN ({placeholders})", event_ids)
    missing = sorted(set(event_ids) - {r[0] for r in rows})
    if missing:
        raise KeyError(f"unknown event ids: {missing}")
