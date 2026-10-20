"""What Orion spends on models, and a ceiling on it (D79).

Every model call goes through this wrapper. What a call cost is journaled (`spend.recorded`,
with the model, who asked and whether the owner was waiting for the answer), so the tally is a
projection like any other: rebuilt from the journal, the same on every restart.

Two kinds of calls:

- **in the foreground**: a turn answering the owner (a message, a call). The owner is waiting,
  so these are never cut; each turn has its own cap (`agent.max_cost_usd_per_turn`).
- **in the background**: everything Orion does on its own (the night's thinking, curiosity,
  missions, the chronicle, the web clerk working for them, initiative messages). These stop when
  the day's or the month's ceiling is reached, until the next day or month: the call fails
  closed (`ProviderError`), and each background job already treats that as « not now ».

The owner hears about it once per period: at `warn_at` of the month's ceiling, and when the
background stops (through the initiative engine: quiet hours apply).

    spend.recorded
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from pydantic import BaseModel, ConfigDict

from orion.config import BudgetConfig
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.initiative.engine import Alert
from orion.providers.base import (
    Embedder,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderError,
    Speaker,
)
from orion.providers.foreground import waiting
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

log = structlog.get_logger(__name__)

SPENT = "spend.recorded"


@dataclass
class Tally:
    name: str = "spending"
    days: dict[str, list[float]] = field(default_factory=dict[str, list[float]])
    """local day → [foreground, background] in dollars"""
    months: dict[str, float] = field(default_factory=dict[str, float])
    """local month (YYYY-MM) → total"""
    zone: str = "Europe/Paris"
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.days.clear()
        self.months.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted or event.kind != SPENT:
            return
        payload: dict[str, Any] = event.payload
        at = datetime.fromisoformat(str(payload["at"])).astimezone(ZoneInfo(self.zone))
        cost = float(payload["cost_usd"])
        day = self.days.setdefault(at.date().isoformat(), [0.0, 0.0])
        day[0 if payload.get("foreground") else 1] += cost
        month = at.strftime("%Y-%m")
        self.months[month] = self.months.get(month, 0.0) + cost

    def background(self, day: date) -> float:
        return self.days.get(day.isoformat(), [0.0, 0.0])[1]

    def month(self, day: date) -> float:
        return self.months.get(day.strftime("%Y-%m"), 0.0)


class Spending:
    """Wraps the model provider: journals what each call cost, and stops background calls
    over the ceiling."""

    name = "spending"

    def __init__(
        self,
        inner: ModelProvider,
        journal: EventStore,
        budget: BudgetConfig,
        timezone: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.inner = inner
        self._journal = journal
        self.budget = budget
        self.zone = ZoneInfo(timezone)
        self.clock = clock or (lambda: datetime.now(self.zone))
        self.tally = Tally(zone=timezone)
        self._runner = ProjectionRunner(journal, [self.tally])

    async def sync(self) -> None:
        await self._runner.catch_up()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        owner_waiting = waiting()
        if not owner_waiting:
            await self.sync()
            over = self.over(self.clock().date())
            if over is not None:
                raise ProviderError(
                    f"spending ceiling reached ({over}): background paused", retryable=False
                )
        response = await self.inner.complete(request)
        if response.usage.cost_usd > 0:
            await self._journal.append(
                [
                    NewEvent(
                        kind=SPENT,
                        payload={
                            "at": self.clock().isoformat(),
                            "model": response.model or request.model,
                            "session": request.session_id or "",
                            "foreground": owner_waiting,
                            "cost_usd": round(response.usage.cost_usd, 6),
                        },
                        provenance=Provenance(source="orion:spending", actor="orion:spending"),
                        trust=Trust.SYSTEM,
                    )
                ]
            )
        return response

    def over(self, day: date) -> str | None:
        """Which ceiling the background has reached today, if any."""
        if (
            self.budget.daily_usd is not None
            and self.tally.background(day) >= self.budget.daily_usd
        ):
            return f"{self.budget.daily_usd:.2f} $ today"
        if self.budget.monthly_usd is not None and self.tally.month(day) >= self.budget.monthly_usd:
            return f"{self.budget.monthly_usd:.2f} $ this month"
        return None

    def summary(self, day: date) -> str:
        fg, bg = self.tally.days.get(day.isoformat(), [0.0, 0.0])
        month = self.tally.month(day)
        lines = [
            f"Aujourd'hui : {fg + bg:.2f} $ ({fg:.2f} $ en te répondant, {bg:.2f} $ de moi-même)",
            f"Ce mois-ci : {month:.2f} $"
            + (f" sur {self.budget.monthly_usd:.2f} $" if self.budget.monthly_usd else ""),
        ]
        if self.budget.daily_usd is not None:
            lines.append(f"Plafond de ce que je fais seul : {self.budget.daily_usd:.2f} $ par jour")
        over = self.over(day)
        if over is not None:
            lines.append(f"Plafond atteint ({over}) : ce que je fais seul est en pause.")
        return "\n".join(lines)

    async def alerts(self, now: datetime) -> list[Alert]:
        """Told once per period: nearing the month's ceiling, and when the background stops.
        (The engine's ledger keeps each key from being said twice; quiet hours apply.)"""
        await self.sync()
        today = now.astimezone(self.zone).date()
        month = today.strftime("%Y-%m")
        found: list[Alert] = []
        spent = self.tally.month(today)
        ceiling = self.budget.monthly_usd
        if ceiling is not None and spent >= ceiling:
            found.append(
                Alert(
                    key=f"spending:month:{month}",
                    notice=f"Tu as dépensé {spent:.2f} $ en modèles ce mois-ci, le plafond "
                    f"({ceiling:.2f} $) est atteint. Ce que tu fais de toi-même (la nuit, la "
                    "curiosité, les missions) est en pause jusqu'au 1er ; tu continues de lui "
                    "répondre. Dis-le-lui en deux phrases, et que le plafond se change dans "
                    "`[budget]` sur le serveur.",
                    why="le plafond de dépense du mois est atteint",
                    importance=0.7,
                    fallback=f"Plafond du mois atteint ({ceiling:.2f} $) : je mets en pause ce "
                    "que je fais seul jusqu'au 1er. Je continue de te répondre.",
                )
            )
        elif ceiling is not None and spent >= ceiling * self.budget.warn_at:
            found.append(
                Alert(
                    key=f"spending:warn:{month}",
                    notice=f"Tu as dépensé {spent:.2f} $ en modèles ce mois-ci, sur un plafond "
                    f"de {ceiling:.2f} $. Préviens-le en une phrase, sans dramatiser.",
                    why="la dépense du mois approche du plafond",
                    importance=0.4,
                    fallback=f"Ce mois-ci, j'ai dépensé {spent:.2f} $ sur {ceiling:.2f} $.",
                )
            )
        daily = self.budget.daily_usd
        if daily is not None and self.tally.background(today) >= daily:
            found.append(
                Alert(
                    key=f"spending:day:{today.isoformat()}",
                    notice=f"Ce que tu as fait de toi-même aujourd'hui a coûté "
                    f"{self.tally.background(today):.2f} $, le plafond du jour ({daily:.2f} $) "
                    "est atteint : la suite attend demain. Dis-le-lui en une phrase.",
                    why="le plafond de dépense du jour est atteint",
                    fallback=f"Plafond du jour atteint ({daily:.2f} $) : ce que je fais seul "
                    "reprend demain.",
                )
            )
        return found


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


def spending_tools(spending: Spending) -> list[Tool]:
    async def show(_args: Any, _ctx: ToolContext) -> ToolResult:
        await spending.sync()
        return ToolResult(spending.summary(spending.clock().date()))

    return [
        Tool(
            name="spending",
            description="What you have spent on models today and this month, and the ceiling "
            "on what you do on your own (« combien tu m'as coûté ? »).",
            input_model=_NoArgs,
            handler=show,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        )
    ]


class _SpendingEmbedder(Spending):
    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        assert isinstance(self.inner, Embedder)
        return await self.inner.embed(model, texts)


class _SpendingSpeaker(Spending):
    async def speak(self, model: str, text: str, voice: str) -> bytes:
        assert isinstance(self.inner, Speaker)
        return await self.inner.speak(model, text, voice)


class _SpendingBoth(_SpendingEmbedder, _SpendingSpeaker):
    pass


def with_spending(
    inner: ModelProvider,
    journal: EventStore,
    budget: BudgetConfig,
    timezone: str,
    clock: Callable[[], datetime] | None = None,
) -> Spending:
    """The wrapper, embedding and speaking only if the provider does (Orion checks)."""
    embeds, speaks = isinstance(inner, Embedder), isinstance(inner, Speaker)
    kind = (
        _SpendingBoth
        if embeds and speaks
        else _SpendingEmbedder
        if embeds
        else _SpendingSpeaker
        if speaks
        else Spending
    )
    return kind(inner, journal, budget, timezone, clock)
