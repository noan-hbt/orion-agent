"""Memory service: the only way to change memory, always by appending journal events.

Also implements forgetting by source: every event whose provenance matches the source is
redacted, together with everything derived from or caused by those events (transitively),
then the memory projection is rebuilt and checked against a fresh replay of the journal.

Two guards hold here, in code rather than in a prompt: what comes from outside (a mail, the web,
below SYSTEM trust) never replaces nor removes what Orion was told or worked out itself
(`MemoryRefused`); and a fact asserted again, word for word, is not stored twice.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel

from orion.core.events import Event, NewEvent, Provenance, Trust, owned_by
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.memory.model import (
    COMMITMENT_CLOSED,
    COMMITMENT_CREATED,
    FACT_ASSERTED,
    FACT_INVALIDATED,
    FACT_PINNED,
    PERSON_ALIASED,
    RULE_LEARNED,
    RULE_RETIRED,
    SETTING_CHANGED,
    Category,
    CommitmentClosed,
    CommitmentCreated,
    CommitmentStatus,
    FactAsserted,
    FactInvalidated,
    FactPinned,
    PersonAliased,
    RuleBasis,
    RuleLearned,
    RuleRetired,
    RuleScope,
    SettingChanged,
    SettingKey,
)
from orion.memory.situations import (
    CLOSED,
    OPENED,
    UPDATED,
    Ending,
    SituationClosed,
    SituationOpened,
    Situations,
    SituationUpdated,
)
from orion.memory.store import Fact, MemoryStore

if TYPE_CHECKING:
    from orion.memory.semantic import SemanticIndex

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Origin:
    """Where a memory write comes from: who made it, from which event, at what trust."""

    actor: str
    source: str
    trust: Trust
    derived_from: tuple[str, ...] = ()
    correlation_id: str | None = None


class MemoryRefused(ValueError):
    """A write that may not touch the facts it names (less trusted than they are)."""


def same_statement(text: str) -> str:
    """How two statements are compared for duplicates: case, accents, punctuation and spacing
    do not matter."""
    plain = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w]+", " ", plain).split())


@dataclass(frozen=True)
class ForgetReport:
    redacted_events: int
    facts_removed: int
    commitments_removed: int
    verified: bool


class MemoryService:
    def __init__(
        self, journal: EventStore, store: MemoryStore, semantic: SemanticIndex | None = None
    ) -> None:
        self._journal = journal
        self.store = store
        self.semantic = semantic
        self.situations = Situations()
        self._runner = ProjectionRunner(journal, [store, self.situations])

    async def search(self, text: str, limit: int = 8, include_past: bool = False) -> list[Fact]:
        """By meaning and by words when semantic search is set up, by words otherwise."""
        if self.semantic is not None:
            return await self.semantic.search(text, limit, include_past)
        return await self.store.search(text, limit, include_past)

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
        """The new fact's id, or the id of the same fact already known (nothing is written)."""
        supersedes = list(supersedes or [])
        await self._may_change(supersedes, origin)
        key = same_statement(statement)
        for known in await self.store.facts():
            if known.id in supersedes or same_statement(known.statement) != key:
                continue
            if known.trust >= origin.trust:
                return known.id  # already known, as surely
            supersedes.append(known.id)  # said again by a surer source: it takes its place
        fact_id = f"f_{new_id()}"
        payload = FactAsserted(
            fact_id=fact_id,
            statement=statement,
            about=about or [],
            category=category,
            valid_from=valid_from,
            supersedes=supersedes,
        )
        await self._write(FACT_ASSERTED, payload, origin)
        return fact_id

    async def pin(self, fact_id: str, pinned: bool, origin: Origin) -> None:
        await self._write(FACT_PINNED, FactPinned(fact_id=fact_id, pinned=pinned), origin)

    async def _may_change(self, fact_ids: list[str], origin: Origin) -> None:
        """Content from outside may not replace or remove a fact surer than itself."""
        if origin.trust >= Trust.SYSTEM:
            return
        for fact_id in fact_ids:
            fact = await self.store.fact(fact_id)
            if fact is not None and fact.trust > origin.trust:
                raise MemoryRefused(
                    f"{fact_id} is known at trust {fact.trust.name}, above {origin.trust.name}"
                )

    async def alias_person(self, alias: str, canonical: str, reason: str, origin: Origin) -> None:
        await self._write(
            PERSON_ALIASED, PersonAliased(alias=alias, canonical=canonical, reason=reason), origin
        )

    async def invalidate(
        self, fact_id: str, reason: str, origin: Origin, valid_to: str | None = None
    ) -> None:
        await self._may_change([fact_id], origin)
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

    async def open_situation(
        self,
        title: str,
        summary: str,
        origin: Origin,
        *,
        about: list[str] | None = None,
        until: str | None = None,
    ) -> str:
        situation_id = f"si_{new_id()}"
        payload = SituationOpened(
            situation_id=situation_id, title=title, summary=summary, about=about or [], until=until
        )
        await self._write(OPENED, payload, origin)
        return situation_id

    async def update_situation(
        self,
        situation_id: str,
        note: str,
        origin: Origin,
        *,
        summary: str | None = None,
        until: str | None = None,
    ) -> None:
        payload = SituationUpdated(
            situation_id=situation_id, note=note, summary=summary, until=until
        )
        await self._write(UPDATED, payload, origin)

    async def close_situation(
        self, situation_id: str, origin: Origin, ending: Ending = Ending.SETTLED, outcome: str = ""
    ) -> None:
        payload = SituationClosed(situation_id=situation_id, ending=ending, outcome=outcome)
        await self._write(CLOSED, payload, origin)

    async def learn_rule(
        self,
        rule: str,
        origin: Origin,
        *,
        scope: RuleScope = RuleScope.OTHER,
        basis: RuleBasis = RuleBasis.EXPLICIT,
        replaces: list[str] | None = None,
    ) -> str:
        rule_id = f"u_{new_id()}"
        payload = RuleLearned(
            rule_id=rule_id, rule=rule, scope=scope, basis=basis, replaces=replaces or []
        )
        await self._write(RULE_LEARNED, payload, origin)
        return rule_id

    async def retire_rule(self, rule_id: str, reason: str, origin: Origin) -> None:
        await self._write(RULE_RETIRED, RuleRetired(rule_id=rule_id, reason=reason), origin)

    async def change_setting(
        self, key: SettingKey, value: str | int | list[str], reason: str, origin: Origin
    ) -> str:
        change_id = f"s_{new_id()}"
        payload = SettingChanged(change_id=change_id, key=key, value=value, reason=reason)
        await self._write(SETTING_CHANGED, payload, origin)
        return change_id

    async def forget_source(self, source_prefix: str, reason: str, actor: str) -> ForgetReport:
        """Redact every event from `source_prefix` and everything derived from it."""
        events = await self._all_events()
        doomed = {e.id for e in events if owned_by(e.provenance.source, source_prefix)}
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
        await ProjectionRunner.rebuild_everywhere(self._journal)
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
            same_rules = await shadow.rules(include_retired=True) == await self.store.rules(
                include_retired=True
            )
            same_settings = await shadow.setting_changes() == await self.store.setting_changes()
            same_aliases = await shadow.aliases() == await self.store.aliases()
        finally:
            await shadow.close()
        return same_facts and same_commitments and same_rules and same_settings and same_aliases

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
