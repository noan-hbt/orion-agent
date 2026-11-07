"""What a proactive message must pass before it goes, one check per kind (F-09): each has an
outcome, journaled with the decision, instead of one model's verdict mixing everything.

- `outcome`, in code and free: a message where Orion says it did something (« j'ai réservé »,
  « j'ai envoyé ») needs an action seen CONFIRMED (`orion.tools.outcome`, the only proof rule,
  D274), in this turn or in what it was told. No model call: the rule is the proof.
- `factual`: the second model (`Verifier`), against what Orion knew and read.

Relevance to a goal and the guardian's policy on an action are not checked here yet.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from orion.cognition.grounding import claims_a_state
from orion.tools.outcome import Outcome, outcomes

_DID = re.compile(
    r"\bj(?:e l|e les|e t|e lui|e leur)?'ai (?:bien |déjà |aussi )?(?:"
    r"envoy|réserv|pay|command|annul|supprim|transf[ée]r|dépos|soumis|programm|install|"
    r"déploy|résili|pris (?:rendez-vous|le rendez-vous|ta place|les billets)|acheté|vir[ée])",
    re.IGNORECASE,
)
"""Orion says it brought about an effect in the world: what needs a confirmed action."""
_TYPOGRAPHY = str.maketrans({chr(0x2019): "'"})


@dataclass(frozen=True)
class Check:
    kind: str
    ok: bool
    reason: str = ""

    def journaled(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "ok": self.ok,
            **({"reason": self.reason} if self.reason else {}),
        }


def outcome_check(draft: str, known: str, results: Iterable[str]) -> Check:
    """`known`: what Orion was told (its notice); `results`: the tool results of its turn."""
    text = draft.translate(_TYPOGRAPHY)
    if _DID.search(text) is None or not claims_a_state(text):
        return Check("outcome", True)
    seen = outcomes([*results, *known.splitlines()])
    if any(outcome is Outcome.CONFIRMED for outcome, _ in seen.values()):
        return Check("outcome", True)
    return Check(
        "outcome",
        False,
        "il dit avoir fait quelque chose (réservé, envoyé, payé…) sans action confirmée : "
        "aucune preuve (identifiant, confirmation) n'a été vue",
    )
