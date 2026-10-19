"""Background memory consolidation (the reflective process, M2 version).

After a turn, a cheap model reads the exchange next to what memory already holds and proposes
operations: add a fact, update one (the old version is kept as history), invalidate one, add or
close a commitment. Operations are validated before being applied; anything malformed is
dropped and logged. Nothing here runs on the conversation's critical path.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Literal, cast

import structlog
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from orion.core.events import Trust
from orion.memory.model import Category, CommitmentStatus
from orion.memory.render import format_commitment, format_fact
from orion.memory.service import MemoryService, Origin
from orion.memory.store import Fact
from orion.providers.base import ModelProvider, ModelRequest, ProviderError

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You maintain the long-term memory of Orion, a personal assistant.

You receive one exchange between the owner and Orion, plus what memory already holds on related
topics. Decide what, if anything, must change in memory. Reply with ONE JSON object:

{"operations": [ ... ]}

Allowed operations (dates are ISO, null when unknown):
- {"op": "add_fact", "statement": str, "about": [str],
   "category": "profile|preference|person|project|other", "valid_from": date}
- {"op": "update_fact", "replaces": [fact_id], ...same fields as add_fact}
- {"op": "invalidate_fact", "fact_id": str, "reason": str, "valid_to": date}
- {"op": "add_commitment", "what": str, "debtor": "owner|orion|<name>",
   "creditor": "owner|orion|<name>", "due": date or datetime}
- {"op": "close_commitment", "commitment_id": str, "status": "done|cancelled",
   "evidence": str | null}

Rules:
- Only record what the owner stated or clearly confirmed. Never record Orion's own guesses,
  suggestions or jokes as facts.
- Facts are durable: worth knowing in a month. Skip small talk, one-off questions, moods of the
  moment, and anything already in memory (same meaning counts as already there).
- When the owner corrects or changes something already in memory, use update_fact (or
  invalidate_fact if there is no replacement). Never delete history yourself.
- A commitment is a concrete promise with someone owing something: "je dois envoyer le devis à
  Paul vendredi", "Léa me rend mon livre", "rappelle-moi de…" (debtor: orion). Resolve relative
  dates against today's date.
- Write statements in the owner's language, self-contained, naming people explicitly.
- Most exchanges need no change: then reply {"operations": []}.
"""


class _AddFact(BaseModel):
    model_config = ConfigDict(extra="ignore")
    op: Literal["add_fact"]
    statement: str = Field(min_length=1, max_length=1000)
    about: list[str] = Field(default_factory=list[str])
    category: Category = Category.OTHER
    valid_from: str | None = None


class _UpdateFact(_AddFact):
    op: Literal["update_fact"]  # type: ignore[assignment]
    replaces: list[str] = Field(min_length=1)


class _InvalidateFact(BaseModel):
    model_config = ConfigDict(extra="ignore")
    op: Literal["invalidate_fact"]
    fact_id: str
    reason: str
    valid_to: str | None = None


class _AddCommitment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    op: Literal["add_commitment"]
    what: str = Field(min_length=1, max_length=500)
    debtor: str
    creditor: str
    due: str | None = None


class _CloseCommitment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    op: Literal["close_commitment"]
    commitment_id: str
    status: CommitmentStatus = CommitmentStatus.DONE
    evidence: str | None = None


Operation = Annotated[
    _AddFact | _UpdateFact | _InvalidateFact | _AddCommitment | _CloseCommitment,
    Field(discriminator="op"),
]
_OPERATION = TypeAdapter[Operation](Operation)


class ExtractionReport(BaseModel):
    applied: list[str] = Field(default_factory=list[str])
    rejected: list[str] = Field(default_factory=list[str])


class MemoryExtractor:
    def __init__(self, provider: ModelProvider, memory: MemoryService, model: str) -> None:
        self._provider = provider
        self._memory = memory
        self._model = model

    async def consolidate(
        self,
        owner_text: str,
        reply_text: str,
        origin: Origin,
        today: date,
        recorded_this_turn: list[Fact],
    ) -> ExtractionReport:
        related = await self._memory.store.search(f"{owner_text} {reply_text}", limit=12)
        known = {f.id: f for f in [*related, *recorded_this_turn]}
        commitments = await self._memory.store.commitments()
        user = "\n".join(
            [
                f"Today: {today.isoformat()} ({today.strftime('%A')})",
                "",
                "Memory already holds:",
                *(format_fact(f) for f in known.values()),
                *(format_commitment(c) for c in commitments),
                "" if known or commitments else "(nothing related)",
                "",
                f"Owner: {owner_text}",
                f"Orion: {reply_text}",
            ]
        )
        try:
            response = await self._provider.complete(
                ModelRequest(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user},
                    ],
                    session_id="orion:memory-extractor",
                    json_output=True,
                    temperature=0,
                )
            )
        except ProviderError:
            log.exception("memory.extract_failed")
            return ExtractionReport(rejected=["provider error"])
        return await self._apply(response.text, origin, set(known), {c.id for c in commitments})

    async def _apply(
        self, raw: str, origin: Origin, fact_ids: set[str], commitment_ids: set[str]
    ) -> ExtractionReport:
        report = ExtractionReport()
        try:
            items: object = json.loads(raw).get("operations", [])
        except (json.JSONDecodeError, AttributeError):
            log.warning("memory.extract_bad_json", raw=raw[:300])
            report.rejected.append("invalid JSON")
            return report
        if not isinstance(items, list):
            report.rejected.append("operations is not a list")
            return report
        for item in cast(list[object], items):
            try:
                op = _OPERATION.validate_python(item)
            except ValidationError as exc:
                report.rejected.append(f"invalid operation: {exc.errors(include_url=False)[:1]}")
                continue
            outcome = await self._apply_one(op, origin, fact_ids, commitment_ids)
            (report.applied if outcome.startswith("ok") else report.rejected).append(outcome)
        if report.applied or report.rejected:
            log.info("memory.extracted", applied=report.applied, rejected=report.rejected)
        return report

    async def _apply_one(
        self, op: Operation, origin: Origin, fact_ids: set[str], commitment_ids: set[str]
    ) -> str:
        memory = self._memory
        match op:
            case _UpdateFact():
                unknown = [i for i in op.replaces if i not in fact_ids]
                if unknown:
                    return f"update_fact refused: unknown ids {unknown}"
                fid = await memory.remember(
                    op.statement,
                    origin,
                    about=op.about,
                    category=op.category,
                    valid_from=op.valid_from,
                    supersedes=op.replaces,
                )
                return f"ok update_fact {fid}"
            case _AddFact():
                fid = await memory.remember(
                    op.statement,
                    origin,
                    about=op.about,
                    category=op.category,
                    valid_from=op.valid_from,
                )
                return f"ok add_fact {fid}"
            case _InvalidateFact():
                if op.fact_id not in fact_ids:
                    return f"invalidate_fact refused: unknown id {op.fact_id}"
                await memory.invalidate(op.fact_id, op.reason, origin, op.valid_to)
                return f"ok invalidate_fact {op.fact_id}"
            case _AddCommitment():
                cid = await memory.add_commitment(
                    op.what, origin, debtor=op.debtor, creditor=op.creditor, due=op.due
                )
                return f"ok add_commitment {cid}"
            case _CloseCommitment():
                if op.commitment_id not in commitment_ids:
                    return f"close_commitment refused: unknown id {op.commitment_id}"
                await memory.close_commitment(op.commitment_id, op.status, origin, op.evidence)
                return f"ok close_commitment {op.commitment_id}"


EXTRACTOR_TRUST = Trust.SYSTEM
"""Extracted memories are interpretations of an exchange, never above SYSTEM trust."""
