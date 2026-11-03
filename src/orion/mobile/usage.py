"""What Orion is used for, counted from the journal: the app's usage screen.

Per local day: the model calls with their tokens (read, written, read again from the cache) and
what they cost, in the foreground or on its own; the messages he sent, by channel, and Orion's
answers; the tools run. And the last thing each plan said of its windows (`plan.quota`). A
projection like any other: rebuilt from the journal alone.

Tokens are counted from the calls journaled with them (`spend.recorded` carries them since the
usage screen): older calls count for their cost only. Every model and provider is reported, a
plan's at 0 $ like a paid one's, and a delegated job counts as one call of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from orion.console.dashboard import moment
from orion.context.threads import MESSAGE_KIND
from orion.core.events import Event
from orion.delegation.jobs import JOB_FINISHED
from orion.providers.spending import EXHAUSTED, QUOTA, SPENT

OWNER_THREAD = "owner"
TOKENS = ("prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens")
USAGE_PERIODS = (7, 30, 90, 0)
"""The periods the screen offers, in days; 0 is since the beginning."""
DELEGATED = "delegated"


@dataclass
class ModelDay:
    provider: str = ""
    calls: int = 0
    alone: int = 0
    prompt: int = 0
    completion: int = 0
    cached: int = 0
    written: int = 0
    cost: float = 0.0
    equivalent: float = 0.0
    points: dict[str, float] = field(default_factory=dict[str, float])
    """plan window → points of it moved while this model was the one working (an estimate)"""

    @property
    def fresh(self) -> int:
        """The tokens really worked on: what the cache gave back again counts apart."""
        return max(self.prompt - self.cached, 0) + self.completion

    def add(self, other: ModelDay) -> None:
        self.provider = self.provider or other.provider
        self.calls += other.calls
        self.alone += other.alone
        self.prompt += other.prompt
        self.completion += other.completion
        self.cached += other.cached
        self.written += other.written
        self.cost += other.cost
        self.equivalent += other.equivalent
        for window, points in other.points.items():
            self.points[window] = self.points.get(window, 0.0) + points


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
    quota: dict[tuple[str, str], float] = field(default_factory=dict[tuple[str, str], float])
    """(provider, window) → the points it moved that day"""
    models: dict[tuple[str, str], ModelDay] = field(default_factory=dict[tuple[str, str], ModelDay])
    """(provider, model) → its day: the same model through two providers stays two."""


def _thinned(
    readings: list[tuple[datetime, float, str | None]], first: date, most: int = 240
) -> list[dict[str, Any]]:
    """A window's readings since `first`, few enough to send: the last one is always there."""
    kept = [r for r in readings if r[0].date() >= first]
    step = max(len(kept) // most, 1)
    thin = kept[-1::-step][::-1]
    return [{"at": at.isoformat(), "used": round(used, 3)} for at, used, _ in thin]


def provider_of(payload: dict[str, Any], model: str) -> str:
    """Calls journaled before D190 name no provider: only OpenRouter's models have a slash."""
    return str(payload.get("provider") or "") or ("openrouter" if "/" in model else "")


def _figures(found: ModelDay) -> dict[str, Any]:
    return {
        "calls": found.calls,
        "alone": found.alone,
        "prompt_tokens": found.prompt,
        "completion_tokens": found.completion,
        "cached_tokens": found.cached,
        "cache_write_tokens": found.written,
        "tokens": found.fresh,
        "cost": round(found.cost, 4),
        "equivalent": round(found.equivalent, 4),
        "quota": {window: round(points, 1) for window, points in found.points.items()},
        "quota_window": _main_window(found),
    }


def _unread(provider: str, found: ModelDay) -> bool:
    """A plan (no cost per call) that never said what its windows have used: ChatGPT's."""
    return provider not in ("", DELEGATED) and found.calls > 0 and found.cost == 0


def _main_window(found: ModelDay) -> str | None:
    """The window a model's points are told in: the week when known, the longest-lived there is."""
    return "seven_day" if "seven_day" in found.points else min(found.points, default=None)


@dataclass
class UsageStats:
    name: str = "usage"
    zone: str = "Europe/Paris"
    days: dict[date, Day] = field(default_factory=dict[date, Day])
    quotas: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    """provider → the last `plan.quota` payload"""
    readings: dict[tuple[str, str], list[tuple[datetime, float, str | None]]] = field(
        default_factory=dict[tuple[str, str], list[tuple[datetime, float, str | None]]]
    )
    """(provider, window) → when, how much used (0 to 1), when it resets"""
    working: dict[str, dict[str, int]] = field(default_factory=dict[str, dict[str, int]])
    """provider → model → the tokens worked since that plan's last reading"""
    exhausted: dict[str, list[list[datetime | None]]] = field(
        default_factory=dict[str, list[list[datetime | None]]]
    )
    """provider → [when its allowance was found used up, when it answered again]"""
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.days.clear()
        self.quotas.clear()
        self.readings.clear()
        self.working.clear()
        self.exhausted.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        kind = event.kind
        if kind not in (
            QUOTA,
            EXHAUSTED,
            SPENT,
            JOB_FINISHED,
            "message.received",
            MESSAGE_KIND,
            "tool.executed",
        ):
            return
        zone = ZoneInfo(self.zone)
        if kind in (SPENT, QUOTA, EXHAUSTED) and payload.get("at"):
            at = datetime.fromisoformat(str(payload["at"])).astimezone(zone)
        else:
            at = moment(event, zone)
        day = self.days.setdefault(at.date(), Day())
        if kind == QUOTA:
            self._read_quota(payload, day, at)
            return
        if kind == EXHAUSTED:
            self.exhausted.setdefault(str(payload.get("provider") or ""), []).append([at, None])
            return
        if kind == SPENT:
            cost = float(payload.get("cost_usd") or 0)
            day.calls += 1
            day.cost[0 if payload.get("foreground") else 1] += cost
            for key in TOKENS:
                day.tokens[key] += int(payload.get(key) or 0)
            name = str(payload.get("model") or "?")
            provider = provider_of(payload, name)
            if (spells := self.exhausted.get(provider)) and spells[-1][1] is None:
                spells[-1][1] = at  # it answers again
            model = day.models.setdefault((provider, name), ModelDay(provider))
            model.calls += 1
            model.alone += 0 if payload.get("foreground") else 1
            model.prompt += int(payload.get("prompt_tokens") or 0)
            model.completion += int(payload.get("completion_tokens") or 0)
            model.cached += int(payload.get("cached_tokens") or 0)
            model.written += int(payload.get("cache_write_tokens") or 0)
            model.cost += cost
            model.equivalent += float(payload.get("equivalent_usd") or 0)
            worked = self.working.setdefault(provider, {})
            fresh = int(payload.get("prompt_tokens") or 0) - int(payload.get("cached_tokens") or 0)
            worked[name] = worked.get(name, 0) + max(fresh, 0) + 1
        elif kind == JOB_FINISHED:
            # A delegated job says what it would cost, not what was billed (a plan pays nothing).
            job = day.models.setdefault((DELEGATED, DELEGATED), ModelDay(DELEGATED))
            job.calls += 1
            job.alone += 1
            job.equivalent += float(payload.get("cost_usd") or 0)
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

    def _read_quota(self, payload: dict[str, Any], day: Day, at: datetime) -> None:
        """A plan says only how much of a window is used, never what one call took. The points
        moved since its last reading go to the models that worked in between, by their tokens:
        an estimate, and a high one, since anything else on the same plan moves them too."""
        provider = str(payload.get("provider") or "")
        self.quotas[provider] = payload
        worked = self.working.pop(provider, {})
        for window in cast(list[dict[str, Any]], payload.get("windows") or []):
            name = str(window.get("name") or "")
            used = float(window.get("used") or 0)
            resets_at = cast(str | None, window.get("resets_at"))
            found = self.readings.setdefault((provider, name), [])
            moved = 0.0
            if found:
                _, last, last_reset = found[-1]
                if resets_at and last_reset and resets_at < last_reset:
                    # A window never resets sooner than it said: another account, not usage.
                    moved = 0.0
                elif used < last and resets_at != last_reset:
                    moved = used  # the window started over
                else:
                    moved = max(used - last, 0.0)  # lower with the same reset: their rounding
            found.append((at, used, resets_at))
            if moved <= 0:
                continue
            points = moved * 100
            day.quota[(provider, name)] = day.quota.get((provider, name), 0.0) + points
            weight = sum(worked.values())
            for model, tokens in worked.items():
                share = day.models.setdefault((provider, model), ModelDay(provider))
                share.points[name] = share.points.get(name, 0.0) + points * tokens / weight

    def report(self, today: date, span: int = 30) -> dict[str, Any]:
        """The last `span` days (0: since the beginning), day by day and model by model, with
        their totals and the same length just before; today, this week; since the beginning;
        the plans' windows."""
        everything = span == 0
        if everything:
            span = (today - min(self.days)).days + 1 if self.days else 1
        order = [today - timedelta(days=i) for i in reversed(range(span))]
        week = [today - timedelta(days=i) for i in reversed(range(7))]
        before = [order[0] - timedelta(days=i) for i in range(1, span + 1)]

        def total(days: list[date]) -> dict[str, Any]:
            found = [self.days[d] for d in days if d in self.days]
            tokens = {k: sum(d.tokens[k] for d in found) for k in TOKENS}
            return {
                "calls": sum(d.calls for d in found),
                **tokens,
                "tokens": max(tokens["prompt_tokens"] - tokens["cached_tokens"], 0)
                + tokens["completion_tokens"],
                "cost": round(sum(sum(d.cost) for d in found), 4),
                "equivalent": round(sum(m.equivalent for d in found for m in d.models.values()), 4),
                "alone": round(sum(d.cost[1] for d in found), 4),
                "received": sum(sum(d.received.values()) for d in found),
                "answers": sum(d.answers for d in found),
                "tools": sum(d.tools for d in found),
                "cache_share": round(tokens["cached_tokens"] / tokens["prompt_tokens"], 3)
                if tokens["prompt_tokens"]
                else None,
            }

        models: dict[tuple[str, str], ModelDay] = {}
        providers: dict[str, ModelDay] = {}
        channels: dict[str, int] = {}
        series: list[dict[str, Any]] = []
        for d in order:
            day = self.days.get(d) or Day()
            shares: dict[str, ModelDay] = {}
            for key, found in day.models.items():
                models.setdefault(key, ModelDay()).add(found)
                providers.setdefault(found.provider, ModelDay()).add(found)
                shares.setdefault(found.provider, ModelDay()).add(found)
            for channel, count in day.received.items():
                channels[channel] = channels.get(channel, 0) + count
            series.append(
                {
                    "day": d.isoformat(),
                    "tokens": sum(s.fresh for s in shares.values()),
                    "calls": sum(s.calls for s in shares.values()),
                    "cost": round(sum(day.cost), 4),
                    "messages": sum(day.received.values()),
                    "providers": {
                        p: {"tokens": s.fresh, "calls": s.calls, "cost": round(s.cost, 4)}
                        for p, s in shares.items()
                    },
                }
            )

        def trail(key: tuple[str, str]) -> list[dict[str, Any]]:
            kept: list[dict[str, Any]] = []
            window = _main_window(models[key])
            for d in order:
                found = self.days[d].models.get(key) if d in self.days else None
                kept.append(
                    {
                        "day": d.isoformat(),
                        "tokens": found.fresh if found else 0,
                        "calls": found.calls if found else 0,
                        "cost": round(found.cost, 4) if found else 0.0,
                        "points": round(found.points.get(window, 0.0), 1)
                        if found and window
                        else 0.0,
                    }
                )
            return kept

        def spelled(provider: str) -> list[dict[str, str | None]]:
            """The times a plan was used up during the period, or still is."""
            return [
                {"at": at.isoformat(), "back": back.isoformat() if back else None}
                for at, back in self.exhausted.get(provider, [])
                if at and (back is None or back.date() >= order[0])
            ]

        def biggest(item: tuple[Any, ModelDay]) -> tuple[int, int, float]:
            return (-item[1].fresh, -item[1].calls, -item[1].cost)

        return {
            "today": total([today]),
            "week": total(week),
            "span": {"days": span, "everything": everything, **total(order)},
            "previous": total(before) if any(d in self.days for d in before) else None,
            "ever": {"since": min(self.days).isoformat() if self.days else None}
            | total(list(self.days)),
            "series": series,
            "providers": [
                {"provider": name, **_figures(found)}
                for name, found in sorted(providers.items(), key=biggest)
            ],
            "models": [
                {"model": key[1], "provider": key[0], **_figures(found), "series": trail(key)}
                for key, found in sorted(models.items(), key=biggest)
            ],
            "plans": [
                {
                    "provider": provider,
                    "window": name,
                    "used": round(found[-1][1], 3),
                    "points": round(
                        sum(
                            self.days[d].quota.get((provider, name), 0.0)
                            for d in order
                            if d in self.days
                        ),
                        1,
                    ),
                    "today": round(
                        self.days[today].quota.get((provider, name), 0.0)
                        if today in self.days
                        else 0.0,
                        1,
                    ),
                    "readings": _thinned(found, order[0]),
                }
                for (provider, name), found in sorted(self.readings.items())
                if found
            ],
            "channels": [
                {"channel": c, "count": n} for c, n in sorted(channels.items(), key=lambda i: -i[1])
            ],
            "quotas": [
                {"provider": provider, "at": q.get("at"), "windows": q.get("windows") or []}
                for provider, q in sorted(self.quotas.items())
            ],
            "unread": [
                {"provider": name, "exhausted": spelled(name)}
                for name in sorted(
                    {n for n, found in providers.items() if _unread(n, found)}
                    | {n for n in self.exhausted if spelled(n)}
                )
                if not any(p == name for p, _ in self.readings)
            ],
        }
