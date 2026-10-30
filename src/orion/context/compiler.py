"""Context compiler: builds each request's message list in cache-friendly layers.

Layout (stable first, volatile last):

    1. system: constitution                         — identical for every request
    2. (tools are sent alongside, from a fixed registry, in a fixed order)
    3. thread window: messages exactly as previously sent
    4. the new user message, carrying its own context line (time, channel)

Nothing volatile ever goes in the system message: the current time lives in the user message
that it describes, and stays there verbatim in later turns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from orion.context.threads import Thread
from orion.providers.base import Message

_WEEKDAYS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def estimate_tokens(messages: list[Message]) -> int:
    """Cheap, deliberately pessimistic estimate (≈3 characters per token)."""
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) // 3 + 4 * len(messages)


def render_user_message(
    text: str, when: datetime, channel: str, blocks: list[str] | None = None
) -> Message:
    """The owner's message with its context: a time/channel line, optional memory blocks, then
    the text itself. Rendered once and journaled verbatim, so it never changes afterwards."""
    weekday = _WEEKDAYS_FR[when.weekday()]
    line = f"[{weekday} {when:%Y-%m-%d %H:%M} · {channel}{night_hint(when)}]"
    parts = [line, *(blocks or []), text]
    return {"role": "user", "content": "\n".join(parts)}


NIGHT_ENDS = 5
"""Before this hour, the owner has not slept yet: their « demain » is the day that starts."""


def night_hint(when: datetime) -> str:
    """At 1 a.m. on a Tuesday, « demain » means Tuesday, after the night, not Wednesday (a
    reminder was once set a day late)."""
    if when.hour >= NIGHT_ENDS:
        return ""
    return f" · nuit : son « demain » est sans doute ce {_WEEKDAYS_FR[when.weekday()]} {when.day}"


@dataclass(frozen=True)
class ContextCompiler:
    constitution: str
    max_window_tokens: int = 200_000
    rebase_target_ratio: float = 0.75

    def system_message(self) -> Message:
        return {"role": "system", "content": self.constitution}

    def compile(self, thread: Thread, new_messages: list[Message]) -> list[Message]:
        return [self.system_message(), *thread.window(), *new_messages]

    def rebase_point(self, thread: Thread, incoming: list[Message]) -> int | None:
        """If the window plus incoming messages exceed the budget, return the new base index.

        The new base is the earliest user message after which the window fits the target size,
        so tool calls and their results are never split apart.
        """
        window = thread.window()
        if estimate_tokens([*window, *incoming]) <= self.max_window_tokens:
            return None
        return _moved(
            thread, _cut(thread, incoming, int(self.max_window_tokens * self.rebase_target_ratio))
        )

    def fresh_point(self, thread: Thread, incoming: list[Message], budget: int) -> int | None:
        """After a pause, the provider's cache is gone and the whole window would be paid in
        full: keep only the latest exchanges that fit `budget`, cut at a user message."""
        if estimate_tokens([*thread.window(), *incoming]) <= budget:
            return None
        return _moved(thread, _cut(thread, incoming, budget))


def _moved(thread: Thread, point: int) -> int | None:
    """`point`, unless the window already starts there (only its last exchange is left)."""
    return point if point > thread.base else None


def _cut(thread: Thread, incoming: list[Message], target: int) -> int:
    """The earliest user message after which the window and `incoming` fit `target`.

    The last exchange always stays: dropping it too left Orion answering a follow-up with no
    idea what it had just done (a long browsing turn alone outweighed the target)."""
    window = thread.window()
    incoming_tokens = estimate_tokens(incoming)
    last = None
    for offset, message in enumerate(window):
        if message.get("role") != "user":
            continue
        if estimate_tokens(window[offset:]) + incoming_tokens <= target:
            return thread.base + offset
        last = offset
    return thread.base + (last if last is not None else len(window))
