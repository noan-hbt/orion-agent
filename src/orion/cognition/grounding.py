"""The proof before the words: a reply that states how things stand is checked first.

Seen for real, each time said with confidence and false: « oui, c'est cette rubrique,
vérifiée » (INPI, 05/11, the mail's path never followed), « connexion Nibelis rejetée par le
coffre » (05/11, it was not), « pas moi » about an access to Cyclades without reading the logs
(31/10). The model reports a state (done, sent, checked, refused, nothing there) from what it
expects, not from what a tool showed.

Before his reply goes, a check in code, free and only as good as its patterns
(`claims_a_state`): a state claimed in a turn where no tool ran is sent back once. A different
model then reads every reply against what the tools returned, not only those a pattern would
pick, but in the background, once the conversation settles (`orion.cognition.review`, D253):
before each reply it cost a call and seconds on every message.

A tool that ran is not a success either (D258): an action of this turn left attempted, pending
or uncertain (`orion.tools.outcome`) may not be reported done. The same check, in code, sends
such a reply back once, unless it already says the outcome is not verified.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from orion.providers.base import Message
from orion.tools.outcome import MEANING, UNSETTLED, Outcome, outcomes

_PARTICIPLE = (
    r"(?:envoy|vérifi|confirm|rejet|refus|valid|annul|réserv|supprim|transf[ée]r|pay|command|"
    r"dépos|soumis|enregistr|programm|install|déploy|bloqu|accept|reç|corrig|répar)"
)
_STATE = re.compile(
    r"|".join(
        (
            # « c'est fait », « c'est envoyé », « c'est réglé »; not « dis-moi quand c'est fait »
            r"(?<!quand )(?<!si )(?<!que )\bc'est (?:bien |déjà |tout )?(?:fait|réglé|"
            r"en place|parti|passé|" + _PARTICIPLE + r")",
            # « j'ai envoyé », « j'ai bien vérifié », « je l'ai annulé »
            r"\bj(?:e l|e les|e t|e lui|e leur)?'ai (?:bien |déjà |tout |aussi )?(?:"
            + _PARTICIPLE
            + r"|fait|mis à jour|modifi|ajout|créé|retir|contrôl|lu|relu|regardé|trouvé|"
            r"testé|lancé|redémarr|réglé|changé|vidé|archiv|classé)",
            # « envoyé ✅ », « rejetée par le coffre », « a été validé », « ont bien été reçus »
            r"\b(?:a|ont|est|sont) (?:bien |déjà )?(?:été )?" + _PARTICIPLE + r"\w*\b",
            r"\b" + _PARTICIPLE + r"\w* (?:par|avec succès|sans erreur)\b",
            # « …, vérifiée : », « Envoyé ✅ », « Réservé. »
            r"(?:^|[,:;])\s*" + _PARTICIPLE + r"\w*\s*(?:[.!,:✅✔]|$)",
            # « aucun mail », « rien reçu », « il n'y a rien »
            r"\baucune? (?:mail|message|réponse|résultat|trace|erreur|accès|connexion|"
            r"paiement|virement|facture|rendez-vous|événement|fichier|document|changement)\b",
            r"\brien (?:reçu|trouvé|vu|de nouveau|dans)\b",
            r"\bil n'y a (?:rien|aucun|pas de)\b",
            # « ce n'est pas moi », « ça a marché », « ça n'a pas fonctionné »
            r"\b(?:ce n'est |c'était |c'est )?pas moi\b",
            r"\bça (?:a|n'a pas) (?:marché|fonctionné|échoué|réussi|abouti)\b",
            r"\b(?:réussi|échoué|terminé|abouti)\s*[.!✅✔]",
        )
    ),
    re.IGNORECASE,
)

EVIDENCE_CHARS = 16_000
"""What the verifier reads of the tool results in the conversation, newest kept."""
WORDS_CHARS = 4_000
"""What it reads of the owner's own words, newest kept: what the claim answers."""

UNCHECKED_NOTE = (
    "[Orion · Ta réponse n'est pas encore partie. Elle affirme un état (fait, envoyé, vérifié, "
    "refusé, rien trouvé…) alors qu'aucun outil n'a tourné pendant ce tour. Si un résultat plus "
    "haut dans la conversation le montre, garde-la ; sinon vérifie avec l'outil qui le montre, "
    "ou dis-lui que tu ne l'as pas vérifié. Puis écris ta réponse finale, entière.]"
)

UNSETTLED_NOTE = (
    "[Orion · Ta réponse n'est pas encore partie. Elle dit où en est une action, mais ce que tu "
    "as lancé pendant ce tour n'a pas d'issue confirmée :\n{actions}\nUn retour de commande ne "
    "prouve pas le résultat, et la preuve d'une autre action ne vaut pas pour celle-ci. Si ce "
    "que tu as lu après l'action le montre, dis ce que tu y as vu ; sinon vérifie avec l'outil "
    "qui lit l'état réel (la page, la boîte d'envoi, `workflows_status`…), d'abord pour une "
    "issue incertaine, ou dis-lui exactement où ça en est (« accepté par le "
    "serveur, remise non vérifiée », « formulaire envoyé, pas de confirmation vue »). Puis écris "
    "ta réponse finale, entière.]"
)

_DELIVERED = re.compile(
    r"\b(?:reçue?s?|arrivée?s?|livrée?s?|délivrée?s?|remise?s?|distribuée?s?)\b|"
    r"\bdans (?:sa|leur|ta) boîte\b|\b(?:il|elle) l'a\b|\b(?:ils|elles) l'ont\b",
    re.IGNORECASE,
)
"""Said of a send: it reached them. A send only pending may be said sent, not this."""
_HEDGED = re.compile(
    r"\b(?:non|pas|sans) (?:encore )?(?:vérifi|confirm|prouv)|\bpas de (?:confirmation|preuve)|"
    r"\ben (?:attente|file)\b|\bincertain|\bje ne sais pas (?:si|encore)|\bremise non",
    re.IGNORECASE,
)
"""A reply that already says the outcome is not known: it may go as it is."""


_SENTENCE = re.compile(r"[^.!?\n]+[.!?]*")
_TYPOGRAPHY = str.maketrans({chr(0x2019): "'", chr(0x2014): ",", chr(0x2013): ","})
"""Curly apostrophes and dashes, as the model writes them, read as the patterns expect."""


def claims_a_state(text: str) -> bool:
    """The reply reports how something stands: what must rest on a tool's result. A question
    (« c'est réglé ? ») asks, it does not report."""
    return any(
        _STATE.search(sentence) is not None
        for sentence in _SENTENCE.findall(text.translate(_TYPOGRAPHY))
        if not sentence.rstrip().endswith("?")
    )


def _text(message: Message) -> str:
    content = message.get("content")
    return content if isinstance(content, str) else ""


def evidence(messages: Sequence[Message]) -> tuple[str, str]:
    """(what the tools returned, what he said) in these messages, each newest kept."""
    results: list[str] = []
    words: list[str] = []
    for message in messages:
        text = _text(message).strip()
        if not text:
            continue
        if message.get("role") == "tool":
            results.append(text)
        elif message.get("role") == "user" and not text.startswith("[Orion"):
            words.append(f"Lui : {text}")
    return "\n---\n".join(results)[-EVIDENCE_CHARS:], "\n".join(words)[-WORDS_CHARS:]


async def ground_check(
    text: str,
    called: Sequence[str],
    messages: Sequence[Message],
) -> str | None:
    """The note that sends Orion back to check, or None when the reply may go. `called`: the
    tools called this turn; `messages`: what this turn added."""
    claims = claims_a_state(text)
    if not called:
        return UNCHECKED_NOTE if claims else None
    if _HEDGED.search(text.translate(_TYPOGRAPHY)):
        return None
    delivered = _says(_DELIVERED, text)
    # Pending (a mail the server took) may be said sent, not received; the rest, nothing done.
    wrong = {
        action: (outcome, proof)
        for action, (outcome, proof) in unsettled(messages).items()
        if delivered or (claims and outcome is not Outcome.PENDING)
    }
    if not wrong:
        return None
    actions = "\n".join(
        f"- {action} : {MEANING[outcome]}" + (f" ({proof})" if proof else "")
        for action, (outcome, proof) in wrong.items()
    )
    return UNSETTLED_NOTE.format(actions=actions)


def _says(pattern: re.Pattern[str], text: str) -> bool:
    return any(
        pattern.search(sentence) is not None
        for sentence in _SENTENCE.findall(text.translate(_TYPOGRAPHY))
        if not sentence.rstrip().endswith("?")
    )


def unsettled(messages: Sequence[Message]) -> dict[str, tuple[Outcome, str]]:
    """The actions in these tool results whose latest outcome does not show they happened."""
    found = outcomes(_text(m) for m in messages if m.get("role") == "tool")
    return {action: seen for action, seen in found.items() if seen[0] in UNSETTLED}
