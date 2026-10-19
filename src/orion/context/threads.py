"""Conversation threads, projected from the journal.

A thread is the ordered list of provider-format messages exchanged in one conversation, exactly
as they were sent. Because nothing is ever rewritten, each request's message list is a strict
extension of the previous one, and the provider's prefix cache keeps hitting.

When a thread grows past its budget it is *rebased*: the start of the visible window jumps
forward in one step (to a user-message boundary). The cache misses once at that point instead of
on every turn, which a sliding window would cause. Earlier messages stay in the journal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from orion.core.events import Event, Trust
from orion.providers.base import Message

MESSAGE_KIND = "conversation.message"
REBASED_KIND = "thread.rebased"


@dataclass
class Thread:
    id: str
    messages: list[Message] = field(default_factory=list[Message])
    trusts: list[Trust] = field(default_factory=list[Trust])
    base: int = 0
    last_at: datetime | None = None
    """When the last message was added (the turn's time), if recorded."""

    def window(self) -> list[Message]:
        return self.messages[self.base :]

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
                thread = self.get(event.payload["thread_id"])
                thread.messages.append(event.payload["message"])
                thread.trusts.append(event.trust)
                if "at" in event.payload:
                    thread.last_at = datetime.fromisoformat(str(event.payload["at"]))
            elif event.kind == REBASED_KIND:
                thread = self.get(event.payload["thread_id"])
                thread.base = max(thread.base, int(event.payload["base"]))
        self._checkpoint = event.seq

    async def reset(self) -> None:
        self._threads = {}
        self._checkpoint = 0

    def get(self, thread_id: str) -> Thread:
        thread = self._threads.get(thread_id)
        if thread is None:
            thread = self._threads[thread_id] = Thread(thread_id)
        return thread
