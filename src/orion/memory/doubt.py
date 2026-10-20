"""A memory that doubts: `[modules.doubt]` in orion.toml (on by default).

Facts are dated, but none of them ages: « cherche un appart à Dreux » stays true forever, and one
day Orion says something that was true in March. Some facts change often (where he lives, his
work, his projects, what he is looking for, who he is with); others hardly ever (his birthday,
his tastes). This module tells them apart, with plain rules on the words and the category, and
gives the first ones a shelf life (D83):

- past it, a fact is « to check »: `doubt` (stale) lists them, and Orion says « en mars, tu
  cherchais… » rather than stating it as today's truth;
- now and then (once a week at most, at a quiet hour), Orion asks about the oldest one, in
  passing: « tu cherches toujours à Dreux ? ». His answer confirms it (`doubt` confirm: it is
  fresh again) or corrects it (`forget_fact`, then `remember` the new version);
- once asked (the initiative engine journals what it delivered), a fact is not asked again for
  three months.

Nothing here changes a fact: confirming is an event of its own, so memory stays rebuildable.

    doubt.confirmed
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.hlc import Timestamp
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.initiative.engine import DELIVERED, Alert
from orion.memory.model import FACT_ASSERTED, FACT_INVALIDATED, Category
from orion.modules.base import Daemon, Module, ModuleSettings, Plate
from orion.tools.actions import grouped
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig

CONFIRMED = "doubt.confirmed"
KEY = "doubt:"

ASK_AGAIN_AFTER = timedelta(days=90)
ASK_WINDOW = timedelta(hours=3)
_MONTHS = [
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
]
CHANGING = (
    "cherche",
    "recherche",
    "habite",
    "vit",
    "loge",
    "demenage",
    "travaille",
    "bosse",
    "poste",
    "stage",
    "alternance",
    "etudie",
    "etudiant",
    "master",
    "licence",
    "couple",
    "celibataire",
    "copine",
    "copain",
    "sort",
    "prepare",
    "projet",
    "veut",
    "voudrait",
    "essaie",
    "compte",
    "envisage",
    "hesite",
    "attend",
    "enceinte",
    "malade",
    "blesse",
    "regime",
    "loyer",
    "salaire",
    "abonnement",
    "actuellement",
    "moment",
)
"""Words of a situation rather than a trait (without accents)."""
SHELF_LIFE = {
    Category.PROJECT: 60,
    Category.PROFILE: 120,
    Category.PERSON: 180,
    Category.OTHER: 120,
}
"""Days a changing fact stays fresh, by category. Preferences do not expire."""


def _words(text: str) -> set[str]:
    plain = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(c if c.isalnum() else " " for c in plain if not unicodedata.combining(c))
    return set(plain.split())


def shelf_life(statement: str, category: str) -> int | None:
    """Days before a fact is worth checking, or None: it does not go stale."""
    try:
        kind = Category(category)
    except ValueError:
        kind = Category.OTHER
    if kind is Category.PREFERENCE:
        return None
    if kind is Category.PROJECT or _words(statement) & set(CHANGING):
        return SHELF_LIFE[kind]
    return None


class DoubtSettings(ModuleSettings):
    enabled: bool = True
    at: time = Field(default=time(18, 30), description="When a question may be asked")
    every_days: int = Field(default=7, ge=1, le=60, description="At most one question per …")


@dataclass
class Known:
    fact_id: str
    statement: str
    since: datetime
    """When it was last said or confirmed."""
    life: int
    trust: Trust
    asked: datetime | None = None


@dataclass
class DoubtState:
    name: str = "doubt"
    facts: dict[str, Known] = field(default_factory=dict[str, Known])
    last_asked: datetime | None = None
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.facts.clear()
        self.last_asked = None
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        at = datetime.fromtimestamp(Timestamp.decode(event.hlc).wall_ms / 1000, UTC)
        if event.kind == FACT_ASSERTED:
            for old in payload.get("supersedes", []):
                self.facts.pop(str(old), None)
            statement = str(payload["statement"])
            life = shelf_life(statement, str(payload.get("category", "other")))
            if life is not None:
                fact_id = str(payload["fact_id"])
                self.facts[fact_id] = Known(fact_id, statement, at, life, event.trust)
        elif event.kind == FACT_INVALIDATED:
            self.facts.pop(str(payload["fact_id"]), None)
        elif event.kind == CONFIRMED:
            known = self.facts.get(str(payload["fact_id"]))
            if known is not None:
                known.since, known.asked = at, None
        elif event.kind == DELIVERED and str(payload.get("key", "")).startswith(KEY):
            asked = datetime.combine(date.fromisoformat(str(payload["day"])), time(12), UTC)
            known = self.facts.get(str(payload["key"]).split(":")[1])
            if known is not None:
                known.asked = asked
            self.last_asked = asked


class Doubt:
    def __init__(
        self,
        journal: EventStore,
        settings: DoubtSettings,
        timezone: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._journal = journal
        self.settings = settings
        self.zone = ZoneInfo(timezone)
        self.clock = clock or (lambda: datetime.now(self.zone))
        self.state = DoubtState()
        self._runner = ProjectionRunner(journal, [self.state])

    async def sync(self) -> None:
        await self._runner.catch_up()

    def stale(self, now: datetime) -> list[Known]:
        """Changing facts past their shelf life, oldest first."""
        found = [k for k in self.state.facts.values() if now - k.since > timedelta(days=k.life)]
        return sorted(found, key=lambda k: k.since)

    def when(self, known: Known) -> str:
        at = known.since.astimezone(self.zone)
        return f"en {_MONTHS[at.month - 1]}" + (
            f" {at.year}" if at.year != self.clock().year else ""
        )

    async def record(self, kind: str, fact_id: str, provenance: Provenance, trust: Trust) -> None:
        await self._journal.append(
            [NewEvent(kind=kind, payload={"fact_id": fact_id}, provenance=provenance, trust=trust)]
        )
        await self.sync()

    async def alerts(self, now: datetime) -> list[Alert]:
        await self.sync()
        last = self.state.last_asked
        if last is not None and now - last < timedelta(days=self.settings.every_days):
            return []
        start = datetime.combine(now.date(), self.settings.at, self.zone)
        if not start <= now < start + ASK_WINDOW:
            return []
        for known in self.stale(now):
            if known.asked is not None and now - known.asked < ASK_AGAIN_AFTER:
                continue
            return [
                Alert(
                    key=f"{KEY}{known.fact_id}:{now.date().isoformat()}",
                    notice=f"{self.when(known).capitalize()}, tu as noté : « {known.statement} » "
                    f"({known.fact_id}). C'est le genre de chose qui change, et ça fait un "
                    "moment. Demande-lui en une phrase légère, en passant, si c'est toujours le "
                    f"cas. S'il confirme : `doubt` (confirm, {known.fact_id}). Si ça a changé : "
                    "`forget_fact`, puis `remember` ce qui est vrai maintenant. Si vous en avez "
                    "parlé récemment, réponds SILENCE.",
                    why="je vérifie un truc que je sais de toi",
                    fallback=f"Au fait, {self.when(known)} tu m'avais dit : « {known.statement} ». "
                    "C'est toujours le cas ?",
                    trust=min(known.trust, Trust.SYSTEM),
                    expires_at=start + ASK_WINDOW,
                    importance=0.2,
                )
            ]
        return []


class ConfirmInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact: str = Field(description="The fact id (f_…) he says is still true")


class StaleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def doubt_tools(doubt: Doubt) -> list[Tool]:
    async def confirm(args: Any, ctx: ToolContext) -> ToolResult:
        await doubt.sync()
        if args.fact not in doubt.state.facts:
            return ToolResult(f"No changing fact {args.fact}.", is_error=True)
        provenance = Provenance(
            source=f"thread:{ctx.thread_id}",
            actor="orion:core",
            derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
        )
        await doubt.record(CONFIRMED, args.fact, provenance, ctx.context_trust)
        return ToolResult("Noté : c'est toujours vrai.")

    async def stale(_args: Any, _ctx: ToolContext) -> ToolResult:
        await doubt.sync()
        found = doubt.stale(doubt.clock())
        if not found:
            return ToolResult("Rien à vérifier : ce qui change est récent.")
        return ToolResult(
            "Peut-être plus vrai (dis « en mars, tu… » plutôt que de l'affirmer) :\n"
            + "\n".join(f"- ({k.fact_id}) {doubt.when(k)} : {k.statement}" for k in found[:20])
        )

    return [
        Tool(
            name="doubt_confirm",
            description="He says a fact you checked with him is still true: it is fresh again.",
            input_model=ConfirmInput,
            handler=confirm,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
        Tool(
            name="doubt_stale",
            description="What you know that changes often (where he lives, his work, his "
            "projects) and was said long ago: maybe no longer true.",
            input_model=StaleInput,
            handler=stale,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        ),
    ]


class DoubtModule(Module):
    name = "doubt"
    instructions = "doubt.md"
    settings_model = DoubtSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.doubt: Doubt | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        self.doubt = Doubt(plate.journal, self.settings, self.config.agent.timezone)
        await self.doubt.sync()
        return [
            grouped(
                "doubt",
                "What you know that may have changed since: list it, and note what he confirms.",
                doubt_tools(self.doubt),
                {"doubt_confirm": "confirm", "doubt_stale": "stale"},
            )
        ]

    async def serve(self, daemon: Daemon) -> None:
        if self.doubt is not None:
            daemon.alerts.append(self.doubt)
