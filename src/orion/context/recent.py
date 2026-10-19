"""The last exchanges of a thread as plain lines, for readers outside the conversation (the
voice brief, the verifier): memory blocks and context lines removed, initiative notices
summarised."""

from __future__ import annotations

import re
from collections.abc import Sequence

from orion.providers.base import Message

_BLOCK = re.compile(r"<(memoire|souvenirs|manuel)>.*?</\1>\s*", re.DOTALL)
_INITIATIVE = re.compile(r"<initiative[^>]*>\s*(.*?)\s*</initiative>", re.DOTALL)
_CONTEXT_LINE = re.compile(r"^\[[^\]]*\]\s*")


def clean(message: Message) -> str | None:
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    role = message.get("role")
    if role == "user":
        initiative = _INITIATIVE.search(content)
        if initiative:
            return f"(Orion a pris l'initiative : {initiative[1].strip()})"
        text = _CONTEXT_LINE.sub("", _BLOCK.sub("", content)).strip()
        return f"Lui : {text}" if text else None
    if role == "assistant":
        return f"Orion : {content.strip()}"
    return None


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def recent_lines(messages: Sequence[Message], count: int, limit: int = 300) -> list[str]:
    """One line per readable message among the last `count`, oldest first."""
    return [clip(line, limit) for line in (clean(m) for m in messages[-count:]) if line]
