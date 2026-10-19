"""Memory service: the only way to change memory, always by appending journal events.

Also implements forgetting by source: every event whose provenance matches the source is
redacted, together with everything derived from or caused by those events (transitively),
then the memory projection is rebuilt and checked against a fresh replay of the journal.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from pydantic import BaseModel

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.memory.model import (
    COMMITMENT_CLOSED,
    COMMITMENT_CREATED,
    FACT_ASSERTED,
    FACT_INVALIDATED,
    Category,
    CommitmentClosed,
    CommitmentCreated,
    CommitmentStatus,
    FactAsserted,
    FactInvalidated,
)
from orion.memory.store import MemoryStore

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Origin:
    """Where a memory write comes from: who made it, from which event, at what trust."""

    actor: str
    source: str
    trust: Trust
    derived_from: tuple[str, ...] = ()
    correlation_id: str | None = None


@dataclass(frozen=True)
class ForgetReport:
    redacted_events: int
    facts_removed: int
    commitments_removed: int
    verified: bool


class MemoryService:
    def __init__(self, journal: EventStore, store: MemoryStore) -> None:
        self._journal = journal
        self.store = store
        self._runner = ProjectionRunner(journal, [store])

    async def sync(self) -> None:
        await self._runner.catch_up()

    async def remember(
        self,
        statement: str,
        origin: Origin,
        *,
        about: list[str] | None = None,
        category: Category = Category.OTHER,
        valid_from: str | None = None,
        supersedes: list[str] | None = None,
    ) -> str:
        fact_id = f"f_{new_id()}"
        payload = FactAsserted(
            fact_id=fact_id,
            statement=statement,
            about=about or [],
            category=category,
            valid_from=valid_from,
            supersedes=supersedes or [],
        )
        await self._write(FACT_ASSERTED, payload, origin)
        return fact_id

    async def invalidate(
        self, fact_id: str, reason: str, origin: Origin, valid_to: str | None = None
    ) -> None:
        await self._write(
            FACT_INVALIDATED,
            FactInvalidated(fact_id=fact_id, reason=reason, valid_to=valid_to),
            origin,
        )

    async def add_commitment(
        self, what: str, origin: Origin, *, debtor: str, creditor: str, due: str | None = None
    ) -> str:
        commitment_id = f"c_{new_id()}"
        payload = CommitmentCreated(
            commitment_id=commitment_id, debtor=debtor, creditor=creditor, what=what, due=due
        )
        await self._write(COMMITMENT_CREATED, payload, origin)
        return commitment_id

    async def close_commitment(
        self,
        commitment_id: str,
        status: CommitmentStatus,
        origin: Origin,
        evidence: str | None = None,
    ) -> None:
        payload = CommitmentClosed(commitment_id=commitment_id, status=status, evidence=evidence)
        await self._write(COMMITMENT_CLOSED, payload, origin)

    async def forget_source(self, source_prefix: str, reason: str, actor: str) -> ForgetReport:
        """Redact every event from `source_prefix` and everything derived from it."""
        events = await self._all_events()
        doomed = {e.id for e in events if e.provenance.source.startswith(source_prefix)}
        changed = True
        while changed:  # transitive closure over derivation and causation links
            changed = False
            for e in events:
                if e.id in doomed or e.redacted:
                    continue
                links = set(e.provenance.derived_from)
                if e.causation_id:
                    links.add(e.causation_id)
                if links & doomed:
                    doomed.add(e.id)
                    changed = True
        doomed = {e.id for e in events if e.id in doomed and not e.redacted}

        facts_before = {f.id for f in await self.store.facts(include_past=True)}
        commitments_before = {c.id for c in await self.store.commitments(status=None)}
        if doomed:
            await self._journal.redact(sorted(doomed), reason=reason, actor=actor)
        await self._runner.rebuild(self.store.name)
        facts_after = {f.id for f in await self.store.facts(include_past=True)}
        commitments_after = {c.id for c in await self.store.commitments(status=None)}

        verified = await self._verify_rebuild()
        report = ForgetReport(
            redacted_events=len(doomed),
            facts_removed=len(facts_before - facts_after),
            commitments_removed=len(commitments_before - commitments_after),
            verified=verified,
        )
        log.info("memory.forget", source=source_prefix, **report.__dict__)
        return report

    async def _verify_rebuild(self) -> bool:
        """Replay the journal into a fresh store and compare with the live one."""
        shadow = MemoryStore(":memory:")
        await shadow.open()
        try:
            await ProjectionRunner(self._journal, [shadow]).catch_up()
            same_facts = await shadow.facts(include_past=True) == await self.store.facts(
                include_past=True
            )
            same_commitments = await shadow.commitments(
                status=None
            ) == await self.store.commitments(status=None)
        finally:
            await shadow.close()
        return same_facts and same_commitments

    async def _all_events(self) -> list[Event]:
        events: list[Event] = []
        cursor = 0
        while batch := await self._journal.read(cursor, 1000):
            events.extend(batch)
            cursor = batch[-1].seq
        return events

    async def _write(self, kind: str, payload: BaseModel, origin: Origin) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload.model_dump(mode="json"),
                    provenance=Provenance(
                        source=origin.source, actor=origin.actor, derived_from=origin.derived_from
                    ),
                    trust=origin.trust,
                    causation_id=origin.derived_from[0] if origin.derived_from else None,
                    correlation_id=origin.correlation_id,
                )
            ]
        )
        await self.sync()
