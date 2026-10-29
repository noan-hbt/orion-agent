"""What Orion spends, and a ceiling that stops the background, never the owner (D79)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from orion.config import BudgetConfig
from orion.core.journal import SqliteJournal
from orion.core.projections import ProjectionRunner
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
