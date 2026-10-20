"""The library's index: a projection of the journal, rebuilt from it at will.

Two kinds of entries, both searched by words (FTS5) and, when embeddings are set up, by meaning:

- documents (notes, texts, pages) added with `library.added`, cut into passages;
- the conversation with the owner: what they said (`message.received`) and what Orion answered
  (`conversation.message` from Orion in the owner's thread), each with its date, so « qu'est-ce
  que je disais de ça en août ? » has an answer.

    library.added  library.removed
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orion.context.threads import MESSAGE_KIND
from orion.core.events import Event, Trust
from orion.core.hlc import Timestamp
from orion.core.sqlite import SqliteWorker
from orion.memory.store import fts_query

ADDED = "library.added"
REMOVED = "library.removed"
OWNER_MESSAGE = "message.received"
OWNER_THREAD = "owner"
PASSAGE_CHARS = 1200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    doc_id   TEXT PRIMARY KEY,
    title    TEXT NOT NULL,
    source   TEXT NOT NULL,
    added_at TEXT NOT NULL,
    trust    INTEGER NOT NULL,
    chars    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS passages (
    rowid  INTEGER PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES docs(doc_id) ON DELETE CASCADE,
    n      INTEGER NOT NULL,
    text   TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(text, content='passages',
    content_rowid='rowid', tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS talk (
    rowid INTEGER PRIMARY KEY,
    seq   INTEGER NOT NULL,
    at    TEXT NOT NULL,
    who   TEXT NOT NULL,
    text  TEXT NOT NULL,
    trust INTEGER NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS talk_fts USING fts5(text, content='talk',
    content_rowid='rowid', tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""


def passages(text: str, size: int = PASSAGE_CHARS) -> list[str]:
    """Paragraphs gathered up to `size` characters (a longer paragraph is cut)."""
    out: list[str] = []
    current = ""
    for paragraph in (p.strip() for p in text.replace("\r\n", "\n").split("\n\n")):
        if not paragraph:
            continue
        while len(paragraph) > size:
            if current:
                out.append(current)
                current = ""
            out.append(paragraph[:size])
            paragraph = paragraph[size:]
        if current and len(current) + len(paragraph) + 2 > size:
            out.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        out.append(current)
    return out


@dataclass(frozen=True)
class Passage:
    doc_id: str
    title: str
    source: str
    added_at: str
    trust: Trust
    text: str


@dataclass(frozen=True)
class Said:
    at: datetime
    who: str
    """owner or orion"""
    text: str
    trust: Trust


class LibraryStore:
    """Projection of library events and of the owner's conversation. `:memory:` in tests."""

    name = "library"

    def __init__(self, path: Path | str) -> None:
        self._sql = SqliteWorker(path, "library", _SCHEMA)

    async def open(self) -> None:
        await self._sql.open()

    async def close(self) -> None:
        await self._sql.close()

    # -- projection ---------------------------------------------------------------------------

    async def checkpoint(self) -> int:
        return await self._sql.run(self._checkpoint_sync)

    async def reset(self) -> None:
        await self._sql.run(self._reset_sync)

    async def apply(self, event: Event) -> None:
        await self._sql.run(self._apply_sync, event)

    def _checkpoint_sync(self) -> int:
        row = self._sql.db.execute("SELECT value FROM meta WHERE key = 'seq'").fetchone()
        return int(row[0]) if row else 0

    def _reset_sync(self) -> None:
        with self._sql.transaction() as db:
            for table in ("passages", "docs", "talk", "meta"):
                db.execute(f"DELETE FROM {table}")
            db.execute("INSERT INTO passages_fts(passages_fts) VALUES ('rebuild')")
            db.execute("INSERT INTO talk_fts(talk_fts) VALUES ('rebuild')")

    def _apply_sync(self, event: Event) -> None:
        with self._sql.transaction() as db:
            if not event.redacted:
                self._apply_one(db, event)
            db.execute(
                "INSERT INTO meta (key, value) VALUES ('seq', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (event.seq,),
            )

    def _apply_one(self, db: sqlite3.Connection, event: Event) -> None:
        payload: dict[str, Any] = event.payload
        if event.kind == ADDED:
            doc_id = str(payload["doc_id"])
            self._remove(db, doc_id)
            text = str(payload["text"])
            db.execute(
                "INSERT INTO docs (doc_id, title, source, added_at, trust, chars) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    doc_id,
                    str(payload["title"]),
                    event.provenance.source,
                    _at(event).isoformat(),
                    int(event.trust),
                    len(text),
                ),
            )
            for n, passage in enumerate(passages(text)):
                cursor = db.execute(
                    "INSERT INTO passages (doc_id, n, text) VALUES (?, ?, ?)", (doc_id, n, passage)
                )
                db.execute(
                    "INSERT INTO passages_fts (rowid, text) VALUES (?, ?)",
                    (cursor.lastrowid, passage),
                )
        elif event.kind == REMOVED:
            self._remove(db, str(payload["doc_id"]))
        elif (
            event.kind == OWNER_MESSAGE
            and event.trust >= Trust.OWNER
            and payload.get("thread_id") == OWNER_THREAD
            and str(payload.get("text", "")).strip()
        ):
            self._said(db, event, "owner", str(payload["text"]))
        elif event.kind == MESSAGE_KIND and payload.get("thread_id") == OWNER_THREAD:
            message: dict[str, Any] = payload.get("message", {})
            content = str(message.get("content") or "").strip()
            if message.get("role") == "assistant" and content:
                self._said(db, event, "orion", content)

    def _said(self, db: sqlite3.Connection, event: Event, who: str, text: str) -> None:
        cursor = db.execute(
            "INSERT INTO talk (seq, at, who, text, trust) VALUES (?, ?, ?, ?, ?)",
            (event.seq, _at(event).isoformat(), who, text, int(event.trust)),
        )
        db.execute("INSERT INTO talk_fts (rowid, text) VALUES (?, ?)", (cursor.lastrowid, text))

    def _remove(self, db: sqlite3.Connection, doc_id: str) -> None:
        rows = db.execute("SELECT rowid, text FROM passages WHERE doc_id = ?", (doc_id,)).fetchall()
        for row in rows:
            db.execute(
                "INSERT INTO passages_fts (passages_fts, rowid, text) VALUES ('delete', ?, ?)",
                (row["rowid"], row["text"]),
            )
        db.execute("DELETE FROM passages WHERE doc_id = ?", (doc_id,))
        db.execute("DELETE FROM docs WHERE doc_id = ?", (doc_id,))

    # -- queries ------------------------------------------------------------------------------

    async def search(self, text: str, limit: int) -> list[Passage]:
        query = fts_query(text)
        if query is None:
            return []
        return await self._sql.run(self._search_sync, query, limit)

    def _search_sync(self, query: str, limit: int) -> list[Passage]:
        rows = self._sql.db.execute(
            "SELECT p.doc_id, d.title, d.source, d.added_at, d.trust, p.text FROM passages_fts f "
            "JOIN passages p ON p.rowid = f.rowid JOIN docs d ON d.doc_id = p.doc_id "
            "WHERE passages_fts MATCH ? ORDER BY bm25(passages_fts) LIMIT ?",
            (query, limit),
        ).fetchall()
        return [_passage(r) for r in rows]

    async def all_passages(self) -> list[Passage]:
        return await self._sql.run(self._all_sync)

    def _all_sync(self) -> list[Passage]:
        rows = self._sql.db.execute(
            "SELECT p.doc_id, d.title, d.source, d.added_at, d.trust, p.text FROM passages p "
            "JOIN docs d ON d.doc_id = p.doc_id ORDER BY p.rowid"
        ).fetchall()
        return [_passage(r) for r in rows]

    async def docs(self) -> list[tuple[str, str, str, int]]:
        """(id, title, added at, characters), newest first."""
        return await self._sql.run(self._docs_sync)

    def _docs_sync(self) -> list[tuple[str, str, str, int]]:
        rows = self._sql.db.execute(
            "SELECT doc_id, title, added_at, chars FROM docs ORDER BY added_at DESC"
        ).fetchall()
        return [(r[0], r[1], r[2], int(r[3])) for r in rows]

    async def recall(
        self, text: str, since: datetime | None, until: datetime | None, limit: int
    ) -> list[Said]:
        """What was said, matching `text` (or anything, if empty), within the dates."""
        return await self._sql.run(
            self._recall_sync, fts_query(text) if text else None, since, until, limit
        )

    def _recall_sync(
        self, query: str | None, since: datetime | None, until: datetime | None, limit: int
    ) -> list[Said]:
        where: list[str] = []
        params: list[object] = []
        if since is not None:
            where.append("t.at >= ?")
            params.append(since.astimezone(UTC).isoformat())
        if until is not None:
            where.append("t.at < ?")
            params.append(until.astimezone(UTC).isoformat())
        if query is not None:
            sql = (
                "SELECT t.at, t.who, t.text, t.trust FROM talk_fts f JOIN talk t "
                "ON t.rowid = f.rowid WHERE talk_fts MATCH ?"
                + "".join(f" AND {w}" for w in where)
                + " ORDER BY bm25(talk_fts) LIMIT ?"
            )
            params = [query, *params, limit]
        else:
            sql = (
                "SELECT t.at, t.who, t.text, t.trust FROM talk t"
                + (" WHERE " + " AND ".join(where) if where else "")
                + " ORDER BY t.seq DESC LIMIT ?"
            )
            params = [*params, limit]
        rows = self._sql.db.execute(sql, params).fetchall()
        said = [
            Said(datetime.fromisoformat(r[0]), str(r[1]), str(r[2]), Trust(int(r[3]))) for r in rows
        ]
        return sorted(said, key=lambda s: s.at)


def _passage(row: sqlite3.Row) -> Passage:
    return Passage(row[0], row[1], row[2], row[3], Trust(int(row[4])), row[5])


def _at(event: Event) -> datetime:
    return datetime.fromtimestamp(Timestamp.decode(event.hlc).wall_ms / 1000, UTC)
