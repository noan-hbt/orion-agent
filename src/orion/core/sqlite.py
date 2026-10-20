"""A SQLite connection owned by one worker thread.

All calls run on that thread, so the asyncio loop never blocks on disk and the connection is
never shared across threads. Used by the journal and by projections that persist to SQLite.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path


class SqliteWorker:
    def __init__(self, path: Path | str, name: str, schema: str = "") -> None:
        self._path = path
        self._schema = schema
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        self._conn: sqlite3.Connection | None = None

    async def open(self) -> None:
        await self.run(self._open_sync)

    async def close(self) -> None:
        if self._conn is not None:
            await self.run(self._conn.close)
            self._conn = None
        self._executor.shutdown(wait=True)

    async def run[T](self, fn: Callable[..., T], *args: object) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    @property
    def db(self) -> sqlite3.Connection:
        """The connection. Only valid inside a function passed to `run`."""
        if self._conn is None:
            raise RuntimeError("database is not open")
        return self._conn

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

    def _open_sync(self) -> None:
        if isinstance(self._path, Path):
            self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        if self._schema:
            conn.executescript(self._schema)
        self._conn = conn


def dump(db: sqlite3.Connection, tables: tuple[str, ...]) -> dict[str, list[tuple[object, ...]]]:
    """Every row of these tables, in a stable order: a store's state, to compare after a
    rebuild (D81)."""
    return {
        table: sorted(
            (tuple(row) for row in db.execute(f"SELECT * FROM {table}")),
            key=repr,
        )
        for table in tables
    }
