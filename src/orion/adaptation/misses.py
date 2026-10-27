"""What went wrong for real, read back from the journal (D152).

The behavioural evaluations (`evals`) are written by hand, after the owner tells someone what
went wrong. His own signals already say it, in the journal: a 👎 on one of Orion's messages,
and his words when he corrects Orion (« non, c'est pas ça », « tu t'es trompé », « je t'ai
déjà dit… »). `orion misses` lists them with what he had asked and what Orion answered, and
`--case N` writes one as the start of an evaluation case, to finish by hand and add to
`evals/cases.py`. Nothing is sent anywhere: it is read on his server, by him.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from orion.adaptation.signals import Signal, collect
from orion.core.events import Event

_CORRECTION = re.compile(
    r"|".join(
        (
            r"^\s*(?:non|nan|nope)\b[ ,.!]",
            r"\bc'est (?:pas|faux|n'importe quoi)\b",
            r"\bce n'est pas (?:ça|ce que)\b",
            r"\bt'as (?:rien|pas) compris\b",
            r"\btu (?:t'es|t'étais|es) (?:trompé|planté)\b",
            r"\bje (?:t'ai|te l'ai) (?:déjà )?dit\b",
            r"\bpourquoi (?:tu as|t'as) (?:fait|mis|lancé|répondu)\b",
            r"\btu (?:as|avais) oublié\b",
            r"\bc'est pas ce que (?:je|j'ai)\b",
            r"\bça (?:marche|a marché) pas\b",
        )
    ),
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Miss:
    at: datetime
    how: str
    """« 👎 » or « corrigé »."""
    asked: str
    """What he had asked, as he wrote it ('' when unknown)."""
    answered: str
    """What Orion answered."""
    said: str
    """His correction, or the message he put a 👎 on."""


def corrects(text: str) -> bool:
    return bool(_CORRECTION.search(text))


def misses(events: Sequence[Event], zone: ZoneInfo, now: datetime) -> list[Miss]:
    """Oldest first. `events`: the whole journal."""
    found: list[Miss] = []
    previous: Signal | None = None
    for signal in collect(events, zone, now):
        if signal.kind == "reaction" and signal.text.startswith("👎"):
            answered = signal.text.split(" sur : ", 1)[-1]
            found.append(Miss(signal.at, "👎", "", answered, ""))
        elif signal.kind == "message":
            if previous is not None and previous.reply and corrects(signal.text):
                found.append(Miss(signal.at, "corrigé", previous.text, previous.reply, signal.text))
            previous = signal
    return found


def as_case(miss: Miss, name: str) -> str:
    """The start of an evaluation case: what he said, when; the check is to write."""
    at = miss.at
    origin = f"Raté en vrai le {at:%d/%m/%Y} ({miss.how}) : Orion a répondu « {miss.answered} »"
    if miss.said:
        origin += f", et il a corrigé : « {miss.said} »"
    return "\n".join(
        [
            "    Case(",
            f"        name={name!r},",
            f"        origin={origin + '.'!r},",
            f"        at=paris({at.year}, {at.month}, {at.day}, {at.hour}, {at.minute}),",
            "        facts=OWNER,",
            f"        steps=(Say({(miss.asked or 'TODO : ce qu il avait demandé')!r}),),",
            "        checks=(judged('TODO : la bonne réponse fait-elle … ?'),),",
            "    ),",
        ]
    )
