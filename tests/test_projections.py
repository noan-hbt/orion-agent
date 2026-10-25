from __future__ import annotations

import asyncio

import pytest

from orion.core.events import Event
from orion.core.journal import SqliteJournal
from orion.core.projections import ProjectionRunner
from tests.conftest import make_event


class WordCount:
    """Toy projection: counts words per source. Pure function of the events it saw."""

    name = "word_count"

    def __init__(self, fail_on_seq: int | None = None) -> None:
        self.counts: dict[str, int] = {}
        self._checkpoint = 0
        self.fail_on_seq = fail_on_seq

    async def checkpoint(self) -> int:
        return self._checkpoint

    async def apply(self, event: Event) -> None:
        if event.seq == self.fail_on_seq:
            raise ValueError("boom")
        if event.kind == "note.added" and not event.redacted:
            source = event.provenance.source
            self.counts[source] = self.counts.get(source, 0) + len(
                str(event.payload["text"]).split()
            )
        self._checkpoint = event.seq

    async def reset(self) -> None:
        self.counts = {}
        self._checkpoint = 0


async def test_catch_up_is_incremental(journal: SqliteJournal) -> None:
    projection = WordCount()
    runner = ProjectionRunner(journal, [projection], batch=2)
    await journal.append([make_event("note.added", "a", text="un deux trois")])
    assert await runner.catch_up() == 1
    await journal.append([make_event("note.added", "b", text="quatre"), make_event("other")])
    assert await runner.catch_up() == 2
    assert projection.counts == {"a": 3, "b": 1}
    assert await runner.catch_up() == 0


async def test_catch_ups_at_once_apply_each_event_once(journal: SqliteJournal) -> None:
    projection = WordCount()
    runner = ProjectionRunner(journal, [projection], batch=1)
    await journal.append([make_event("note.added", "a", text=w) for w in ("un", "deux", "trois")])
    await asyncio.gather(runner.catch_up(), runner.catch_up(), runner.rebuild_all())
    assert projection.counts == {"a": 3}


async def test_rebuild_gives_identical_state_and_reflects_redaction(journal: SqliteJournal) -> None:
    projection = WordCount()
    runner = ProjectionRunner(journal, [projection])
    keep, forget = await journal.append(
        [
            make_event("note.added", "a", text="garde moi"),
            make_event("note.added", "b", text="oublie"),
        ]
    )
    await runner.catch_up()
    assert projection.counts == {"a": 2, "b": 1}

    await runner.rebuild("word_count")
    assert projection.counts == {"a": 2, "b": 1}

    await journal.redact([forget.id], reason="forget b", actor="owner:main")
    await runner.rebuild("word_count")
    assert projection.counts == {"a": 2}
    assert keep.id  # untouched


async def test_failed_apply_stops_at_checkpoint_and_resumes(journal: SqliteJournal) -> None:
    projection = WordCount(fail_on_seq=2)
    runner = ProjectionRunner(journal, [projection])
    await journal.append([make_event("note.added", "a", text=w) for w in ["x", "y", "z"]])
    with pytest.raises(ValueError):
        await runner.catch_up()
    assert await projection.checkpoint() == 1

    projection.fail_on_seq = None
    assert await runner.catch_up() == 2
    assert projection.counts == {"a": 3}


def test_duplicate_projection_names_rejected(journal: SqliteJournal) -> None:
    with pytest.raises(ValueError):
        ProjectionRunner(journal, [WordCount(), WordCount()])
