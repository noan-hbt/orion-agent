"""Structured pieces in Orion's messages (D126): a few tags the app draws, the others read.

Orion writes plain text, and where a choice or a record helps, one of three tags:

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

Tags rather than a tool: no extra model call, nothing that changes the tool registry (cache),
and a message stays one message, journaled as sent. The app draws the pieces; every other
channel gets `plain()`, which reads well as text. Anything malformed stays text, as written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

TAG = re.compile(r"<(choices|card|done)((?:\s+[a-z]+(?:=\"[^\"]*\")?)*)\s*>(.*?)</\1>", re.S)
ATTRIBUTE = re.compile(r"([a-z]+)(?:=\"([^\"]*)\")?")
MAX_OPTIONS = 12
MAX_LABEL = 120


@dataclass(frozen=True)
class Piece:
    kind: str
    """`text`, `choices`, `card` or `done`."""
    data: dict[str, Any]


def _attributes(raw: str) -> dict[str, str]:
    return {m.group(1): (m.group(2) or "").strip() for m in ATTRIBUTE.finditer(raw)}


def _options(body: str) -> list[str]:
    lines = [line.strip() for line in body.strip().splitlines()]
    options = [re.sub(r"^(?:[-*•]|\d+[.)])\s*", "", line)[:MAX_LABEL] for line in lines if line]
    return [o for o in options if o][:MAX_OPTIONS]


def _piece(kind: str, attributes: dict[str, str], body: str) -> Piece | None:
    if kind == "choices":
        options = _options(body)
        if not options:
            return None
        multiple = "multiple" in attributes
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
    for match in TAG.finditer(text):
        piece = _piece(match.group(1), _attributes(match.group(2)), match.group(3))
        if piece is None:
            continue
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
    return f"✓ {data['text']}"


def plain(text: str) -> str:
    """The message for a channel that draws nothing: the same content, as text."""
    if "<" not in text:
        return text
    return "\n\n".join(_plain(p) for p in parse(text))


def public(text: str) -> list[dict[str, Any]]:
    """The pieces as the app reads them; an empty list when the message is only text."""
    pieces = parse(text)
    if all(p.kind == "text" for p in pieces):
        return []
    return [{"type": p.kind, **p.data} for p in pieces]
