from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from orion.core.events import NewEvent, Provenance, Trust
from orion.core.hlc import HybridClock
from orion.core.journal import SqliteJournal


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[SqliteJournal]:
    async with SqliteJournal(tmp_path / "journal.sqlite3", HybridClock("test")) as j:
        yield j


def make_event(kind: str = "test.happened", source: str = "test:1", **payload: object) -> NewEvent:
    return NewEvent(
        kind=kind,
        payload=dict(payload),
        provenance=Provenance(source=source, actor="test:runner"),
        trust=Trust.OWNER,
    )
