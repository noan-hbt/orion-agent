from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from orion.core.events import NewEvent, Provenance, Trust
from orion.core.hlc import HybridClock
from orion.core.journal import SqliteJournal
from orion.core.projections import ProjectionRunner, state_of


@pytest.fixture
async def journal(tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[SqliteJournal]:
    async with SqliteJournal(tmp_path / "journal.sqlite3", HybridClock("test")) as j:
        yield j
        marker = request.node.get_closest_marker("no_rebuild_check")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if marker is None:
            await rebuild_check(j)


async def rebuild_check(journal: SqliteJournal) -> None:
    """Every projection built on this test's journal must come back the same when rebuilt from
    the journal alone (D81): the whole suite becomes a replay test of every module."""
    different: list[str] = []
    for runner in list(ProjectionRunner.live):
        if runner.journal is not journal:
            continue
        for projection in runner.projections:
            try:
                await ProjectionRunner(journal, [projection]).catch_up()  # a live one may lag
                before = await state_of(projection)
            except Exception:  # noqa: BLE001 - its store was closed by another fixture's teardown
                continue
            await runner.rebuild(projection.name)
            after = await state_of(projection)
            if after != before:
                different.append(f"{projection.name}: {_first_difference(before, after)}")
    if different:
        pytest.fail(
            "projections not rebuilt identically from the journal:\n" + "\n".join(different)
        )


def _first_difference(before: Any, after: Any, path: str = "") -> str:
    if isinstance(before, dict) and isinstance(after, dict):
        b: dict[str, Any] = before  # pyright: ignore[reportUnknownVariableType]
        a: dict[str, Any] = after  # pyright: ignore[reportUnknownVariableType]
        for key in sorted({*b, *a}):
            if b.get(key) != a.get(key):
                return _first_difference(b.get(key), a.get(key), f"{path}.{key}")
    return f"{path or '<state>'}: {before!r:.200} != {after!r:.200}"


def make_event(kind: str = "test.happened", source: str = "test:1", **payload: object) -> NewEvent:
    return NewEvent(
        kind=kind,
        payload=dict(payload),
        provenance=Provenance(source=source, actor="test:runner"),
        trust=Trust.OWNER,
    )


@pytest.fixture(autouse=True)
def modules_run_in_this_process() -> Iterator[None]:
    """A test that starts the real guardian turns modules off for the process (D105)."""
    from orion.modules import installer

    yield
    installer._Process.imports_modules = True  # pyright: ignore[reportPrivateUsage]
