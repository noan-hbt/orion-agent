"""What Orion is used for, counted from the journal: the app's usage screen.

Per local day: the model calls with their tokens (read, written, read again from the cache) and
what they cost, in the foreground or on its own; the messages he sent, by channel, and Orion's
answers; the tools run. And the last thing each plan said of its windows (`plan.quota`). A
projection like any other: rebuilt from the journal alone.

Tokens are counted from the calls journaled with them (`spend.recorded` carries them since the
usage screen): older calls count for their cost only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from orion.console.dashboard import moment
from orion.context.threads import MESSAGE_KIND
from orion.core.events import Event
from orion.providers.spending import QUOTA, SPENT

OWNER_THREAD = "owner"
TOKENS = ("prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens")


@dataclass
class Day:
    calls: int = 0
    tokens: dict[str, int] = field(default_factory=lambda: dict.fromkeys(TOKENS, 0))
    cost: list[float] = field(default_factory=lambda: [0.0, 0.0])
    """[in the foreground, on its own]"""
    received: dict[str, int] = field(default_factory=dict[str, int])
    """channel → his messages"""
    answers: int = 0
    tools: int = 0
    models: dict[str, list[float]] = field(default_factory=dict[str, list[float]])
    """model → [calls, tokens, cost]"""


@dataclass
class UsageStats:
    name: str = "usage"
    zone: str = "Europe/Paris"
    days: dict[date, Day] = field(default_factory=dict[date, Day])
    quotas: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    """provider → the last `plan.quota` payload"""
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.days.clear()
        self.quotas.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        kind = event.kind
        if kind == QUOTA:
            self.quotas[str(payload.get("provider") or "")] = payload
            return
        if kind not in (SPENT, "message.received", MESSAGE_KIND, "tool.executed"):
            return
        zone = ZoneInfo(self.zone)
        if kind == SPENT and payload.get("at"):
            at = datetime.fromisoformat(str(payload["at"])).astimezone(zone)
        else:
            at = moment(event, zone)
        day = self.days.setdefault(at.date(), Day())
        if kind == SPENT:
            cost = float(payload.get("cost_usd") or 0)
            day.calls += 1
            day.cost[0 if payload.get("foreground") else 1] += cost
            for key in TOKENS:
                day.tokens[key] += int(payload.get(key) or 0)
            model = day.models.setdefault(str(payload.get("model") or "?"), [0, 0, 0.0])
            model[0] += 1
            model[1] += int(payload.get("prompt_tokens") or 0) + int(
                payload.get("completion_tokens") or 0
            )
            model[2] += cost
        elif kind == "message.received":
            channel = str(payload.get("channel") or "?")
            day.received[channel] = day.received.get(channel, 0) + 1
        elif kind == MESSAGE_KIND:
            message = cast(dict[str, Any], payload.get("message") or {})
            if (
                payload.get("thread_id") == OWNER_THREAD
                and message.get("role") == "assistant"
                and message.get("content")
            ):
                day.answers += 1
        else:
            day.tools += 1

    def report(self, today: date, span: int = 30) -> dict[str, Any]:
        """The last `span` days, day by day, with their totals; today, this week; since the
        beginning; the plans' windows."""
        order = [today - timedelta(days=i) for i in reversed(range(span))]
        week = order[-7:]

        def total(days: list[date]) -> dict[str, Any]:
            found = [self.days[d] for d in days if d in self.days]
            tokens = {k: sum(d.tokens[k] for d in found) for k in TOKENS}
            return {
                "calls": sum(d.calls for d in found),
                **tokens,
                "cost": round(sum(sum(d.cost) for d in found), 4),
                "alone": round(sum(d.cost[1] for d in found), 4),
                "received": sum(sum(d.received.values()) for d in found),
                "answers": sum(d.answers for d in found),
                "tools": sum(d.tools for d in found),
                "cache_share": round(tokens["cached_tokens"] / tokens["prompt_tokens"], 3)
                if tokens["prompt_tokens"]
                else None,
            }

        models: dict[str, list[float]] = {}
        channels: dict[str, int] = {}
        for d in order:
            if (day := self.days.get(d)) is None:
                continue
            for name, (calls, tokens, cost) in day.models.items():
                kept = models.setdefault(name, [0, 0, 0.0])
                kept[0] += calls
                kept[1] += tokens
                kept[2] += cost
            for channel, count in day.received.items():
                channels[channel] = channels.get(channel, 0) + count
        return {
            "today": total([today]),
            "week": total(week),
            "span": {"days": span, **total(order)},
            "ever": {"since": min(self.days).isoformat() if self.days else None}
            | total(list(self.days)),
            "series": [
                {
                    "day": d.isoformat(),
                    "tokens": sum(self.days[d].tokens[k] for k in TOKENS[:2])
                    if d in self.days
                    else 0,
                    "cost": round(sum(self.days[d].cost), 4) if d in self.days else 0.0,
                    "messages": sum(self.days[d].received.values()) if d in self.days else 0,
                }
                for d in order
            ],
            "models": [
                {"model": name, "calls": int(c), "tokens": int(t), "cost": round(cost, 4)}
                for name, (c, t, cost) in sorted(models.items(), key=lambda i: (-i[1][2], -i[1][1]))
            ][:8],
            "channels": [
                {"channel": c, "count": n} for c, n in sorted(channels.items(), key=lambda i: -i[1])
            ],
            "quotas": [
                {"provider": provider, "at": q.get("at"), "windows": q.get("windows") or []}
                for provider, q in sorted(self.quotas.items())
            ],
        }
