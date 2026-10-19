"""Verifier: a different model checks a draft against its sources before it goes out.

Used where an error costs the most trust for the least benefit: messages Orion sends on its own
initiative. The verifier sees only the draft and the facts it should rest on (never the
conversation), and answers whether the draft states anything those facts do not support: a
wrong date, time, amount, name, or an invented detail. Generator and verifier are different
models, so they do not share the same blind spots.

Fail closed: if the verifier cannot answer, the draft counts as not verified.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from orion.providers.base import ModelProvider, ModelRequest, ProviderError

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You check a message before a personal assistant sends it to its owner.

You get FACTS (the only ground truth) and a DRAFT. Find statements in the draft that contradict
the facts or add specifics the facts do not contain: dates, weekdays, times, amounts, names,
places, deadlines, claims that something was done. Tone, wording, jokes, greetings, opinions,
questions and offers to help are fine. Paraphrase and reasonable reading of the facts are fine
(something "not marked as done" may be called "still not done"; "demain" for tomorrow's date).
Flag only what would mislead the owner about a fact; when in doubt, it is ok.

Reply with ONE JSON object: {"ok": true|false, "issues": ["short description", ...]}.
"""


class _Verdict(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ok: bool
    issues: list[str] = Field(default_factory=list[str])


@dataclass(frozen=True)
class Verification:
    ok: bool
    issues: list[str] = field(default_factory=list[str])


class Verifier:
    def __init__(self, provider: ModelProvider, model: str) -> None:
        self._provider = provider
        self._model = model

    async def check(self, draft: str, facts: str) -> Verification:
        try:
            response = await self._provider.complete(
                ModelRequest(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": f"FACTS:\n{facts}\n\nDRAFT:\n{draft}"},
                    ],
                    session_id="orion:verifier",
                    json_output=True,
                    temperature=0,
                )
            )
        except ProviderError:
            log.exception("verifier.unavailable")
            return Verification(False, ["verifier unavailable"])
        try:
            verdict = _Verdict.model_validate(json.loads(response.text))
        except (json.JSONDecodeError, ValidationError):
            log.warning("verifier.bad_answer", raw=response.text[:300])
            return Verification(False, ["verifier answer unreadable"])
        return Verification(verdict.ok, verdict.issues)
