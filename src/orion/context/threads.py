"""Conversation threads, projected from the journal.

A thread is the ordered list of provider-format messages exchanged in one conversation, exactly
as they were sent. Because nothing is ever rewritten, each request's message list is a strict
extension of the previous one, and the provider's prefix cache keeps hitting.

When a thread grows past its budget it is *rebased*: the start of the visible window jumps
forward in one step (to a user-message boundary). The cache misses once at that point instead of
on every turn, which a sliding window would cause. Earlier messages stay in the journal.

After a pause long enough for the provider's cache to have expired anyway, old bulky tool
results are *pruned*: the window shows their start and a note instead of the whole page. Like a
rebase it is a journal event, so the projection stays rebuildable and the journal keeps every
message whole; unlike a rebase, it costs no cache, since there was none left to lose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from orion.core.events import Event, Trust
from orion.providers.base import Message

MESSAGE_KIND = "conversation.message"
REBASED_KIND = "thread.rebased"
PRUNED_KIND = "thread.pruned"
PRUNED_KEEP = 600
"""Characters of a pruned tool result still shown."""


def pruned(message: Message) -> Message:
    content = str(message.get("content") or "")
    cut = len(content) - PRUNED_KEEP
    return {
        **message,
        "content": f"{content[:PRUNED_KEEP]}\n[ancien résultat allégé : {cut} caractères retirés]",
    }


@dataclass
class Thread:
    id: str
    messages: list[Message] = field(default_factory=list[Message])
    trusts: list[Trust] = field(default_factory=list[Trust])
    base: int = 0
    last_at: datetime | None = None
    """When the last message was added (the turn's time), if recorded."""
    pruned: set[int] = field(default_factory=set[int])
    """Indexes of tool results shown shortened."""

    def window(self) -> list[Message]:
        return [
            pruned(m) if i in self.pruned else m
            for i, m in enumerate(self.messages[self.base :], start=self.base)
        ]

    def prunable(self, keep_turns: int, min_chars: int) -> list[int]:
        """Bulky tool results in the window, older than the last `keep_turns` user messages."""
        users = [
            i
            for i in range(self.base, len(self.messages))
            if self.messages[i].get("role") == "user"
        ]
        if len(users) <= keep_turns:
            return []
        limit = users[-keep_turns] if keep_turns else len(self.messages)
        return [
            i
            for i in range(self.base, limit)
            if i not in self.pruned
            and self.messages[i].get("role") == "tool"
            and len(str(self.messages[i].get("content") or "")) > min_chars
        ]

    def window_trust(self) -> Trust:
        return min(self.trusts[self.base :], default=Trust.OWNER)


class ThreadProjection:
    name = "threads"

    def __init__(self) -> None:
        self._threads: dict[str, Thread] = {}
        self._checkpoint = 0

    async def checkpoint(self) -> int:
        return self._checkpoint

    async def apply(self, event: Event) -> None:
        if not event.redacted:
            if event.kind == MESSAGE_KIND:
                thread = self._thread(event.payload["thread_id"])
                thread.messages.append(event.payload["message"])
                thread.trusts.append(event.trust)
                if "at" in event.payload:
                    thread.last_at = datetime.fromisoformat(str(event.payload["at"]))
            elif event.kind == REBASED_KIND:
                thread = self._thread(event.payload["thread_id"])
                thread.base = max(thread.base, int(event.payload["base"]))
            elif event.kind == PRUNED_KIND:
                thread = self._thread(event.payload["thread_id"])
                thread.pruned.update(int(i) for i in event.payload["indexes"])
        self._checkpoint = event.seq

    async def reset(self) -> None:
        self._threads = {}
        self._checkpoint = 0

    def get(self, thread_id: str) -> Thread:
        """The thread, or an empty one: reading never adds a thread (a rebuild from the
        journal would not have it, D81)."""
        return self._threads.get(thread_id) or Thread(thread_id)

    def _thread(self, thread_id: str) -> Thread:
        thread = self._threads.get(thread_id)
        if thread is None:
            thread = self._threads[thread_id] = Thread(thread_id)
        return thread
