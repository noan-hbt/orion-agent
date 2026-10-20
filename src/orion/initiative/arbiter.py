"""The arbiter: is this worth interrupting the owner now?

The gate (quiet hours, budget, expiry) says whether Orion may speak; the arbiter says whether it
should, now. It weighs the message's urgency against what interrupting costs at this moment,
with plain rules whose verdict is journaled with the message:

- urgency is the message's importance, rising as its deadline nears and as it waits (the cost
  of delay grows, so nothing waits forever);
- interruption costs more while the owner is busy (a calendar event under way) and as the
  day's budget runs out, and less right after an event ended: the natural break where a
  message lands best;
- while the owner said they are focused (« je bosse jusqu'à 18 h »), only what cannot wait
  gets through, and waiting does not make the rest more urgent: it all waits for the end.

A message that is not worth it yet waits for a better moment (it is not dropped): the next
tick asks again. Explicit requests (reminders, outcomes the owner asked for) never go through
the arbiter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

BREAK = timedelta(minutes=20)
"""How long after an event ends counts as a break."""


@dataclass(frozen=True)
class Moment:
    busy: bool = False
    """The owner is in a calendar event right now."""
    free_since: datetime | None = None
    """When the owner's last event ended, if they are free."""
    focus_until: datetime | None = None
    """The owner asked not to be disturbed until then (the focus module)."""


class PresenceSource(Protocol):
    async def presence(self, now: datetime) -> Moment: ...


@dataclass(frozen=True)
class Judgement:
    now: bool
    urgency: float
    cost: float

    @property
    def reason(self) -> str:
        verdict = "maintenant" if self.now else "plus tard"
        return f"{verdict} (urgence {self.urgency:.2f}, dérangement {self.cost:.2f})"


def merge(moments: list[Moment]) -> Moment:
    busy = any(m.busy for m in moments)
    ends = [m.free_since for m in moments if m.free_since is not None]
    focus = [m.focus_until for m in moments if m.focus_until is not None]
    return Moment(busy, None if busy or not ends else max(ends), max(focus, default=None))


FOCUS_COST = 0.95
"""While the owner is focused, only a message that cannot wait (urgency 0.95 and above) goes."""


def judge(
    importance: float,
    now: datetime,
    moment: Moment,
    spent: int,
    budget: int,
    expires_at: datetime | None = None,
    waiting_since: datetime | None = None,
) -> Judgement:
    urgency = importance
    focused = moment.focus_until is not None and now < moment.focus_until
    if expires_at is not None:
        left = (expires_at - now).total_seconds() / 60
        if left < 30:
            urgency += 0.4 * (1 - max(left, 0) / 30)
    if waiting_since is not None and not focused:
        hours = (now - waiting_since).total_seconds() / 3600
        urgency += min(0.4, 0.1 * hours)
    cost = 0.3
    if moment.busy:
        cost += 0.5
    elif moment.free_since is not None and now - moment.free_since <= BREAK:
        cost -= 0.2
    if budget:
        cost += 0.2 * min(spent / budget, 1.0)
    if focused:
        cost = max(cost, FOCUS_COST)
    urgency, cost = min(urgency, 1.0), max(cost, 0.0)
    return Judgement(urgency >= cost, round(urgency, 2), round(cost, 2))
