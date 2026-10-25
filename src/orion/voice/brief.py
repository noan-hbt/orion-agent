"""The voice brief: what Inkbox Voice AI needs to sound like Orion and know what Orion knows.

Inkbox's hosted voice drives the call itself and cannot call Orion's tools mid-call. So Orion
keeps it briefed: a steering prompt rebuilt from memory (playbook, profile, open commitments,
upcoming reminders, the last exchanges), pushed to Inkbox whenever it changes. After the call,
the transcript comes back into the owner's thread, where Orion does what was promised.

Only content at SYSTEM trust or above goes into the brief, and the line only accepts the
owner's number, so the brief is only ever spoken to the owner.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime

from orion.context.recent import recent_lines
from orion.core.events import Trust
from orion.initiative.timers import Timer
from orion.initiative.tools import describe_moment
from orion.memory.model import Category
from orion.memory.store import Commitment, Fact, Rule
from orion.providers.base import Message

MAX_CHARS = 6000
RECENT_MESSAGES = 8
_WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

PERSONA = """Tu es Orion, l'assistant personnel de ton interlocuteur, au téléphone. C'est toi qui
lui écris aussi par iMessage : même mémoire, même personnalité.

Au téléphone : tu tutoies, phrases courtes et naturelles, de l'humour et une pointe de sarcasme
quand ça s'y prête, jamais de listes ni d'identifiants lus à voix haute. Tu vas droit au but.

Pendant l'appel, tu ne peux ni enregistrer, ni programmer de rappel, ni envoyer de message. Quand
il te demande quelque chose de ce genre, dis simplement que c'est noté : la conversation est
relue juste après l'appel par toi-même, côté messages, et ce qui a été convenu sera fait et
confirmé par message. Ne promets rien d'autre que ça.

Si tu as appelé de toi-même, commence par la raison de l'appel.
Ce qui suit est privé : tu ne le révèles à personne d'autre que lui."""


def _who(name: str) -> str:
    return {"owner": "lui", "orion": "toi (Orion)"}.get(name, name)


def build_brief(
    now: datetime,
    timezone: str,
    rules: Sequence[Rule],
    facts: Sequence[Fact],
    commitments: Sequence[Commitment],
    timers: Sequence[Timer],
    recent: Sequence[Message],
) -> str:
    trusted = Trust.SYSTEM
    sections: list[tuple[str, list[str]]] = [
        (
            "Sa façon de travailler avec toi (à respecter)",
            [f"- {r.rule}" for r in rules if r.trust >= trusted],
        ),
        (
            "Ce que tu sais de lui",
            [
                f"- {f.statement}"
                for f in facts
                if f.trust >= trusted and f.category in {Category.PROFILE, Category.PREFERENCE}
            ],
        ),
        (
            "Engagements en cours",
            [
                f"- {_who(c.debtor)} → {_who(c.creditor)} : {c.what}"
                + (f" (pour le {c.due})" if c.due else "")
                for c in commitments
                if c.trust >= trusted
            ],
        ),
        (
            "Rappels programmés",
            [
                f"- {describe_moment(t.due_at, timezone)} : {t.text}"
                for t in timers[:10]
                if t.trust >= trusted
            ],
        ),
        (
            "Vos derniers échanges, du plus ancien au plus récent",
            [f"- {line}" for line in recent_lines(recent, RECENT_MESSAGES)],
        ),
    ]
    out = [PERSONA, "", f"Nous sommes {_WEEKDAYS[now.weekday()]} {now:%Y-%m-%d}."]
    for title, lines in sections:
        if lines:
            out += ["", f"{title} :", *lines]
    brief = "\n".join(out)
    return brief if len(brief) <= MAX_CHARS else brief[: MAX_CHARS - 1] + "…"


def fingerprint(brief: str) -> str:
    return hashlib.sha256(brief.encode()).hexdigest()
