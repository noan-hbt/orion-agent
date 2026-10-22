"""What Orion did away from the owner's conversation since they last spoke (D109).

The owner's conversation shows what was said in it, not what Orion did elsewhere meanwhile: a
mission's pass, a file sent from it, a contact answered, another Orion's message, an approval
asked from another conversation. So « tu as bossé pendant mon absence ? » once got « non, rien »
while a mission had run and sent a picture. When the owner writes again, the journal after the
conversation's last message is read, and what happened elsewhere is attached to their message
as a dated list: facts from the record, nothing for the model to guess.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from orion.core.events import Event
from orion.core.hlc import Timestamp

READ_AT_MOST = 5000
"""Events read after the conversation's last message; a long absence shows its start."""
SHOWN = 15
QUOTE = 160


def _quote(text: object) -> str:
    flat = " ".join(str(text or "").split())
    return f"« {flat[:QUOTE]}{'…' if len(flat) > QUOTE else ''} »"


def _line(event: Event, requested: dict[str, str]) -> str | None:
    p = event.payload
    match event.kind:
        case "mission.ran":
            said = str(p.get("said") or "")
            return f"passage de mission ({p.get('mission_id')}) : " + (
                f"rapport {_quote(said)}" if said else "rien de neuf"
            )
        case "secretary.replied":
            return f"j'ai répondu à {p.get('contact', 'un contact')} : {_quote(p.get('reply'))}"
        case "federation.chat":
            line = f"l'Orion de {p.get('label') or p.get('peer')} m'a écrit {_quote(p.get('them'))}"
            return line + (f", j'ai répondu {_quote(p.get('reply'))}" if p.get("reply") else "")
        case "federation.chat.sent":
            peer = p.get("label") or p.get("peer")
            return f"j'ai écrit à l'Orion de {peer} : {_quote(p.get('text'))}"
        case "meeting.agreed":
            return f"rendez-vous calé avec {p.get('peer')} : {p.get('start', '?')}"
        case "meeting.declined" | "meeting.cancelled":
            return f"rendez-vous annulé ou refusé avec {p.get('peer', '?')}"
        case "approval.decided":
            tool = requested.get(str(p.get("approval_id")), "")
            if not tool:
                return None
            return f"accord demandé pour {tool} : {'accepté' if p.get('approved') else 'refusé'}"
        case "thinking.thought":
            return f"j'ai réfléchi la nuit à {_quote(p.get('question'))}"
        case "outbox.failed":
            return f"un envoi a échoué : {_quote(p.get('error'))}"
        case _:
            return None


def meanwhile(events: Sequence[Event], seen_correlations: set[str], zone: ZoneInfo) -> str | None:
    """The block for the owner's message, or None when nothing happened elsewhere.

    `events` follow the conversation's last message; `seen_correlations` are the turns whose
    words are already in the conversation (what they sent is not news)."""
    replied = {e.correlation_id for e in events if e.kind == "secretary.replied"}
    requested = {
        str(e.payload.get("approval_id")): str(e.payload.get("tool"))
        for e in events
        if e.kind == "approval.requested"
    }
    lines: list[str] = []
    for event in events:
        if event.redacted:
            continue
        if event.kind == "outbox.queued":
            if event.correlation_id in seen_correlations or event.correlation_id in replied:
                continue
            what = "un fichier " if event.payload.get("media") else ""
            line = f"je t'ai envoyé {what}{_quote(event.payload.get('text'))}"
        else:
            line = _line(event, requested)
        if line is None:
            continue
        at = datetime.fromtimestamp(Timestamp.decode(event.hlc).wall_ms / 1000, UTC)
        lines.append(f"- {at.astimezone(zone):%d/%m %H:%M} · {line}")
    if not lines:
        return None
    more = len(lines) - SHOWN
    shown = lines[:SHOWN] + ([f"- … et {more} autre(s)"] if more > 0 else [])
    return (
        "<pendant_ce_temps>\nCe que tu as fait ou reçu hors de cette conversation depuis votre "
        "dernier échange (d'après ton journal) :\n" + "\n".join(shown) + "\n</pendant_ce_temps>"
    )
