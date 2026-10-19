"""Projections: derived state built by replaying the journal.

A projection must be a pure function of the events it has seen: dropping its state and
replaying the journal from the start has to rebuild exactly the same state. That property is
what makes memory reconstructible and forgetting verifiable.

Each projection's checkpoint (last applied sequence) is persisted next to its state by the
projection itself, so applying an event and advancing the checkpoint happen atomically.
"""

from __future__ import annotations

from typing import Protocol

import structlog

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


class ProjectionRunner:
    """Keeps a set of projections caught up with the journal."""

    def __init__(
        self, journal: EventStore, projections: list[Projection], batch: int = 500
    ) -> None:
        names = [p.name for p in projections]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate projection names: {names}")
        self._journal = journal
        self._projections = projections
        self._batch = batch

    async def catch_up(self) -> int:
        """Apply every pending event to every projection. Returns the number of applications."""
        applied = 0
        for projection in self._projections:
            applied += await self._catch_up_one(projection)
        return applied

    async def rebuild(self, name: str) -> int:
        """Reset one projection and replay the whole journal into it."""
        projection = self._find(name)
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
