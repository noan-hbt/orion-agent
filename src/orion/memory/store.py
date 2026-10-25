"""Memory store: a SQLite projection of memory events, with full-text search.

Facts are bi-temporal. `valid_from` / `valid_to` say when a fact was true in the world;
`known_from_seq` / `known_to_seq` say when Orion learned it and when it stopped believing it
(the journal positions of the asserting and invalidating events). Nothing is overwritten: an
invalidated fact stays queryable as history.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import structlog

from orion.core.events import Event, Trust
from orion.core.sqlite import SqliteWorker, dump
from orion.memory.model import (
    COMMITMENT_CLOSED,
    COMMITMENT_CREATED,
    FACT_ASSERTED,
    FACT_INVALIDATED,
    PERSON_ALIASED,
    RULE_LEARNED,
    RULE_RETIRED,
    SETTING_CHANGED,
    CommitmentClosed,
    CommitmentCreated,
    CommitmentStatus,
    FactAsserted,
    FactInvalidated,
    PersonAliased,
    RuleBasis,
    RuleLearned,
    RuleRetired,
    RuleScope,
    SettingChanged,
    SettingKey,
)

log = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS facts (
    id              TEXT PRIMARY KEY,
    statement       TEXT NOT NULL,
    about           TEXT NOT NULL,
    category        TEXT NOT NULL,
    valid_from      TEXT,
    valid_to        TEXT,
    known_from_seq  INTEGER NOT NULL,
    known_from_hlc  TEXT NOT NULL,
    known_to_seq    INTEGER,
    invalid_reason  TEXT,
    trust           INTEGER NOT NULL,
    event_id        TEXT NOT NULL,
    source          TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    fact_id UNINDEXED, statement, about, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS commitments (
    id          TEXT PRIMARY KEY,
    debtor      TEXT NOT NULL,
    creditor    TEXT NOT NULL,
    what        TEXT NOT NULL,
    due         TEXT,
    status      TEXT NOT NULL,
    created_seq INTEGER NOT NULL,
    closed_seq  INTEGER,
    evidence    TEXT,
    trust       INTEGER NOT NULL,
    event_id    TEXT NOT NULL,
    source      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rules (
    id              TEXT PRIMARY KEY,
    rule            TEXT NOT NULL,
    scope           TEXT NOT NULL,
    basis           TEXT NOT NULL,
    created_seq     INTEGER NOT NULL,
    retired_seq     INTEGER,
    retired_reason  TEXT,
    trust           INTEGER NOT NULL,
    event_id        TEXT NOT NULL,
    source          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS aliases (
    alias_key   TEXT PRIMARY KEY,
    alias       TEXT NOT NULL,
    canonical   TEXT NOT NULL,
    reason      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    trust       INTEGER NOT NULL,
    event_id    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS setting_changes (
    id          TEXT PRIMARY KEY,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    reason      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    actor       TEXT NOT NULL,
    trust       INTEGER NOT NULL,
    event_id    TEXT NOT NULL
);
"""

_WORD = re.compile(r"\w{3,}", re.UNICODE)


def name_key(name: str) -> str:
    """How names are compared: case, accents and spacing do not matter."""
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).split())


@dataclass(frozen=True)
class Fact:
    id: str
    statement: str
    about: list[str]
    category: str
    valid_from: str | None
    valid_to: str | None
    known_from_seq: int
    known_to_seq: int | None
    invalid_reason: str | None
    trust: Trust
    source: str

    @property
    def current(self) -> bool:
        return self.known_to_seq is None


@dataclass(frozen=True)
class Commitment:
    id: str
    debtor: str
    creditor: str
    what: str
    due: str | None
    status: CommitmentStatus
    evidence: str | None
    trust: Trust


@dataclass(frozen=True)
class Rule:
    id: str
    rule: str
    scope: RuleScope
    basis: RuleBasis
    created_seq: int
    retired_seq: int | None
    retired_reason: str | None
    trust: Trust
    source: str

    @property
    def active(self) -> bool:
        return self.retired_seq is None


@dataclass(frozen=True)
class SettingChange:
    id: str
    key: SettingKey
    value: str | int | list[str]
    reason: str
    seq: int
    actor: str
    trust: Trust


def _rule(row: sqlite3.Row) -> Rule:
    return Rule(
        id=row["id"],
        rule=row["rule"],
        scope=RuleScope(row["scope"]),
        basis=RuleBasis(row["basis"]),
        created_seq=row["created_seq"],
        retired_seq=row["retired_seq"],
        retired_reason=row["retired_reason"],
        trust=Trust(row["trust"]),
        source=row["source"],
    )


def _setting(row: sqlite3.Row) -> SettingChange:
    return SettingChange(
        id=row["id"],
        key=SettingKey(row["key"]),
        value=json.loads(row["value"]),
        reason=row["reason"],
        seq=row["seq"],
        actor=row["actor"],
        trust=Trust(row["trust"]),
    )


def _fact(row: sqlite3.Row) -> Fact:
    return Fact(
        id=row["id"],
        statement=row["statement"],
        about=json.loads(row["about"]),
        category=row["category"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        known_from_seq=row["known_from_seq"],
        known_to_seq=row["known_to_seq"],
        invalid_reason=row["invalid_reason"],
        trust=Trust(row["trust"]),
        source=row["source"],
    )


def _commitment(row: sqlite3.Row) -> Commitment:
    return Commitment(
        id=row["id"],
        debtor=row["debtor"],
        creditor=row["creditor"],
        what=row["what"],
        due=row["due"],
        status=CommitmentStatus(row["status"]),
        evidence=row["evidence"],
        trust=Trust(row["trust"]),
    )


def fts_query(text: str, max_terms: int = 24) -> str | None:
    """Turn free text into a safe FTS5 OR-query of quoted words (no operators leak through)."""
    words: list[str] = []
    for word in _WORD.findall(text.lower()):
        if word not in words:
            words.append(word)
    if not words:
        return None
    return " OR ".join(f'"{w}"' for w in words[:max_terms])


class MemoryStore:
    """Projection of memory events. Use `path=":memory:"` in tests."""

    name = "memory"

    def __init__(self, path: Path | str) -> None:
        self._sql = SqliteWorker(path, "memory", _SCHEMA)

    async def open(self) -> None:
        await self._sql.open()

    async def close(self) -> None:
        await self._sql.close()

    # --- Projection protocol ---

    async def checkpoint(self) -> int:
        return await self._sql.run(self._checkpoint_sync)

    async def apply(self, event: Event) -> None:
        await self._sql.run(self._apply_sync, event)

    async def reset(self) -> None:
        await self._sql.run(self._reset_sync)

    async def snapshot(self) -> dict[str, list[tuple[object, ...]]]:
        """Every row: what a rebuild from the journal must give back (D81)."""
        return await self._sql.run(
            lambda: dump(
                self._sql.db,
                ("meta", "facts", "commitments", "rules", "aliases", "setting_changes"),
            )
        )

    # --- queries ---

    async def search(self, text: str, limit: int = 8, include_past: bool = False) -> list[Fact]:
        query = fts_query(text)
        if query is None:
            return []
        return await self._sql.run(self._search_sync, query, limit, include_past)

    async def facts(self, include_past: bool = False) -> list[Fact]:
        return await self._sql.run(self._facts_sync, include_past)

    async def fact(self, fact_id: str) -> Fact | None:
        return await self._sql.run(self._fact_sync, fact_id)

    async def commitments(
        self, status: CommitmentStatus | None = CommitmentStatus.OPEN
    ) -> list[Commitment]:
        return await self._sql.run(self._commitments_sync, status)

    async def commitment(self, commitment_id: str) -> Commitment | None:
        return await self._sql.run(self._commitment_sync, commitment_id)

    async def rules(self, include_retired: bool = False) -> list[Rule]:
        return await self._sql.run(self._rules_sync, include_retired)

    async def rule(self, rule_id: str) -> Rule | None:
        return await self._sql.run(self._rule_sync, rule_id)

    async def aliases(self) -> dict[str, str]:
        """alias key → canonical name."""
        return await self._sql.run(self._aliases_sync)

    def _aliases_sync(self) -> dict[str, str]:
        rows = self._sql.db.execute("SELECT alias_key, canonical FROM aliases ORDER BY seq")
        return {str(r[0]): str(r[1]) for r in rows}

    async def setting_changes(self) -> list[SettingChange]:
        """Every setting change, oldest first. The current value of a key is its last change."""
        return await self._sql.run(self._settings_sync)

    async def settings(self) -> dict[SettingKey, SettingChange]:
        return {change.key: change for change in await self.setting_changes()}

    # --- synchronous internals (memory thread only) ---

    def _checkpoint_sync(self) -> int:
        row = self._sql.db.execute("SELECT value FROM meta WHERE key = 'checkpoint'").fetchone()
        return int(row[0]) if row else 0

    def _reset_sync(self) -> None:
        with self._sql.transaction() as db:
            db.execute("DELETE FROM facts")
            db.execute("DELETE FROM facts_fts")
            db.execute("DELETE FROM commitments")
            db.execute("DELETE FROM rules")
            db.execute("DELETE FROM setting_changes")
            db.execute("DELETE FROM aliases")
            db.execute("DELETE FROM meta")

    def _apply_sync(self, event: Event) -> None:
        with self._sql.transaction() as db:
            row = db.execute("SELECT value FROM meta WHERE key = 'checkpoint'").fetchone()
            if row is not None and int(row[0]) >= event.seq:
                return  # already applied
            if not event.redacted:
                self._dispatch(db, event)
            db.execute(
                "INSERT INTO meta (key, value) VALUES ('checkpoint', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(event.seq),),
            )

    def _dispatch(self, db: sqlite3.Connection, event: Event) -> None:
        if event.kind == FACT_ASSERTED:
            self._assert_fact(db, event, FactAsserted.model_validate(event.payload))
        elif event.kind == FACT_INVALIDATED:
            payload = FactInvalidated.model_validate(event.payload)
            self._invalidate(db, payload.fact_id, payload.reason, payload.valid_to, event.seq)
        elif event.kind == COMMITMENT_CREATED:
            c = CommitmentCreated.model_validate(event.payload)
            db.execute(
                "INSERT OR IGNORE INTO commitments (id, debtor, creditor, what, due, status, "
                "created_seq, trust, event_id, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    c.commitment_id,
                    c.debtor,
                    c.creditor,
                    c.what,
                    c.due,
                    CommitmentStatus.OPEN.value,
                    event.seq,
                    int(event.trust),
                    event.id,
                    event.provenance.source,
                ),
            )
        elif event.kind == COMMITMENT_CLOSED:
            c = CommitmentClosed.model_validate(event.payload)
            db.execute(
                "UPDATE commitments SET status = ?, closed_seq = ?, evidence = ? "
                "WHERE id = ? AND status = 'open'",
                (c.status.value, event.seq, c.evidence, c.commitment_id),
            )

        elif event.kind == RULE_LEARNED:
            self._learn_rule(db, event, RuleLearned.model_validate(event.payload))
        elif event.kind == RULE_RETIRED:
            r = RuleRetired.model_validate(event.payload)
            self._retire_rule(db, r.rule_id, r.reason, event.seq)
        elif event.kind == PERSON_ALIASED:
            a = PersonAliased.model_validate(event.payload)
            db.execute(
                "INSERT OR REPLACE INTO aliases (alias_key, alias, canonical, reason, seq, trust, "
                "event_id) VALUES (?,?,?,?,?,?,?)",
                (
                    name_key(a.alias),
                    a.alias,
                    a.canonical,
                    a.reason,
                    event.seq,
                    int(event.trust),
                    event.id,
                ),
            )
        elif event.kind == SETTING_CHANGED:
            c = SettingChanged.model_validate(event.payload)
            db.execute(
                "INSERT OR IGNORE INTO setting_changes (id, key, value, reason, seq, actor, trust, "
                "event_id) VALUES (?,?,?,?,?,?,?,?)",
                (
                    c.change_id,
                    c.key.value,
                    json.dumps(c.value, ensure_ascii=False),
                    c.reason,
                    event.seq,
                    event.provenance.actor,
                    int(event.trust),
                    event.id,
                ),
            )

    def _learn_rule(self, db: sqlite3.Connection, event: Event, rule: RuleLearned) -> None:
        for old in rule.replaces:
            self._retire_rule(db, old, f"replaced by {rule.rule_id}", event.seq)
        db.execute(
            "INSERT OR IGNORE INTO rules (id, rule, scope, basis, created_seq, trust, event_id, "
            "source) VALUES (?,?,?,?,?,?,?,?)",
            (
                rule.rule_id,
                rule.rule,
                rule.scope.value,
                rule.basis.value,
                event.seq,
                int(event.trust),
                event.id,
                event.provenance.source,
            ),
        )

    @staticmethod
    def _retire_rule(db: sqlite3.Connection, rule_id: str, reason: str, seq: int) -> None:
        db.execute(
            "UPDATE rules SET retired_seq = ?, retired_reason = ? "
            "WHERE id = ? AND retired_seq IS NULL",
            (seq, reason, rule_id),
        )

    def _assert_fact(self, db: sqlite3.Connection, event: Event, fact: FactAsserted) -> None:
        for old in fact.supersedes:
            self._invalidate(db, old, f"superseded by {fact.fact_id}", fact.valid_from, event.seq)
        about = [a.strip() for a in fact.about if a.strip()]
        inserted = db.execute(
            "INSERT OR IGNORE INTO facts (id, statement, about, category, valid_from, "
            "known_from_seq, known_from_hlc, trust, event_id, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                fact.fact_id,
                fact.statement,
                json.dumps(about, ensure_ascii=False),
                fact.category.value,
                fact.valid_from,
                event.seq,
                event.hlc,
                int(event.trust),
                event.id,
                event.provenance.source,
            ),
        )
        if inserted.rowcount == 0:
            return  # the same fact asserted again: searched once
        db.execute(
            "INSERT INTO facts_fts (fact_id, statement, about) VALUES (?,?,?)",
            (fact.fact_id, fact.statement, " ".join(about)),
        )

    @staticmethod
    def _invalidate(
        db: sqlite3.Connection, fact_id: str, reason: str, valid_to: str | None, seq: int
    ) -> None:
        db.execute(
            "UPDATE facts SET known_to_seq = ?, invalid_reason = ?, "
            "valid_to = COALESCE(?, valid_to) WHERE id = ? AND known_to_seq IS NULL",
            (seq, reason, valid_to, fact_id),
        )

    def _search_sync(self, query: str, limit: int, include_past: bool) -> list[Fact]:
        rows = self._sql.db.execute(
            "SELECT facts.* FROM facts_fts JOIN facts ON facts.id = facts_fts.fact_id "
            "WHERE facts_fts MATCH ? AND (? OR facts.known_to_seq IS NULL) "
            "ORDER BY bm25(facts_fts) LIMIT ?",
            (query, include_past, limit),
        )
        return [_fact(r) for r in rows]

    def _facts_sync(self, include_past: bool) -> list[Fact]:
        rows = self._sql.db.execute(
            "SELECT * FROM facts WHERE ? OR known_to_seq IS NULL ORDER BY known_from_seq",
            (include_past,),
        )
        return [_fact(r) for r in rows]

    def _fact_sync(self, fact_id: str) -> Fact | None:
        row = self._sql.db.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
        return _fact(row) if row else None

    def _commitments_sync(self, status: CommitmentStatus | None) -> list[Commitment]:
        if status is None:
            rows = self._sql.db.execute("SELECT * FROM commitments ORDER BY created_seq")
        else:
            rows = self._sql.db.execute(
                "SELECT * FROM commitments WHERE status = ? ORDER BY due IS NULL, due, created_seq",
                (status.value,),
            )
        return [_commitment(r) for r in rows]

    def _commitment_sync(self, commitment_id: str) -> Commitment | None:
        row = self._sql.db.execute(
            "SELECT * FROM commitments WHERE id = ?", (commitment_id,)
        ).fetchone()
        return _commitment(row) if row else None

    def _rules_sync(self, include_retired: bool) -> list[Rule]:
        rows = self._sql.db.execute(
            "SELECT * FROM rules WHERE ? OR retired_seq IS NULL ORDER BY created_seq",
            (include_retired,),
        )
        return [_rule(r) for r in rows]

    def _rule_sync(self, rule_id: str) -> Rule | None:
        row = self._sql.db.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
        return _rule(row) if row else None

    def _settings_sync(self) -> list[SettingChange]:
        rows = self._sql.db.execute("SELECT * FROM setting_changes ORDER BY seq")
        return [_setting(r) for r in rows]
