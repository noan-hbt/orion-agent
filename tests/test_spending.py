"""What Orion spends, and a ceiling that stops the background, never the owner (D79)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from orion.config import BudgetConfig
from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import SqliteJournal
from orion.core.projections import ProjectionRunner
from orion.delegation.jobs import JOB_FINISHED
from orion.mobile.usage import UsageStats
from orion.providers.base import (
    Embedder,
    ModelRequest,
    ModelResponse,
    ProviderError,
    QuotaWindow,
    Speaker,
)
from orion.providers.foreground import foreground
from orion.providers.spending import QUOTA, SPENT, Spending, spending_tools, with_spending
from orion.tools.base import ToolContext
from tests.fakes import Script, ScriptedProvider, reply

PARIS = ZoneInfo("Europe/Paris")
NOON = datetime(2026, 10, 20, 12, 0, tzinfo=PARIS)
REQUEST = ModelRequest(model="m", messages=[{"role": "user", "content": "x"}], session_id="s")


def wallet(
    journal: SqliteJournal, provider: ScriptedProvider, now: list[datetime], **budget: float | None
) -> Spending:
    return with_spending(
        provider, journal, BudgetConfig.model_validate(budget), "Europe/Paris", clock=lambda: now[0]
    )


async def test_each_call_is_journaled_and_tallied_by_who_was_waiting(
    journal: SqliteJournal,
) -> None:
    provider = ScriptedProvider(reply("a", cost=0.25), reply("b", cost=0.5), reply("c", cost=0))
    now = [NOON]
    spending = wallet(journal, provider, now)
    await spending.complete(REQUEST)
    with foreground():
        await spending.complete(REQUEST)
    await spending.complete(REQUEST)  # free (a plan): journaled for its tokens
    events = [e for e in await journal.read() if e.kind == SPENT]
    assert [e.payload["foreground"] for e in events] == [False, True, False]
    await spending.sync()
    assert spending.tally.days["2026-10-20"] == [0.5, 0.25]
    (tool,) = spending_tools(spending)
    shown = await tool.handler(tool.parse("{}"), ToolContext("owner", "c"))
    assert "0.75 $ (0.50 $ en te répondant, 0.25 $ de moi-même)" in shown.content


async def test_the_background_stops_at_the_daily_ceiling_the_owner_never(
    journal: SqliteJournal,
) -> None:
    provider = ScriptedProvider(reply("a", cost=1.0), reply("b", cost=0.2), reply("c", cost=0.1))
    now = [NOON]
    spending = wallet(journal, provider, now, daily_usd=1.0)
    await spending.complete(REQUEST)
    with pytest.raises(ProviderError, match=r"1\.00 \$ today") as stopped:
        await spending.complete(REQUEST)
    assert not stopped.value.retryable
    with foreground():
        await spending.complete(REQUEST)  # he is waiting: it goes through
    (alert,) = await spending.alerts(now[0])
    assert alert.key == "spending:day:2026-10-20" and "demain" in alert.fallback

    now[0] = NOON + timedelta(days=1)  # a new day
    await spending.complete(REQUEST)
    assert await spending.alerts(now[0]) == []


async def test_the_month_warns_once_then_stops_everything_but_him(journal: SqliteJournal) -> None:
    provider = ScriptedProvider(*(reply("x", cost=4.0) for _ in range(3)))
    now = [NOON]
    spending = wallet(journal, provider, now, daily_usd=100, monthly_usd=10)
    await spending.complete(REQUEST)
    await spending.complete(REQUEST)
    (warn,) = await spending.alerts(now[0])
    assert warn.key == "spending:warn:2026-10" and "8.00 $ sur 10.00 $" in warn.fallback
    with foreground():
        await spending.complete(REQUEST)
    (cap,) = await spending.alerts(now[0])
    assert cap.key == "spending:month:2026-10" and cap.importance > warn.importance
    with pytest.raises(ProviderError, match="this month"):
        await spending.complete(REQUEST)


async def test_no_ceiling_means_none(journal: SqliteJournal) -> None:
    provider = ScriptedProvider(reply("a", cost=50.0), reply("b", cost=50.0))
    spending = wallet(journal, provider, [NOON], daily_usd=None, monthly_usd=None)
    await spending.complete(REQUEST)
    await spending.complete(REQUEST)
    assert await spending.alerts(NOON) == []


def test_the_wrapper_embeds_and_speaks_only_if_the_provider_does(journal: SqliteJournal) -> None:
    plain = with_spending(ScriptedProvider(), journal, BudgetConfig(), "Europe/Paris")
    assert not isinstance(plain, Embedder) and not isinstance(plain, Speaker)

    class Both(ScriptedProvider):
        async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
            return [[1.0] for _ in texts]

        async def speak(self, model: str, text: str, voice: str) -> bytes:
            return b"son"

    both = with_spending(Both(), journal, BudgetConfig(), "Europe/Paris")
    assert isinstance(both, Embedder) and isinstance(both, Speaker)


async def test_a_plans_calls_and_windows_reach_the_usage_screen(journal: SqliteJournal) -> None:
    def plan(used: float) -> Script:
        def make(request: ModelRequest) -> ModelResponse:
            response = reply("ok", prompt_tokens=1000, cached=800, cost=0)(request)
            window = QuotaWindow(name="five_hour", used=used, resets_at="2026-10-20T15:00:00+00:00")
            return response.model_copy(update={"provider": "claude-code", "quota": [window]})

        return make

    provider = ScriptedProvider(plan(0.171), plan(0.172), plan(0.25))
    spending = wallet(journal, provider, [NOON])
    for _ in range(3):
        await spending.complete(REQUEST)
    stats = UsageStats()
    await ProjectionRunner(journal, [stats]).catch_up()
    seen = stats.report(NOON.date())
    assert seen["today"]["calls"] == 3 and seen["today"]["prompt_tokens"] == 3000
    assert seen["today"]["cache_share"] == 0.8
    assert len([e for e in await journal.read() if e.kind == QUOTA]) == 2  # 17 % twice: once
    [quota] = seen["quotas"]
    assert quota["provider"] == "claude-code" and quota["windows"][0]["used"] == 0.25


async def test_a_plan_at_no_cost_stands_beside_paid_models_and_compares_with_before(
    journal: SqliteJournal,
) -> None:
    today = datetime.now(ZoneInfo("Europe/Paris")).replace(hour=12)
    source = Provenance(source="test", actor="test")

    def spent(model: str, provider: str, tokens: int, cost: float, ago: int = 0) -> NewEvent:
        return NewEvent(
            kind=SPENT,
            payload={
                "at": (today - timedelta(days=ago)).isoformat(),
                "model": model,
                "provider": provider,
                "foreground": True,
                "cost_usd": cost,
                "equivalent_usd": 0.0 if cost else 0.5,
                "prompt_tokens": tokens,
                "completion_tokens": 10,
                "cached_tokens": tokens // 2,
            },
            provenance=source,
            trust=Trust.SYSTEM,
        )

    await journal.append(
        [
            *(spent(f"paid/model-{i}", "openrouter", 100, 1.0) for i in range(9)),
            spent("paid/model-0", "together", 100, 1.0),
            spent("claude-opus-5-5", "claude-code", 1000, 0),
            spent("claude-opus-5-5", "claude-code", 1000, 0, ago=10),
            NewEvent(
                kind=JOB_FINISHED, payload={"cost_usd": 2.0}, provenance=source, trust=Trust.SYSTEM
            ),
        ]
    )
    stats = UsageStats()
    await ProjectionRunner(journal, [stats]).catch_up()
    seen = stats.report(today.date(), 7)
    plan = next(m for m in seen["models"] if m["model"] == "claude-opus-5-5")
    assert plan["calls"] == 1 and plan["tokens"] == 510 and plan["equivalent"] == 0.5
    assert sum(d["calls"] for d in plan["series"]) == 1
    assert {p["provider"] for p in seen["providers"]} == {
        "claude-code",
        "openrouter",
        "together",
        "delegated",
    }
    assert {m["provider"] for m in seen["models"] if m["model"] == "paid/model-0"} == {
        "openrouter",
        "together",
    }
    assert seen["previous"]["tokens"] == 510
    assert seen["span"]["cost"] == 10.0  # neither a plan's equivalent nor a job is money spent
    assert stats.report(today.date(), 0)["span"]["days"] == 11


async def test_a_plans_points_go_to_the_models_that_worked_and_survive_a_reset(
    journal: SqliteJournal,
) -> None:
    now = datetime.now(ZoneInfo("Europe/Paris")).replace(hour=12)
    source = Provenance(source="test", actor="test")

    def spent(model: str, tokens: int) -> NewEvent:
        payload = {
            "at": now.isoformat(),
            "model": model,
            "provider": "claude-code",
            "cost_usd": 0.0,
            "prompt_tokens": tokens,
        }
        return NewEvent(kind=SPENT, payload=payload, provenance=source, trust=Trust.SYSTEM)

    def read(used: float, resets: str) -> NewEvent:
        window = {"name": "seven_day", "used": used, "resets_at": resets}
        payload = {"at": now.isoformat(), "provider": "claude-code", "windows": [window]}
        return NewEvent(kind=QUOTA, payload=payload, provenance=source, trust=Trust.SYSTEM)

    await journal.append(
        [
            read(0.10, "2026-11-09T00:00:00+00:00"),
            spent("opus", 3000),
            spent("haiku", 1000),
            read(0.14, "2026-11-09T00:00:00+00:00"),
            read(
                0.13, "2026-11-09T00:00:00+00:00"
            ),  # the provider's rounding: nothing was given back
            spent("opus", 1000),
            read(0.02, "2026-11-16T00:00:00+00:00"),  # a new window: its two points count
            read(0.40, "2026-11-10T00:00:00+00:00"),  # resets sooner: another account, not usage
        ]
    )
    stats = UsageStats()
    await ProjectionRunner(journal, [stats]).catch_up()
    seen = stats.report(now.date(), 7)
    [plan] = seen["plans"]
    assert plan["used"] == 0.4 and plan["points"] == 6.0 and len(plan["readings"]) == 5
    points = {m["model"]: m["quota"]["seven_day"] for m in seen["models"]}
    assert points == {"opus": 5.0, "haiku": 1.0}
