"""A stronger model on call: `[modules.expert]` in orion.toml.

The everyday model is fast and cheap. When a question is hard (a plan with many constraints,
a tricky calculation, a delicate message), Orion decides itself:

- `consult_expert`: put one question to the stronger model, with the context Orion chooses to
  give, and get its answer back as a tool result; the conversation stays on the everyday model;
- `think_harder`: the rest of this turn runs on the stronger model (its cost cap goes up to
  `max_cost_usd_per_turn` of this section). The next turn is back on the everyday model.

Both are journaled with their cost (`expert.consulted`, `expert.switched`), and capped per day.

    expert.consulted  expert.switched
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from orion.cognition.loop import SWITCH_COST, SWITCH_MODEL
from orion.config import STRONG_MODEL
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.modules.base import Module, ModuleSettings, Plate
from orion.providers.base import ModelProvider, ModelRequest, ProviderError
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

log = structlog.get_logger(__name__)

CONSULTED = "expert.consulted"
SWITCHED = "expert.switched"

ADVISOR = """Tu conseilles Orion, l'assistant personnel d'une personne. Orion te pose une question
difficile avec le contexte qu'il juge utile. Réponds en français, précisément et directement :
la réponse, le raisonnement qui compte, et ce qui reste incertain. Pas de préambule. Tu ne
parles pas à la personne : Orion reformulera."""


class ExpertSettings(ModuleSettings):
    enabled: bool = True
    model: str | None = Field(
        default=None, description="The stronger model, consulted or used; default: strong_model"
    )
    max_per_day: int = Field(default=30, ge=0, description="Consultations and switches per day")
    max_cost_usd_per_turn: float = Field(
        default=2.0, gt=0, description="A turn's cost cap once switched to the stronger model"
    )
    answer_tokens: int = Field(
        default=8000,
        ge=1000,
        le=32000,
        description="Room for the expert's answer, thinking included (it thinks before writing)",
    )


class _Day:
    name = "expert"

    def __init__(self) -> None:
        self.used: dict[str, int] = {}
        self._seq = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.used.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.kind in (CONSULTED, SWITCHED) and not event.redacted:
            day = str(event.payload.get("day", ""))
            self.used[day] = self.used.get(day, 0) + 1


class ConsultInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=5, max_length=4000)
    context: str = Field(
        default="",
        max_length=40_000,
        description="What the expert needs to know: it sees nothing else (no memory, no "
        "conversation). Only the personal details the question needs.",
    )


class SwitchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    why: str = Field(max_length=200, description="Why this turn needs the stronger model")


class Expert:
    def __init__(
        self, journal: EventStore, provider: ModelProvider, settings: ExpertSettings
    ) -> None:
        self._journal = journal
        self._provider = provider
        self.settings = settings
        self.model = settings.model or STRONG_MODEL
        self._day = _Day()
        self._runner = ProjectionRunner(journal, [self._day])

    async def _left_today(self, today: date) -> int:
        await self._runner.catch_up()
        return self.settings.max_per_day - self._day.used.get(today.isoformat(), 0)

    async def consult(self, question: str, context: str, ctx: ToolContext) -> ToolResult:
        today = datetime.now().date()
        if await self._left_today(today) <= 0:
            return ToolResult("No consultation left today: answer with what you have.", True)
        content = f"{context}\n\nQuestion : {question}" if context else question
        try:
            response = await self._provider.complete(
                ModelRequest(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": ADVISOR},
                        {"role": "user", "content": content},
                    ],
                    session_id="orion:expert",
                    max_tokens=self.settings.answer_tokens,
                )
            )
        except ProviderError as exc:
            log.warning("expert.unavailable", error=str(exc))
            return ToolResult(f"The expert is unavailable: {exc}", is_error=True)
        await self._record(
            CONSULTED,
            {
                "day": today.isoformat(),
                "model": response.model,
                "question": question[:300],
                "cost_usd": response.usage.cost_usd,
            },
            ctx,
        )
        answer = (response.text or "").strip()
        if not answer:
            return ToolResult(
                "The expert used all its room thinking and wrote nothing: ask a narrower "
                "question, or answer with what you have.",
                is_error=True,
            )
        if response.finish_reason == "length":
            answer += "\n[réponse coupée : elle a atteint sa longueur maximale]"
        # Its answer is only as trustworthy as what it was given.
        return ToolResult(
            answer,
            trust=min(ctx.context_trust, Trust.SYSTEM),
            data={"cost_usd": response.usage.cost_usd},
        )

    async def switch(self, why: str, ctx: ToolContext) -> ToolResult:
        today = datetime.now().date()
        if await self._left_today(today) <= 0:
            return ToolResult("No switch left today: carry on with the everyday model.", True)
        await self._record(
            SWITCHED, {"day": today.isoformat(), "model": self.model, "why": why}, ctx
        )
        return ToolResult(
            f"La suite de ce tour se fait sur {self.model}.",
            data={
                SWITCH_MODEL: self.model,
                SWITCH_COST: self.settings.max_cost_usd_per_turn,
            },
        )

    async def _record(self, kind: str, payload: dict[str, Any], ctx: ToolContext) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload={"thread_id": ctx.thread_id, **payload},
                    provenance=Provenance(source=f"thread:{ctx.thread_id}", actor="orion:core"),
                    trust=Trust.SYSTEM,
                    correlation_id=ctx.correlation_id,
                )
            ]
        )


def expert_tools(expert: Expert) -> list[Tool]:
    async def consult(args: Any, ctx: ToolContext) -> ToolResult:
        return await expert.consult(args.question, args.context, ctx)

    async def switch(args: Any, ctx: ToolContext) -> ToolResult:
        return await expert.switch(args.why, ctx)

    return [
        Tool(
            name="consult_expert",
            description="Ask a stronger model one hard question (a plan with many constraints, "
            "a tricky calculation, a delicate message to write, a decision to weigh). It sees "
            "only what you put in `context`. Slower and dearer: not for simple things.",
            input_model=ConsultInput,
            handler=consult,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            accepts_untrusted_context=True,
            max_output_chars=12_000,
        ),
        Tool(
            name="think_harder",
            description="Do the rest of this turn on a stronger model, when the whole task is "
            "hard (several steps to plan, many constraints, a lot at stake). Back to the "
            "everyday model at the next message.",
            input_model=SwitchInput,
            handler=switch,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            accepts_untrusted_context=True,
        ),
    ]


class ExpertModule(Module):
    name = "expert"
    instructions = "expert.md"
    settings_model = ExpertSettings

    async def assemble(self, plate: Plate) -> list[Tool]:
        settings: ExpertSettings = self.settings
        model = settings.model or self.config.agent.strong_model
        return expert_tools(
            Expert(plate.journal, plate.provider, settings.model_copy(update={"model": model}))
        )

    def doctor(self) -> list[str]:
        settings: ExpertSettings = self.settings
        model = settings.model or self.config.agent.strong_model
        return [f"stronger model on call: {model} (at most {settings.max_per_day}/day)"]
