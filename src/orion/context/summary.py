"""What a rebase leaves behind, carried forward as a short summary.

When the window jumps forward (a thread rebase), the turns before the new base leave the model's
view. Memory keeps the facts, but not the thread of the conversation: what was being done, what
was decided, what was left open. Claude Code compacts the same way: it summarizes the history it
drops rather than forgetting it. Here the summary goes into the new user message, which is
journaled verbatim, so the cache contract holds (nothing earlier is rewritten).

Only trusted turns are summarized: a summary of a page or a mail would carry its instructions
past the rebase, which is exactly what rebasing an untrusted window is meant to stop.
"""

from __future__ import annotations

from typing import Any

import structlog

from orion.providers.base import Message, ModelProvider, ModelRequest, ProviderError

log = structlog.get_logger(__name__)

MAX_INPUT_CHARS = 60_000
TOOL_CHARS = 300
SUMMARY_TOKENS = 700

PROMPT = """Tu résumes le début d'une conversation entre une personne et son assistant, Orion,
qui va sortir de la fenêtre de l'assistant. Écris en français, à la deuxième personne pour
Orion (« Tu as… »), en 10 lignes au plus :
- ce qui était en cours et où ça en est ;
- ce qui a été décidé ou promis, avec les chiffres, dates et noms utiles ;
- ce qui reste ouvert ou en attente d'une réponse.
Rien d'autre : pas de préambule, pas de faits durables sur la personne (sa mémoire les garde)."""


def _transcript(messages: list[Message]) -> str:
    lines: list[str] = []
    for message in messages:
        role = message.get("role")
        content = str(message.get("content") or "")
        if role == "tool":
            lines.append(f"[outil] {content[:TOOL_CHARS]}")
        elif role == "assistant":
            asked: list[dict[str, Any]] = message.get("tool_calls") or []
            calls = [str(c["function"]["name"]) for c in asked]
            if content:
                lines.append(f"Orion : {content}")
            if calls:
                lines.append(f"[Orion appelle {', '.join(calls)}]")
        elif role == "user":
            lines.append(f"Personne : {content}")
    return "\n".join(lines)[-MAX_INPUT_CHARS:]


async def summarize(
    provider: ModelProvider, model: str, messages: list[Message], session_id: str
) -> str | None:
    """A few lines on what `messages` were about, or None if there is nothing or it failed."""
    transcript = _transcript(messages)
    if not transcript.strip():
        return None
    request = ModelRequest(
        model=model,
        messages=[
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": transcript},
        ],
        session_id=session_id,
        max_tokens=SUMMARY_TOKENS,
    )
    try:
        response = await provider.complete(request)
    except ProviderError:
        log.warning("thread.summary_failed", exc_info=True)
        return None
    text = (response.text or "").strip()
    return text or None


def sides(messages: list[Message]) -> tuple[str, str]:
    """What the owner said and what Orion said (with a glimpse of its tools), for the memory
    extractor's last look at turns about to leave the window."""
    owner: list[str] = []
    orion: list[str] = []
    for message in messages:
        role = message.get("role")
        content = str(message.get("content") or "")
        if role == "user" and content:
            owner.append(content)
        elif role == "assistant" and content:
            orion.append(content)
        elif role == "tool" and content:
            orion.append(f"[outil] {content[:TOOL_CHARS]}")
    return "\n".join(owner)[-MAX_INPUT_CHARS:], "\n".join(orion)[-MAX_INPUT_CHARS:]


def summary_block(text: str) -> str:
    return f"<plus-tot-dans-la-conversation>\n{text}\n</plus-tot-dans-la-conversation>"
