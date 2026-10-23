"""Before handing back: a reply that ends on an offer gets one more step (D118).

Seen for real: Orion stopping on « je peux te chercher ça si tu veux ? » when the search was
his to do. The model judging its own work tends to call it done, so the check is in code: when
the final reply (not sent yet) ends on an offer to do something, Orion is told so once, and
either does it first or writes the same reply again (a real choice that belongs to the owner,
or something that needs his yes).

Only the end of the reply is read: an offer in passing, earlier on, is not a stop.
"""

from __future__ import annotations

import re

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
        )
    ),
    re.IGNORECASE,
)

NUDGE = (
    "[Orion · Ta réponse n'est pas encore partie. Elle finit en lui proposant de faire quelque "
    "chose. Si tu peux le faire maintenant, sans risque, fais-le d'abord, puis réponds avec ce "
    "que tu as fait. Si c'est un choix qui n'appartient qu'à lui, réécris ta réponse telle "
    "quelle.]"
)


def ends_on_offer(text: str) -> bool:
    return _OFFER.search(text.strip()[-TAIL_CHARS:]) is not None


def offer_check(text: str) -> str | None:
    """The note that sends Orion back to work, or None when the reply may go."""
    return NUDGE if ends_on_offer(text) else None
