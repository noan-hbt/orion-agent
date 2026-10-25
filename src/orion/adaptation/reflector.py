"""Reflection: Orion reviews how its recent exchanges went and adjusts its playbook.

A cheap model reads the owner's recent messages next to Orion's answers, the owner's tapbacks,
and whether proactive messages got a response. It proposes at most a few rules ("Il répond en
deux mots le matin : rester bref avant 10h") or retires rules the evidence now contradicts.

Guard rails, enforced in code:
- every rule cites at least two pieces of evidence, all of them the owner's own events;
- only rules Orion inferred can be retired here; what the owner said explicitly stays until
  the owner changes it;
- learned rules sit at SYSTEM trust and derive from their evidence, so forgetting a
  conversation also forgets what was learned from it.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal, cast

import structlog
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from orion.adaptation.signals import Signal
from orion.core.events import Trust
from orion.memory.model import RuleBasis, RuleScope
from orion.memory.render import format_rule
from orion.memory.service import MemoryService, Origin
from orion.memory.store import Rule
from orion.providers.base import ModelProvider, ModelRequest, ProviderError

log = structlog.get_logger(__name__)

MAX_NEW_RULES = 3
MIN_EVIDENCE = 2
REFLECTION_TRUST = Trust.SYSTEM

SYSTEM_PROMPT = """You help Orion, a personal assistant, adapt to its owner over time.

You receive Orion's current playbook (rules on how to work with this owner; [appris] marks
rules Orion inferred, the others were stated by the owner) and recent signals:
- MESSAGE: what the owner wrote, and what Orion answered;
- REACTION: a tapback the owner put on one of Orion's messages;
- INITIATIVE: Orion wrote first; engaged tells whether the owner responded within 3 hours.

Look for durable patterns in how the owner likes to be helped: length and tone of answers,
humour, format, language and register, timing, topics they never want or always want, how they
react to proactive messages. Ignore one-offs and moods. Do not restate facts about the owner's
life (that is memory, not playbook).

Reply with ONE JSON object: {"operations": [...]}. Allowed operations:
- {"op": "learn", "rule": str, "scope": "style|initiative|work|other",
   "evidence": [signal ids], "replaces": [rule ids]}
- {"op": "retire", "rule_id": str, "reason": str, "evidence": [signal ids]}

Rules:
- A rule is one short, actionable sentence in the owner's language, written as an instruction
  to Orion ("Réponses courtes le matin : deux phrases max."). Refer to the owner as "il"/"elle"
  or by name, never as "the owner" or "l'owner".
- Each learned rule cites at least two signals that clearly support it.
- Only retire rules marked [appris], and only when recent signals contradict them.
- Never contradict a rule the owner stated; refine it with "replaces" only if it is [appris].
- At most three operations. Most of the time, nothing needs to change: {"operations": []}.
"""


class _Learn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    op: Literal["learn"]
    rule: str = Field(min_length=3, max_length=300)
    scope: RuleScope = RuleScope.OTHER
    evidence: list[str] = Field(default_factory=list[str])
    replaces: list[str] = Field(default_factory=list[str])


class _Retire(BaseModel):
    model_config = ConfigDict(extra="ignore")
    op: Literal["retire"]
    rule_id: str
    reason: str = Field(min_length=1, max_length=300)
    evidence: list[str] = Field(default_factory=list[str])


Operation = Annotated[_Learn | _Retire, Field(discriminator="op")]
_OPERATION = TypeAdapter[Operation](Operation)


class ReflectionReport(BaseModel):
    learned: list[str] = Field(default_factory=list[str])
    retired: list[str] = Field(default_factory=list[str])
    rejected: list[str] = Field(default_factory=list[str])
    unavailable: bool = Field(default=False, exclude=True)
    """The model could not be reached: nothing was reviewed, to be tried again."""


def render_signals(signals: list[Signal]) -> str:
    lines: list[str] = []
    for s in signals:
        when = f"{s.at:%a %H:%M}"
        if s.kind == "message":
            lines.append(f"[{s.id}] MESSAGE {when} owner: {s.text}")
            if s.reply:
                lines.append(f"    Orion answered: {s.reply}")
        elif s.kind == "reaction":
            lines.append(f"[{s.id}] REACTION {when} {s.text}")
        else:
            state = {True: "yes", False: "no", None: "too early"}[s.engaged]
            lines.append(f"[{s.id}] INITIATIVE {when} {s.text} (engaged: {state})")
    return "\n".join(lines)


class Reflector:
    def __init__(self, provider: ModelProvider, memory: MemoryService, model: str) -> None:
        self._provider = provider
        self._memory = memory
        self._model = model

    async def reflect(self, signals: list[Signal]) -> ReflectionReport:
        rules = await self._memory.store.rules()
        user = "\n".join(
            [
                "Current playbook:",
                *(format_rule(r) for r in rules),
                "" if rules else "(empty)",
                "",
                "Recent signals:",
                render_signals(signals),
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
                    session_id="orion:reflector",
                    json_output=True,
                    temperature=0,
                )
            )
        except ProviderError:
            log.exception("adaptation.reflect_failed")
            return ReflectionReport(rejected=["provider error"], unavailable=True)
        evidence = {s.id for s in signals if s.kind in ("message", "reaction")}
        return await self._apply(response.text, {r.id: r for r in rules}, evidence)

    async def _apply(
        self, raw: str, rules: dict[str, Rule], evidence_ids: set[str]
    ) -> ReflectionReport:
        report = ReflectionReport()
        try:
            items: object = json.loads(raw).get("operations", [])
        except (json.JSONDecodeError, AttributeError):
            log.warning("adaptation.bad_json", raw=raw[:300])
            report.rejected.append("invalid JSON")
            return report
        if not isinstance(items, list):
            report.rejected.append("operations is not a list")
            return report
        for item in cast(list[object], items)[: MAX_NEW_RULES * 2]:
            try:
                op = _OPERATION.validate_python(item)
            except ValidationError as exc:
                report.rejected.append(f"invalid operation: {exc.errors(include_url=False)[:1]}")
                continue
            outcome = await self._apply_one(op, rules, evidence_ids, report)
            if outcome is not None:
                report.rejected.append(outcome)
        if report.learned or report.retired or report.rejected:
            log.info("adaptation.reflected", **report.model_dump())
        return report

    async def _apply_one(
        self,
        op: Operation,
        rules: dict[str, Rule],
        evidence_ids: set[str],
        report: ReflectionReport,
    ) -> str | None:
        cited = sorted(set(op.evidence) & evidence_ids)
        if len(set(op.evidence)) != len(cited):
            return f"{op.op} refused: evidence outside the owner's signals"
        match op:
            case _Learn():
                if len(report.learned) >= MAX_NEW_RULES:
                    return "learn refused: too many new rules at once"
                if len(cited) < MIN_EVIDENCE:
                    return f"learn refused: needs {MIN_EVIDENCE} pieces of evidence"
                if any(r.rule.strip().lower() == op.rule.strip().lower() for r in rules.values()):
                    return "learn refused: already in the playbook"
                for rule_id in op.replaces:
                    old = rules.get(rule_id)
                    if old is None or old.basis is not RuleBasis.INFERRED:
                        return f"learn refused: cannot replace {rule_id}"
                rule_id = await self._memory.learn_rule(
                    op.rule,
                    self._origin(cited),
                    scope=op.scope,
                    basis=RuleBasis.INFERRED,
                    replaces=op.replaces,
                )
                report.learned.append(rule_id)
            case _Retire():
                old = rules.get(op.rule_id)
                if old is None or old.basis is not RuleBasis.INFERRED:
                    return f"retire refused: {op.rule_id} is not an inferred rule"
                if not cited:
                    return "retire refused: no evidence"
                await self._memory.retire_rule(op.rule_id, op.reason, self._origin(cited))
                report.retired.append(op.rule_id)
        return None

    @staticmethod
    def _origin(evidence: list[str]) -> Origin:
        return Origin(
            actor="orion:adaptation",
            source="orion:adaptation",
            trust=REFLECTION_TRUST,
            derived_from=tuple(evidence),
        )
