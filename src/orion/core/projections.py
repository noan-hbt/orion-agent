"""Projections: derived state built by replaying the journal.

A projection must be a pure function of the events it has seen: dropping its state and
replaying the journal from the start has to rebuild exactly the same state. That property is
what makes memory reconstructible and forgetting verifiable.

Each projection's checkpoint (last applied sequence) is persisted next to its state by the
projection itself, so applying an event and advancing the checkpoint happen atomically.
"""

from __future__ import annotations

import asyncio
import dataclasses
import weakref
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Any, ClassVar, Protocol

import structlog
from pydantic import BaseModel

from orion.core.events import Event
from orion.core.journal import EventStore

log = structlog.get_logger(__name__)


class Projection(Protocol):
    name: str

    async def checkpoint(self) -> int:
        """Last journal sequence applied (0 if none)."""
        ...

    async def apply(self, event: Event) -> None:
        """Apply one event and advance the checkpoint to `event.seq`, atomically."""
        ...

    async def reset(self) -> None:
        """Drop all derived state and set the checkpoint back to 0."""
        ...


def snapshot(value: Any, _seen: frozenset[int] = frozenset()) -> Any:
    """A projection's state as plain, comparable data: what a rebuild must give back.

    A projection kept outside Python objects (in SQLite) gives its own with an async
    `snapshot()`; this one reads the attributes of the others."""
    if isinstance(value, str | int | float | bool | None | Enum):
        return value
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if id(value) in _seen:
        return "<cycle>"
    seen = _seen | {id(value)}
    if isinstance(value, dict):
        items: dict[Any, Any] = value  # pyright: ignore[reportUnknownVariableType]
        pairs = ((repr(snapshot(k, seen)), snapshot(v, seen)) for k, v in items.items())
        return sorted(pairs, key=repr)
    if isinstance(value, set | frozenset):
        members: frozenset[Any] = frozenset(value)  # pyright: ignore[reportUnknownArgumentType]
        return sorted((snapshot(v, seen) for v in members), key=repr)
    if isinstance(value, list | tuple):
        sequence: list[Any] = list(value)  # pyright: ignore[reportUnknownArgumentType]
        return [snapshot(v, seen) for v in sequence]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: snapshot(getattr(value, f.name), seen) for f in dataclasses.fields(value)}
    if isinstance(value, BaseModel):
        return snapshot(value.model_dump(), seen)
    if hasattr(value, "__dict__"):
        return {k: snapshot(v, seen) for k, v in sorted(vars(value).items())}
    return repr(value)


class ProjectionRunner:
    """Keeps a set of projections caught up with the journal."""

    live: ClassVar[weakref.WeakSet[ProjectionRunner]] = weakref.WeakSet()
    """Every runner alive in this process: the test suite rebuilds each one's projections at
    the end of every test, to prove they depend on the journal alone (D81)."""

    def __init__(
        self, journal: EventStore, projections: list[Projection], batch: int = 500
    ) -> None:
        names = [p.name for p in projections]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate projection names: {names}")
        self._journal = journal
        self._projections = projections
        self._batch = batch
        self._lock = asyncio.Lock()
        """One catch-up or rebuild at a time: two reading the same cursor would apply the same
        events twice, and a rebuild must not reset state under a catch-up."""
        ProjectionRunner.live.add(self)

    @property
    def journal(self) -> EventStore:
        return self._journal

    @property
    def projections(self) -> list[Projection]:
        return list(self._projections)

    async def verify(self) -> list[str]:
        """Rebuild each projection from the journal and compare it with what it was. Returns
        the names of those that came back different (their state does not depend on the
        journal alone). Resets live state: run it on a copy, or in tests."""
        different: list[str] = []
        async with self._lock:
            for projection in self._projections:
                before = await state_of(projection)
                await self._rebuild(projection)
                if await state_of(projection) != before:
                    different.append(projection.name)
        return different

    async def catch_up(self) -> int:
        """Apply every pending event to every projection. Returns the number of applications."""
        applied = 0
        async with self._lock:
            for projection in self._projections:
                applied += await self._catch_up_one(projection)
        return applied

    async def rebuild(self, name: str) -> int:
        """Reset one projection and replay the whole journal into it."""
        projection = self._find(name)
        async with self._lock:
            return await self._rebuild(projection)

    async def rebuild_all(self) -> int:
        """Every projection reset and replayed: after a redaction, nothing keeps what was
        forgotten."""
        applied = 0
        async with self._lock:
            for projection in self._projections:
                applied += await self._rebuild(projection)
        return applied

    @classmethod
    async def rebuild_everywhere(cls, journal: EventStore) -> int:
        """Every projection of every runner on `journal` in this process, rebuilt: after a
        redaction, no module keeps what was forgotten (its library, its pending extraction,
        its doubts…), not only the one that forgot."""
        applied = 0
        for runner in list(cls.live):
            if runner.journal is journal:
                applied += await runner.rebuild_all()
        return applied

    async def _rebuild(self, projection: Projection) -> int:
        name = projection.name
        await projection.reset()
        log.info("projection.rebuild", projection=name)
        return await self._catch_up_one(projection)

    async def _catch_up_one(self, projection: Projection) -> int:
        cursor = await projection.checkpoint()
        applied = 0
        while True:
            events = await self._journal.read(cursor, self._batch)
            if not events:
                return applied
            for event in events:
                try:
                    await projection.apply(event)
                except Exception:
                    # Stop this projection at the failing event; it resumes from its checkpoint.
                    log.exception(
                        "projection.apply_failed",
                        projection=projection.name,
                        seq=event.seq,
                        kind=event.kind,
                    )
                    raise
                cursor = event.seq
                applied += 1

    def _find(self, name: str) -> Projection:
        for projection in self._projections:
            if projection.name == name:
                return projection
        raise KeyError(f"unknown projection: {name}")


async def state_of(projection: Projection) -> Any:
    custom = getattr(projection, "snapshot", None)
    if custom is not None:
        return snapshot(await custom())
    return snapshot(projection)
