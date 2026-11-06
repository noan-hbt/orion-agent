"""What an action led to, as far as Orion can show it (D258, ORION-20).

A tool call that returned is not a success: a mail the SMTP server accepted may never arrive, a
click on « Envoyer » may land on an error page. An acting tool says how far it can vouch for
its action, under a stable `action_id`, with a short proof reference:

- `ATTEMPTED`: the command ran; nothing says what it did (a click, a typed form).
- `PENDING`: handed to someone else who has not settled it yet (a mail accepted by the SMTP
  server: delivery is not provable).
- `CONFIRMED`: a read of the real state shows it (the channel's outbox settled `delivered`,
  the other Orion took the message, the page showed a proof that was not there before, D255).
- `FAILED`: it did not happen.
- `UNCERTAIN`: it may or may not have happened (a step cut by a restart, D257): reconcile first.

The outcome goes first in the tool result, as one line (`marker`), so it survives clipping and
pruning (both keep the start), and is journaled with `tool.executed`. The latest outcome of an
action wins; an outcome of another action proves nothing about this one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum


class Outcome(StrEnum):
    ATTEMPTED = "ATTEMPTED"
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


UNSETTLED = frozenset({Outcome.ATTEMPTED, Outcome.PENDING, Outcome.UNCERTAIN})
"""What may not be called done: it has not been shown to have happened."""

MEANING = {
    Outcome.ATTEMPTED: "tentée, résultat non vu",
    Outcome.PENDING: "en attente, remise non prouvée",
    Outcome.CONFIRMED: "confirmée",
    Outcome.FAILED: "échouée",
    Outcome.UNCERTAIN: "incertaine, peut-être faite ou non",
}

PROOF_CHARS = 120
OUTCOME_LINE = "[issue "
"""How the outcome line starts: pruning keeps that line whole."""
_MARKER = re.compile(
    r"^\[issue (?P<outcome>[A-Z]+) · action (?P<action>\S+)(?: · preuve (?P<proof>[^\]\n]*))?\]"
)


def marker(outcome: Outcome, action_id: str, proof: str = "") -> str:
    """The first line of the tool result: what the action led to, and on what proof."""
    action = "_".join(action_id.replace("]", ")").split()) or "?"
    shown = " ".join(proof.split())[:PROOF_CHARS].replace("]", ")")
    return f"{OUTCOME_LINE}{outcome.value} · action {action}" + (
        f" · preuve {shown}]" if shown else "]"
    )


def outcomes(contents: Iterable[str]) -> dict[str, tuple[Outcome, str]]:
    """action id → (its latest outcome, its proof), from tool results in order."""
    found: dict[str, tuple[Outcome, str]] = {}
    for content in contents:
        match = _MARKER.match(content)
        if match is None or match["outcome"] not in Outcome.__members__:
            continue
        found[match["action"]] = (Outcome(match["outcome"]), match["proof"] or "")
    return found
