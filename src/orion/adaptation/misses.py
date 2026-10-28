"""What went wrong for real, as Orion himself judged it (D152, D177).

The behavioural evaluations (`evals`) are written by hand, after the owner tells someone what
went wrong. Orion already sees it: every evening his pass rereads the day, with the 👎 the
owner put on his messages, and notes each exchange he got wrong (`missed`): what was asked,
what he answered, how the owner showed it, and what went wrong. No pattern on his words
decides it: a « non » that starts an ordinary answer is not a correction, and a correction
put otherwise still is. `orion misses` lists them, and `--case N` writes one as the start of
an evaluation case, to finish by hand and add to `evals/cases.py`. Nothing is sent anywhere:
it is read on his server, by him.

    adaptation.missed
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from orion.adaptation.signals import collect
from orion.core.events import Event

MISSED = "adaptation.missed"


@dataclass(frozen=True)
class Miss:
    at: datetime
    """When the exchange took place."""
    asked: str
    """What he had asked ('' when unknown)."""
    answered: str
    """What Orion answered."""
    said: str
    """How he showed it: his correction, or « 👎 »."""
    why: str
    """What went wrong, in Orion's words."""


def misses(events: Sequence[Event], zone: ZoneInfo) -> list[Miss]:
    """Oldest first. `events`: the journal."""
    found: list[Miss] = []
    for e in events:
        if e.kind != MISSED or e.redacted:
            continue
        payload = e.payload
        found.append(
            Miss(
                datetime.fromisoformat(str(payload["at"])).astimezone(zone),
                str(payload.get("asked", "")),
                str(payload.get("answered", "")),
                str(payload.get("said", "")),
                str(payload.get("why", "")),
            )
        )
    return sorted(found, key=lambda m: m.at)


def dislikes(events: Sequence[Event], zone: ZoneInfo, now: datetime, after_seq: int) -> list[str]:
    """His 👎 since `after_seq`, each with the message it was on, for the evening pass.
    `events`: the whole journal (the message may be older)."""
    return [
        f"[{s.at:%Y-%m-%d %H:%M}] {s.text}"
        for s in collect(events, zone, now, after_seq=after_seq)
        if s.kind == "reaction" and s.text.startswith("👎")
    ]


def as_case(miss: Miss, name: str) -> str:
    """The start of an evaluation case: what he said, when; the check is to write."""
    at = miss.at
    origin = f"Raté en vrai le {at:%d/%m/%Y} : Orion a répondu « {miss.answered} »"
    if miss.said:
        origin += f", et il a réagi : « {miss.said} »"
    if miss.why:
        origin += f" ({miss.why})"
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
