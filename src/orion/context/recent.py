"""The last exchanges of a thread as plain lines, for readers outside the conversation (the
voice brief, the verifier, the night): memory blocks, rebase summaries and context lines
removed, initiative notices summarised."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

from orion.context.threads import MESSAGE_KIND
from orion.core.events import Trust
from orion.core.journal import EventStore
from orion.providers.base import Message

_BLOCK = re.compile(
    r"<(memoire|souvenirs|manuel|consignes|plus-tot-dans-la-conversation)>.*?</\1>\s*", re.DOTALL
)
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


async def conversation_since(
    journal: EventStore, thread_id: str, after_seq: int, floor: Trust
) -> tuple[list[tuple[datetime | None, str]], int]:
    """The thread's readable messages journaled after `after_seq`, with their time, keeping only
    those at `floor` trust or above (what was read from elsewhere stays out), and the last seq
    read. Pages and mails themselves (tool results) never show here; the night reads them all
    (`day_since`, D201)."""
    said: list[tuple[datetime | None, str]] = []
    seq = after_seq
    while True:
        page = await journal.read(seq, 1000)
        if not page:
            return said, seq
        for event in page:
            seq = event.seq
            if (
                event.kind != MESSAGE_KIND
                or event.redacted
                or event.payload.get("thread_id") != thread_id
            ):
                continue
            if event.trust < floor:
                continue
            line = clean(event.payload["message"])
            if line:
                at = event.payload.get("at")
                said.append((datetime.fromisoformat(str(at)) if at else None, line))


TOOL_RESULT_CHARS = 600
"""What the night keeps of one tool result (a page, a mail, a search): its gist, not all of it."""


def _where(thread_id: str) -> str:
    """How a thread other than the owner's is named in the night's reading of the day."""
    for prefix, name in (
        ("veille", "ta veille"),
        ("task:", "une tâche de fond"),
        ("mission:", "une mission"),
        ("contact:", "un contact"),
    ):
        if thread_id.startswith(prefix):
            return name
    return thread_id


ELSEWHERE = {
    Trust.UNKNOWN: "on ne sait où",
    Trust.WEB: "le web",
    Trust.THIRD_PARTY: "un mail ou un tiers",
    Trust.TRUSTED_PEER: "un autre Orion",
    Trust.OWNER_INSTANCE: "un document",
}


def elsewhere(trust: Trust) -> str:
    """Where content came from when it is not his nor Orion's own (D201); empty if it is."""
    if trust >= Trust.SYSTEM:
        return ""
    return f"venu d'ailleurs : {ELSEWHERE.get(trust, 'on ne sait où')}"


def _outside(trust: Trust) -> str:
    origin = elsewhere(trust)
    return f" [{origin}]" if origin else ""


async def day_since(
    journal: EventStore, owner_thread: str, after_seq: int, skip: frozenset[str]
) -> tuple[list[tuple[datetime | None, str]], int]:
    """Everything said and read since `after_seq`, every thread but `skip` (D201): the owner's
    conversation, Orion's watch, tasks, missions and contacts, and what tools returned (pages,
    mails, searches), each clipped, with where it was and whether it came from elsewhere. The
    owner chose to let his night see it all, knowing what a page could plant."""
    said: list[tuple[datetime | None, str]] = []
    seq = after_seq
    while True:
        page = await journal.read(seq, 1000)
        if not page:
            return said, seq
        for event in page:
            seq = event.seq
            thread = event.payload.get("thread_id") if event.kind == MESSAGE_KIND else None
            if not isinstance(thread, str) or event.redacted or thread in skip:
                continue
            message: Message = event.payload["message"]
            content = message.get("content")
            role = message.get("role")
            if role == "tool" or (role == "user" and thread != owner_thread):
                # A tool's result, or the brief Orion gave himself (a watch, a task, a mission).
                what = "résultat d'outil" if role == "tool" else "consigne"
                line = (
                    f"({what} : {clip(content, TOOL_RESULT_CHARS)})"
                    if isinstance(content, str) and content.strip()
                    else None
                )
            else:
                line = clean(message)
            if not line:
                continue
            where = "" if thread == owner_thread else f"[{_where(thread)}] "
            at = event.payload.get("at")
            said.append(
                (
                    datetime.fromisoformat(str(at)) if at else None,
                    f"{where}{line}{_outside(event.trust)}",
                )
            )
