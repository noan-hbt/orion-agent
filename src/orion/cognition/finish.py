"""Before handing back: a reply that is not done yet gets one more step (D118, D144).

Seen for real, each time with the model judging its own work as finished:

- an offer at the end (« je peux te chercher ça si tu veux ? ») when the search was his to do;
- a promise of a follow-up (« je te tiens au courant », « je regarde et je reviens vers toi »)
  with nothing scheduled to keep it: once the reply is sent, nothing ever comes;
- assistant filler (« Bien sûr ! », « N'hésite pas si… ») and titles in a text message.

The check is in code: when the final reply (not sent yet) shows one of these, Orion is told so
once, and either does what is missing first or writes the reply again. Only the end of the
reply is read for offers and promises: one in passing, earlier on, is not a stop.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

TAIL_CHARS = 280
"""How much of the end of the reply is read."""

_OFFER = re.compile(
    r"|".join(
        (
            r"\b(?:tu veux|veux-tu|tu voudrais|voudrais-tu|souhaites-tu|tu souhaites|ça te dit)"
            r" (?:que je|qu'on|que l'on)\b",
            r"\bsi tu (?:le )?(?:veux|souhaites|préfères)\b(?!,? mon avis)",
            r"\bdis-moi si tu (?:veux|souhaites)\b",
            r"\bje (?:peux|pourrais) (?:aussi |te |t'|le |la |les |en |m'en |y )*[\wéèê'-]+"
            r"[^.?!\n]*\?",
            r"\bje (?:te |t')?(?:le |la |les |en )?(?:cherche|regarde|compare|prépare|lance|"
            r"trouve|vérifie|réserve|fais)\b[^.?!\n]*\?",
            r"\bje m'en occupe\s*\?",
            r"\b(?:on y va|on lance|je lance|j'y vais|je continue)\s*\?",
            r"\b(?:envoie|écris|dis|tape|réponds)(?:-moi)?\s+[«\"'“ ]*"
            r"(?:continue|go|ok|la suite)\b",
        )
    ),
    re.IGNORECASE,
)

_PROMISE = re.compile(
    r"|".join(
        (
            r"\bje (?:te )?(?:tiens|tiendrai) au courant\b",
            r"\bje (?:reviens|reviendrai|repasse) vers toi\b",
            r"\bje te (?:redis|redirai|recontacte|réécris|ferai signe|préviens dès|préviens quand)",
            r"\bje (?:te )?(?:fais|ferai) (?:un retour|signe)\b",
            r"\bje vais (?:y )?(?:regarder|chercher|voir|vérifier|creuser|m'en occuper|"
            r"me renseigner|surveiller)\b",
            r"\bje (?:garde|garderai) (?:un )?(?:œil|oeil)\b",
            r"\bje m'en occupe\b(?!\s*\?)",
            r"\bje (?:te )?(?:programme|programmerai|mets|mettrai|cale|calerai) un rappel\b",
            r"\bje te (?:rappelle|rappellerai|relance|relancerai) (?:de|d'|avant|quand|le|la)\b",
        )
    ),
    re.IGNORECASE,
)

KEEPS_A_PROMISE = (
    "later",
    "remind_me",
    "task",
    "mission",
    "think_later",
    "follow_up",
    "awaiting",
    "commitments",
    "departure",
    "relations",
    "booking",
)
"""Tools that schedule what comes next: a promise made beside one of them is kept."""

_FILLER_START = re.compile(
    r"^\W*(?:bien sûr|absolument|excellente question|bonne question|avec plaisir|voici)\b"
    r"[^\n]{0,40}?[!:,.]",
    re.IGNORECASE,
)
_FILLER_END = re.compile(
    r"\bn'hésite pas\b|\bje reste (?:à ta |)disposition\b|"
    r"\bsi tu as (?:d'autres |des |la moindre )?questions?\b|"
    r"\bj'espère que (?:ça|cela) (?:t'aide|aide)\b",
    re.IGNORECASE,
)
_HEADING = re.compile(r"^#{1,4} \S", re.MULTILINE)
SHORT_REPLY = 1500
"""Below this, a reply is a message, not a document: no titles."""

OFFER_NOTE = (
    "Elle finit en lui proposant de faire quelque chose. Si tu peux le faire maintenant, sans "
    "risque, fais-le d'abord, puis réponds avec ce que tu as fait. Si c'est un choix qui "
    "n'appartient qu'à lui, garde-le."
)
PROMISE_NOTE = (
    "Elle promet une suite (le tenir au courant, revenir vers lui, regarder) que rien ne "
    "garantit : une fois ta réponse partie, tu ne reprends la parole que par un rappel, une "
    "tâche, une mission ou un suivi. Fais-le maintenant si c'est court ; sinon programme la "
    "suite avec l'outil qui convient ; ou retire la promesse."
)
FILLER_NOTE = (
    "Elle a des formules d'assistant (« Bien sûr ! », « Voici… », « N'hésite pas… ») ou des "
    "titres, qu'un ami n'écrit pas dans un message. Garde le fond, enlève-les."
)


_LAST_OFFER = re.compile(
    r"(?:^|[.!?…]\s+)(?:je (?:peux|pourrais)|si tu veux,? je)\b[^.!?…]*[.!?…]?\s*$",
    re.IGNORECASE,
)
"""The last sentence offers to do something, question mark or not (« Je peux aussi te
programmer un rappel. »): seen for real, it went out as the end of the reply."""


def ends_on_offer(text: str) -> bool:
    tail = text.strip()[-TAIL_CHARS:]
    return _OFFER.search(tail) is not None or _LAST_OFFER.search(tail) is not None


def promises_later(text: str) -> bool:
    return _PROMISE.search(text.strip()[-TAIL_CHARS:]) is not None


def sounds_like_an_assistant(text: str) -> bool:
    text = text.strip()
    if _FILLER_START.search(text[:80]) or _FILLER_END.search(text[-TAIL_CHARS:]):
        return True
    return len(text) < SHORT_REPLY and _HEADING.search(text) is not None


DEFERS = ("mission:start", "task:start", "think_later")
"""Putting a request off: a mission, a background task, a question kept for the night."""
LOOKS = (
    "web_search",
    "web_read",
    "browser",
    "recall",
    "attachment_look",
    "conversations_search",
    "library",
    "papers",
    "mail",
    "agenda",
    "consult_expert",
    "think_harder",
    "people",
    "skill",
)
"""Doing something for the request now: looking it up, reading, asking the stronger model."""
NOW_NOTE = (
    "Tu as remis sa demande à plus tard (mission, tâche ou réflexion) sans rien chercher "
    "maintenant. Ce qui se trouve en quelques recherches, cherche-le tout de suite et réponds "
    "avec ce que tu as trouvé ; ce que tu as lancé ne sert qu'à la suite (surveiller, "
    "approfondir, revenir plus tard)."
)


def defers_without_trying(called: Sequence[str]) -> bool:
    """D151: seen for real, a mission « every 6 h » for what deserved an answer now."""
    return any(c.startswith(DEFERS) for c in called) and not any(
        c.startswith(LOOKS) for c in called
    )


def finish_check(text: str, called: Sequence[str] = ()) -> str | None:
    """The note that sends Orion back to work, or None when the reply may go. `called`: the
    tools called during the turn (`tool`, or `tool:action` for a tool with actions)."""
    notes: list[str] = []
    if defers_without_trying(called):
        notes.append(NOW_NOTE)
    if ends_on_offer(text):
        notes.append(OFFER_NOTE)
    elif promises_later(text) and not any(name.startswith(KEEPS_A_PROMISE) for name in called):
        notes.append(PROMISE_NOTE)
    if sounds_like_an_assistant(text):
        notes.append(FILLER_NOTE)
    if not notes:
        return None
    return (
        "[Orion · Ta réponse n'est pas encore partie. "
        + " ".join(notes)
        + " Puis écris ta réponse finale, entière.]"
    )
