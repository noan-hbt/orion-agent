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


def render_user_message(text: str, when: datetime, channel: str) -> Message:
    weekday = _WEEKDAYS_FR[when.weekday()]
    return {"role": "user", "content": f"[{weekday} {when:%Y-%m-%d %H:%M} · {channel}]\n{text}"}


@dataclass(frozen=True)
class ContextCompiler:
    constitution: str
    max_window_tokens: int = 120_000
    rebase_target_ratio: float = 0.5

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
        target = int(self.max_window_tokens * self.rebase_target_ratio)
        incoming_tokens = estimate_tokens(incoming)
        for offset, message in enumerate(window):
            if message.get("role") != "user":
                continue
            if estimate_tokens(window[offset:]) + incoming_tokens <= target:
                return thread.base + offset
        # Even the last user turn is too large: drop the whole window.
        return thread.base + len(window)
