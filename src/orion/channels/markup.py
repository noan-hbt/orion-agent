"""Structured pieces in Orion's messages (D126): a few tags the app draws, the others read.

Orion writes plain text, and where a choice or a record helps, one of these tags:

    <choices>                       quick answers: a tap sends the option
    - Plutôt 10 h
    - Parfait
    </choices>

    <choices multiple send="Demander une visite">   options to tick, then one button
    - 680 € · rue Saint-Martin
    - 640 € · av. des Fenots
    </choices>

    <card title="680 € · rue Saint-Martin" subtitle="42 m² · refait" link="https://…">
    Visite possible samedi 10 h
    </card>

    <done undo="Annule le rappel de mardi 9 h">Rappel posé · mardi 9:00 · appeler la banque</done>

and, since D140, records the app draws as iOS does (self-closing, or with a closing tag):

    <event title="Visite T2" when="2026-11-01T10:00" end="2026-11-01T10:30" where="12 rue X"/>
    <mail from="Agence Foncia" subject="Votre dossier">Il manque l'avis d'imposition.</mail>
    <open screen="papers" label="Voir tes papiers"/>
    <progress label="Dossier de location" value="0.6"/>

Tags rather than a tool: no extra model call, nothing that changes the tool registry (cache),
and a message stays one message, journaled as sent. The app draws the pieces; every other
channel gets `plain()`, which reads well as text. Anything malformed stays text, as written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

TAG = re.compile(
    r"<(choices|card|done|event|mail|open|progress)((?:\s+[a-z]+(?:=\"[^\"]*\")?)*)\s*"
    r"(?:/>|>(.*?)</\1>)",
    re.S,
)
ATTRIBUTE = re.compile(r"([a-z]+)(?:=\"([^\"]*)\")?")
MAX_OPTIONS = 5
"""Quick answers: two to five, as app.md tells Orion (D140: the parser and the doc agree)."""
MAX_TICKS = 8
"""Options to tick: a few more, they are a list he goes through."""
MAX_CARDS = 3
MAX_PIECES = 8
"""Pieces drawn in one message; past that, the rest stays text."""
MAX_LABEL = 120
MAX_SNIPPET = 280
SCREENS = {
    "missions": "Voir les missions",
    "planned": "Voir ce qui est prévu",
    "activity": "Voir l'activité",
    "autonomy": "Voir ce que je fais seul",
    "memory": "Voir mes souvenirs",
    "people": "Voir tes proches",
    "papers": "Voir tes papiers",
    "weeks": "Voir tes semaines",
    "commitments": "Voir les engagements",
    "rules": "Voir mes règles",
    "agenda": "Voir l'agenda",
    "mail": "Voir les mails",
    "night": "Voir mes nuits",
    "settings": "Ouvrir les réglages",
}
"""The app screens a message may open (D140); the app maps each to its panel."""


@dataclass(frozen=True)
class Piece:
    kind: str
    """`text`, `choices`, `card`, `done`, `event`, `mail`, `open` or `progress`."""
    data: dict[str, Any]


def _attributes(raw: str) -> dict[str, str]:
    return {m.group(1): (m.group(2) or "").strip() for m in ATTRIBUTE.finditer(raw)}


def _options(body: str, most: int) -> list[str]:
    lines = [line.strip() for line in body.strip().splitlines()]
    options = [re.sub(r"^(?:[-*•]|\d+[.)])\s*", "", line)[:MAX_LABEL] for line in lines if line]
    return [o for o in options if o][:most]


def _moment(raw: str) -> str:
    """An ISO date or date-time as written, or empty when it is not one."""
    try:
        return datetime.fromisoformat(raw).isoformat() if raw else ""
    except ValueError:
        return ""


def _record(kind: str, attributes: dict[str, str], body: str) -> Piece | None:
    """The records of D140: an event, a mail, a screen to open, a progress."""
    if kind == "event":
        title = attributes.get("title", "")[:MAX_LABEL]
        when = _moment(attributes.get("when", ""))
        if not title or not when:
            return None
        return Piece(
            "event",
            {
                "title": title,
                "when": when,
                "all_day": "T" not in attributes.get("when", ""),
                "end": _moment(attributes.get("end", "")),
                "where": attributes.get("where", "")[:MAX_LABEL],
            },
        )
    if kind == "mail":
        sender = attributes.get("from", "")[:MAX_LABEL]
        subject = attributes.get("subject", "")[:MAX_LABEL]
        if not sender and not subject:
            return None
        snippet = " ".join(body.split())[:MAX_SNIPPET]
        return Piece("mail", {"from": sender, "subject": subject, "snippet": snippet})
    if kind == "open":
        screen = attributes.get("screen", "")
        if screen not in SCREENS:
            return None
        label = attributes.get("label", "")[:MAX_LABEL] or SCREENS[screen]
        return Piece("open", {"screen": screen, "label": label})
    label = attributes.get("label", "")[:MAX_LABEL]
    try:
        value = float(attributes.get("value", ""))
    except ValueError:
        return None
    if not label or value != value:  # NaN
        return None
    return Piece("progress", {"label": label, "value": min(1.0, max(0.0, value))})


def _piece(kind: str, attributes: dict[str, str], body: str) -> Piece | None:
    if kind in ("event", "mail", "open", "progress"):
        return _record(kind, attributes, body)
    if kind == "choices":
        multiple = "multiple" in attributes
        options = _options(body, MAX_TICKS if multiple else MAX_OPTIONS)
        if not options:
            return None
        send = attributes.get("send", "")[:MAX_LABEL] or ("Envoyer" if multiple else "")
        return Piece("choices", {"options": options, "multiple": multiple, "send": send})
    if kind == "card":
        title = attributes.get("title", "")[:MAX_LABEL]
        if not title:
            return None
        link = attributes.get("link", "")
        return Piece(
            "card",
            {
                "title": title,
                "subtitle": attributes.get("subtitle", "")[:MAX_LABEL],
                "lines": [line.strip() for line in body.strip().splitlines() if line.strip()],
                "link": link if link.startswith("https://") else "",
            },
        )
    text = " ".join(body.split())
    if not text:
        return None
    return Piece("done", {"text": text, "undo": attributes.get("undo", "")[:MAX_LABEL]})


def parse(text: str) -> list[Piece]:
    """The message as pieces, in order; text between tags stays text."""
    pieces: list[Piece] = []
    at = 0
    drawn = cards = 0
    for match in TAG.finditer(text):
        kind = match.group(1)
        if drawn >= MAX_PIECES or (kind == "card" and cards >= MAX_CARDS):
            continue  # past the limits, the tag stays text, as written
        piece = _piece(kind, _attributes(match.group(2)), match.group(3) or "")
        if piece is None:
            continue
        drawn += 1
        cards += kind == "card"
        before = text[at : match.start()].strip()
        if before:
            pieces.append(Piece("text", {"text": before}))
        pieces.append(piece)
        at = match.end()
    rest = text[at:].strip()
    if rest:
        pieces.append(Piece("text", {"text": rest}))
    return pieces


def _plain(piece: Piece) -> str:
    data = piece.data
    if piece.kind == "text":
        return str(data["text"])
    if piece.kind == "choices":
        return "\n".join(f"- {o}" for o in data["options"])
    if piece.kind == "card":
        head = data["title"] + (f" · {data['subtitle']}" if data["subtitle"] else "")
        return "\n".join([head, *data["lines"], *([data["link"]] if data["link"] else [])])
    if piece.kind == "event":
        when = datetime.fromisoformat(data["when"])
        at = when.strftime("%d/%m") if data["all_day"] else when.strftime("%d/%m %H:%M")
        return " · ".join(x for x in (data["title"], at, data["where"]) if x)
    if piece.kind == "mail":
        subject = f"« {data['subject']} »" if data["subject"] else ""
        head = " · ".join(x for x in (data["from"], subject) if x)
        return "\n".join(x for x in (f"✉ {head}", data["snippet"]) if x)
    if piece.kind == "open":
        return ""  # a screen of the app means nothing elsewhere
    if piece.kind == "progress":
        return f"{data['label']} : {round(data['value'] * 100)} %"
    return f"✓ {data['text']}"


def plain(text: str) -> str:
    """The message for a channel that draws nothing: the same content, as text."""
    if "<" not in text:
        return text
    return "\n\n".join(x for x in (_plain(p) for p in parse(text)) if x)


def public(text: str) -> list[dict[str, Any]]:
    """The pieces as the app reads them; an empty list when the message is only text."""
    pieces = parse(text)
    if all(p.kind == "text" for p in pieces):
        return []
    return [{"type": p.kind, **p.data} for p in pieces]
