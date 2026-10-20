"""Advice: `[modules.advice]` in orion.toml (on by default).

A friend who recommends a restaurant asks, a few days later, how it was, and remembers the
answer the next time. Orion does the same with its own advice (D65):

- when it recommends something the owner may act on (a restaurant, a book, a film, something to
  buy, a way to answer someone), it notes it, with when he will have tried it;
- that day, in the evening, it asks how it went, once, like a friend would (through the
  initiative engine: quiet hours and the budget apply);
- his verdict is kept, and becomes a taste in memory (« n'a pas aimé : trop cher »);
- before recommending again in the same domain, it looks at its record there, and owns it:
  « la dernière fois que je t'ai conseillé un resto, tu l'as trouvé trop cher ».

    advice.given    (what, domain, follow up on)
    advice.verdict  (liked, disliked, mixed or not tried, and why)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.initiative.engine import Alert
from orion.memory.model import Category
from orion.memory.service import MemoryService, Origin
from orion.modules.base import Daemon, Module, ModuleSettings, Plate
from orion.tools.actions import grouped
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig

GIVEN = "advice.given"
VERDICT = "advice.verdict"
ASK_WINDOW = timedelta(days=2)
Verdict = Literal["aimé", "pas aimé", "mitigé", "pas essayé"]
_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


class AdviceSettings(ModuleSettings):
    enabled: bool = True
    ask_at: time = Field(default=time(19, 0), description="When Orion asks how it went")
    default_days: int = Field(default=3, ge=1, le=60, description="If no date was said")


@dataclass
class Advice:
    id: str
    what: str
    domain: str
    given: datetime
    follow_up: date
    trust: Trust
    verdict: str | None = None
    why: str = ""


class AdviceBook:
    """Every piece of advice, in the order given. A projection of the journal."""

    name = "advice"

    def __init__(self) -> None:
        self.items: dict[str, Advice] = {}
        self._seq = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.items.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind == GIVEN:
            self.items[str(payload["advice_id"])] = Advice(
                str(payload["advice_id"]),
                str(payload["what"]),
                str(payload["domain"]),
                datetime.fromisoformat(str(payload["given"])),
                date.fromisoformat(str(payload["follow_up"])),
                event.trust,
            )
        elif event.kind == VERDICT:
            item = self.items.get(str(payload["advice_id"]))
            if item is not None:
                item.verdict = str(payload["verdict"])
                item.why = str(payload.get("why", ""))


class AdviceKeeper:
    def __init__(
        self,
        journal: EventStore,
        memory: MemoryService | None,
        settings: AdviceSettings,
        timezone: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._journal = journal
        self._memory = memory
        self.settings = settings
        self.zone = ZoneInfo(timezone)
        self.clock = clock or (lambda: datetime.now(self.zone))
        self.book = AdviceBook()
        self._runner = ProjectionRunner(journal, [self.book])

    async def sync(self) -> None:
        await self._runner.catch_up()

    async def give(self, what: str, domain: str, follow_up: date, origin: Origin) -> str:
        advice_id = f"ad_{new_id()}"
        await self._append(
            GIVEN,
            {
                "advice_id": advice_id,
                "what": what,
                "domain": domain,
                "given": self.clock().isoformat(),
                "follow_up": follow_up.isoformat(),
            },
            origin,
        )
        return advice_id

    async def judge(self, advice: Advice, verdict: str, why: str, origin: Origin) -> None:
        await self._append(
            VERDICT, {"advice_id": advice.id, "verdict": verdict, "why": why}, origin
        )
        if self._memory is not None and verdict != "pas essayé":
            reason = f" : {why}" if why else ""
            await self._memory.remember(
                f"A {verdict} {advice.what} ({advice.domain}, conseillé par Orion){reason}.",
                origin,
                about=["owner"],
                category=Category.PREFERENCE,
            )

    async def _append(self, kind: str, payload: dict[str, Any], origin: Origin) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=Provenance(
                        source=origin.source, actor=origin.actor, derived_from=origin.derived_from
                    ),
                    trust=origin.trust,
                    correlation_id=origin.correlation_id,
                )
            ]
        )
        await self.sync()

    def record(self, domain: str = "") -> str:
        """Orion's record, in a domain or overall: what it advised, and what he thought."""
        items = [
            a for a in self.book.items.values() if not domain or domain.lower() in a.domain.lower()
        ]
        if not items:
            return "Aucun conseil noté" + (f" en « {domain} »." if domain else ".")
        judged = [a for a in items if a.verdict and a.verdict != "pas essayé"]
        liked = sum(1 for a in judged if a.verdict == "aimé")
        head = f"{len(items)} conseil(s), {len(judged)} avec un avis, dont {liked} aimé(s)."
        lines = [
            f"- ({a.id}) {a.given:%d/%m} {a.domain} : {a.what} → "
            + (f"{a.verdict}{f' ({a.why})' if a.why else ''}" if a.verdict else "pas encore d'avis")
            for a in items[-15:]
        ]
        return "\n".join([head, *lines])

    async def alerts(self, now: datetime) -> list[Alert]:
        await self.sync()
        found: list[Alert] = []
        for advice in self.book.items.values():
            if advice.verdict is not None:
                continue
            due = datetime.combine(advice.follow_up, self.settings.ask_at, self.zone)
            if not due <= now < due + ASK_WINDOW:
                continue
            found.append(
                Alert(
                    key=f"advice:{advice.id}",
                    notice=f"Le {_DAYS[advice.given.weekday()]} {advice.given:%d/%m}, tu lui "
                    f"as conseillé : {advice.what} ({advice.domain}). Demande-lui, en une "
                    "phrase et comme un ami qui s'en souvient, ce qu'il en a pensé ; s'il ne "
                    "l'a pas fait, pas de reproche. Sa réponse, note-la avec `advice` (verdict).",
                    why="je t'avais conseillé quelque chose",
                    fallback=f"Alors, {advice.what}, c'était comment ?",
                    trust=advice.trust,
                    expires_at=due + ASK_WINDOW,
                    importance=0.35,
                )
            )
        return found


class GiveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what: str = Field(
        min_length=3, max_length=200, description="What you recommended, named precisely"
    )
    domain: str = Field(
        min_length=2,
        max_length=40,
        description="One word or two: resto, livre, film, série, achat, sortie, voyage, "
        "façon de faire, message…",
    )
    try_on: date | None = Field(
        default=None, description="When he will have tried it, if known (the dinner's date)"
    )


class VerdictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    advice: str = Field(description="The advice id (ad_…)")
    verdict: Verdict
    why: str = Field(default="", max_length=300, description="Why, in his words")


class RecordInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str = Field(default="", max_length=40, description="Only this domain; empty: all")


def advice_tools(keeper: AdviceKeeper) -> list[Tool]:
    def origin(ctx: ToolContext) -> Origin:
        return Origin(
            actor="orion:core",
            source=f"thread:{ctx.thread_id}",
            trust=ctx.context_trust,
            derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
            correlation_id=ctx.correlation_id,
        )

    async def give(args: Any, ctx: ToolContext) -> ToolResult:
        today = keeper.clock().date()
        follow_up = args.try_on + timedelta(days=1) if args.try_on else None
        follow_up = follow_up or today + timedelta(days=keeper.settings.default_days)
        advice_id = await keeper.give(args.what, args.domain, follow_up, origin(ctx))
        return ToolResult(
            f"Conseil noté ({advice_id}). Tu lui demanderas ce qu'il en a pensé le "
            f"{_DAYS[follow_up.weekday()]} {follow_up:%d/%m}."
        )

    async def verdict(args: Any, ctx: ToolContext) -> ToolResult:
        await keeper.sync()
        advice = keeper.book.items.get(args.advice)
        if advice is None:
            return ToolResult(f"No advice {args.advice}.", is_error=True)
        await keeper.judge(advice, args.verdict, args.why, origin(ctx))
        return ToolResult(f"Noté. {keeper.record(advice.domain).splitlines()[0]}")

    async def record(args: Any, _ctx: ToolContext) -> ToolResult:
        await keeper.sync()
        return ToolResult(keeper.record(args.domain))

    return [
        Tool(
            name="advice_give",
            description="Note a recommendation you just made that he may act on (a restaurant, "
            "a book, something to buy, a way to answer someone), to ask later how it went.",
            input_model=GiveInput,
            handler=give,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
        Tool(
            name="advice_verdict",
            description="What he thought of something you recommended, when he tells you.",
            input_model=VerdictInput,
            handler=verdict,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
        Tool(
            name="advice_record",
            description="Your record as an adviser, in a domain: what you recommended before "
            "and what he thought. Look before recommending again there.",
            input_model=RecordInput,
            handler=record,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        ),
    ]


class AdviceModule(Module):
    name = "advice"
    instructions = "advice.md"
    settings_model = AdviceSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.keeper: AdviceKeeper | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        self.keeper = AdviceKeeper(
            plate.journal, plate.memory, self.settings, self.config.agent.timezone
        )
        await self.keeper.sync()
        return [
            grouped(
                "advice",
                "Your own recommendations to the owner: note them, ask how they went, and look "
                "at your record before recommending again.",
                advice_tools(self.keeper),
                {"advice_give": "give", "advice_verdict": "verdict", "advice_record": "record"},
            )
        ]

    async def serve(self, daemon: Daemon) -> None:
        if self.keeper is not None:
            daemon.alerts.append(self.keeper)
