"""Intentions: `[modules.intentions]` in orion.toml (on by default).

Commitments are tasks (« je dois appeler Paul »). Intentions are what the owner wants to
become: « courir trois fois par semaine », « dépenser moins en restos », « voir plus ma mère ».
They last, and nobody keeps them by being reminded at a fixed time. So Orion:

- keeps each one in memory, as part of who the owner is (a profile fact): it is in view at the
  start of every conversation, and recalled when a message touches it, so Orion can say « tu
  m'avais dit vouloir… » at the moment it matters;
- notes what happens (`intention_note`: « j'ai couru ce matin », « encore un resto ») when the
  owner mentions it;
- takes stock with the owner every week (or the pace they chose): progress said simply, a
  drift said frankly, with tact, and a question about what gets in the way. Through the
  initiative engine, like any message Orion starts.

When what the owner is about to do goes against one (a night out before the exam he said
mattered), Orion says so once, without a lecture, and lets him decide (`intention` challenge,
D66): the challenge is journaled, at most one a day per intention, so Orion never nags, and the
weekly check-in knows when he chose otherwise.

    intention.set  intention.noted  intention.challenged  intention.dropped
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any
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

SET = "intention.set"
NOTED = "intention.noted"
DROPPED = "intention.dropped"
CHALLENGED = "intention.challenged"
CHALLENGE_EVERY = timedelta(hours=20)
CHECK_WINDOW = timedelta(hours=20)
_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


class IntentionSettings(ModuleSettings):
    enabled: bool = True
    check_at: time = Field(default=time(18, 30), description="Local time of the weekly check-in")


@dataclass(frozen=True)
class Note:
    happened: str
    kept: bool
    at: datetime


@dataclass(frozen=True)
class Challenge:
    plan: str
    at: datetime


@dataclass
class Intention:
    id: str
    what: str
    why: str
    per_week: int
    every_days: int
    since: datetime
    fact_id: str
    trust: Trust
    notes: list[Note] = field(default_factory=list[Note])
    challenges: list[Challenge] = field(default_factory=list[Challenge])

    def check_ins(self, now: datetime, at: time) -> tuple[int, datetime] | None:
        """The check-in due now, if one is: its number and when it was due."""
        first = datetime.combine(
            self.since.date() + timedelta(days=self.every_days), at, self.since.tzinfo
        )
        if now < first:
            return None
        number = (now - first).days // self.every_days
        due = first + timedelta(days=number * self.every_days)
        return (number, due) if now < due + CHECK_WINDOW else None

    def since_last(self, now: datetime) -> list[Note]:
        start = now - timedelta(days=self.every_days)
        return [n for n in self.notes if n.at >= start]

    def last_kept(self) -> datetime | None:
        kept = [n.at for n in self.notes if n.kept]
        return max(kept) if kept else None


@dataclass
class IntentionState:
    name: str = "intentions"
    active: dict[str, Intention] = field(default_factory=dict[str, Intention])
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.active.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind == SET:
            intention = Intention(
                str(payload["intention_id"]),
                str(payload["what"]),
                str(payload.get("why", "")),
                int(payload.get("per_week", 0)),
                int(payload.get("every_days", 7)),
                datetime.fromisoformat(str(payload["since"])),
                str(payload.get("fact_id", "")),
                event.trust,
            )
            self.active[intention.id] = intention
        elif event.kind == NOTED:
            intention = self.active.get(str(payload["intention_id"]))
            if intention is not None:
                intention.notes.append(
                    Note(
                        str(payload["happened"]),
                        bool(payload["kept"]),
                        datetime.fromisoformat(str(payload["at"])),
                    )
                )
                intention.trust = min(intention.trust, event.trust)
        elif event.kind == CHALLENGED:
            intention = self.active.get(str(payload["intention_id"]))
            if intention is not None:
                intention.challenges.append(
                    Challenge(str(payload["plan"]), datetime.fromisoformat(str(payload["at"])))
                )
        elif event.kind == DROPPED:
            self.active.pop(str(payload["intention_id"]), None)


class Intentions:
    def __init__(
        self,
        journal: EventStore,
        memory: MemoryService | None,
        settings: IntentionSettings,
        timezone: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._journal = journal
        self._memory = memory
        self.settings = settings
        self.zone = ZoneInfo(timezone)
        self.clock = clock or (lambda: datetime.now(self.zone))
        self.state = IntentionState()
        self._runner = ProjectionRunner(journal, [self.state])

    async def sync(self) -> None:
        await self._runner.catch_up()

    async def set(self, what: str, why: str, per_week: int, every_days: int, origin: Origin) -> str:
        intention_id = f"i_{new_id()}"
        fact_id = ""
        if self._memory is not None:
            target = f" ({per_week} fois par semaine)" if per_week else ""
            reason = f" (parce que : {why})" if why else ""
            fact_id = await self._memory.remember(
                f"Intention que le propriétaire veut tenir, suivie ({intention_id}) : "
                f"{what}{target}{reason}.",
                origin,
                about=["owner"],
                category=Category.PROFILE,
            )
        await self._append(
            SET,
            {
                "intention_id": intention_id,
                "what": what,
                "why": why,
                "per_week": per_week,
                "every_days": every_days,
                "since": self.clock().isoformat(),
                "fact_id": fact_id,
            },
            origin,
        )
        return intention_id

    async def note(self, intention_id: str, happened: str, kept: bool, origin: Origin) -> None:
        await self._append(
            NOTED,
            {
                "intention_id": intention_id,
                "happened": happened,
                "kept": kept,
                "at": self.clock().isoformat(),
            },
            origin,
        )

    async def challenge(self, intention: Intention, plan: str, origin: Origin) -> bool:
        """Say it once: False if Orion already did today about this intention."""
        now = self.clock()
        if any(now - c.at < CHALLENGE_EVERY for c in intention.challenges):
            return False
        await self._append(
            CHALLENGED,
            {"intention_id": intention.id, "plan": plan, "at": now.isoformat()},
            origin,
        )
        return True

    async def drop(self, intention: Intention, why: str, origin: Origin) -> None:
        if self._memory is not None and intention.fact_id:
            await self._memory.invalidate(
                intention.fact_id, f"intention abandonnée : {why}", origin
            )
        await self._append(DROPPED, {"intention_id": intention.id, "why": why}, origin)

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

    def progress(self, intention: Intention, now: datetime) -> str:
        recent = intention.since_last(now)
        kept = sum(1 for n in recent if n.kept)
        missed = len(recent) - kept
        parts = [f"ces {intention.every_days} derniers jours : {kept} fois tenu"]
        if missed:
            parts.append(f"{missed} écart(s)")
        if intention.per_week:
            goal = intention.per_week * intention.every_days / 7
            parts.append(f"objectif {goal:g}")
        last = intention.last_kept()
        if last is None:
            parts.append("jamais noté comme tenu")
        else:
            parts.append(f"dernière fois le {_DAYS[last.weekday()]} {last:%d/%m}")
        return ", ".join(parts)

    async def alerts(self, now: datetime) -> list[Alert]:
        await self.sync()
        found: list[Alert] = []
        for intention in self.state.active.values():
            due = intention.check_ins(now, self.settings.check_at)
            if due is None:
                continue
            number, at = due
            recent = intention.since_last(now)
            lines = "\n".join(
                f"- {_DAYS[n.at.weekday()]} {n.at:%d/%m} : {'tenu' if n.kept else 'écart'}, "
                f"{n.happened}"
                for n in recent
            )
            why = f" (ce qui compte pour lui : {intention.why})" if intention.why else ""
            start = now - timedelta(days=intention.every_days)
            pushed = [c for c in intention.challenges if c.at >= start]
            if pushed:
                lines += "\nTu lui as fait remarquer que ça allait contre :\n" + "\n".join(
                    f"- {_DAYS[c.at.weekday()]} {c.at:%d/%m} : {c.plan}" for c in pushed
                )
            found.append(
                Alert(
                    key=f"intention:{intention.id}:{number}",
                    notice="Fais le point avec le propriétaire sur son intention "
                    f"« {intention.what} »{why}. "
                    f"Où il en est, {self.progress(intention, now)}.\n"
                    f"{lines or '(rien noté sur cette période)'}\n"
                    "Si ça avance, dis-le simplement. Si ça décroche, dis-le franchement, avec "
                    "tact et sans morale, et demande ce qui coince. Si rien n'a été noté, "
                    "demande-lui où il en est. Deux phrases, comme un ami qui s'en souvient.",
                    why="son intention",
                    fallback=f"Où tu en es de « {intention.what} » ?",
                    trust=intention.trust,
                    expires_at=at + CHECK_WINDOW,
                    importance=0.45,
                )
            )
        return found


class SetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what: str = Field(
        min_length=3,
        max_length=200,
        description="An infinitive phrase in the owner's words: 'courir trois fois par semaine'",
    )
    why: str = Field(default="", max_length=300, description="Why it matters to him, if said")
    per_week: int = Field(default=0, ge=0, le=21, description="Times a week, if it is countable")
    every_days: int = Field(default=7, ge=1, le=60, description="How often to take stock")


class NoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intention: str = Field(description="The intention id (i_…)")
    happened: str = Field(min_length=2, max_length=300, description="What he did, in a few words")
    kept: bool = Field(description="True if it goes the way he wants, false for a lapse")


class ChallengeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intention: str = Field(description="The intention id (i_…) it goes against")
    plan: str = Field(
        min_length=3, max_length=300, description="What he is about to do, in a few words"
    )


class DropInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intention: str
    why: str = Field(max_length=300, description="Reached, given up, changed: his reason")


class ListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def intention_tools(intentions: Intentions) -> list[Tool]:
    def origin(ctx: ToolContext) -> Origin:
        return Origin(
            actor="orion:core",
            source=f"thread:{ctx.thread_id}",
            trust=ctx.context_trust,
            derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
            correlation_id=ctx.correlation_id,
        )

    async def set_(args: Any, ctx: ToolContext) -> ToolResult:
        intention_id = await intentions.set(
            args.what, args.why, args.per_week, args.every_days, origin(ctx)
        )
        return ToolResult(
            f"Intention suivie ({intention_id}). Tu feras le point avec lui dans "
            f"{args.every_days} jours, puis au même rythme."
        )

    async def note(args: Any, ctx: ToolContext) -> ToolResult:
        await intentions.sync()
        intention = intentions.state.active.get(args.intention)
        if intention is None:
            return ToolResult(f"No intention {args.intention}.", is_error=True)
        await intentions.note(intention.id, args.happened, args.kept, origin(ctx))
        return ToolResult(f"Noté. {intentions.progress(intention, intentions.clock())}.")

    async def challenge(args: Any, ctx: ToolContext) -> ToolResult:
        await intentions.sync()
        intention = intentions.state.active.get(args.intention)
        if intention is None:
            return ToolResult(f"No intention {args.intention}.", is_error=True)
        if not await intentions.challenge(intention, args.plan, origin(ctx)):
            return ToolResult(
                "Tu le lui as déjà fait remarquer aujourd'hui pour cette intention : ne "
                "reviens pas dessus, c'est sa décision."
            )
        why = f" (ce qui compte pour lui : {intention.why})" if intention.why else ""
        return ToolResult(
            f"Dis-le-lui une fois, en une ou deux phrases, sans morale : ça va contre "
            f"« {intention.what} »{why}. Puis laisse-le décider, et aide-le quoi qu'il choisisse."
        )

    async def drop(args: Any, ctx: ToolContext) -> ToolResult:
        await intentions.sync()
        intention = intentions.state.active.get(args.intention)
        if intention is None:
            return ToolResult(f"No intention {args.intention}.", is_error=True)
        await intentions.drop(intention, args.why, origin(ctx))
        return ToolResult("Intention retirée.")

    async def listing(_args: Any, _ctx: ToolContext) -> ToolResult:
        await intentions.sync()
        now = intentions.clock()
        lines = [
            f"- ({i.id}) {i.what} : {intentions.progress(i, now)}"
            for i in intentions.state.active.values()
        ]
        return ToolResult("\n".join(lines) or "Aucune intention suivie.")

    return [
        Tool(
            name="intention_set",
            description="Follow something the owner wants to become or do lastingly (a habit, "
            "a way of living: run more, spend less on restaurants, see his mother more), when he "
            "says so or agrees you follow it. Not for one-off tasks (those are commitments).",
            input_model=SetInput,
            handler=set_,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
        Tool(
            name="intention_note",
            description="Note what happened about an intention, when he mentions it (« j'ai "
            "couru ce matin »: kept; « encore un resto »: a lapse).",
            input_model=NoteInput,
            handler=note,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
        Tool(
            name="intention_challenge",
            description="What he is about to do goes against one of his intentions (a night "
            "out before the exam he said mattered, a purchase after « je mets de côté »): call "
            "it before saying so, once.",
            input_model=ChallengeInput,
            handler=challenge,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
        Tool(
            name="intention_drop",
            description="Stop following an intention (reached, given up, or he asks).",
            input_model=DropInput,
            handler=drop,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
        Tool(
            name="intentions",
            description="The intentions you follow, with how they are going.",
            input_model=ListInput,
            handler=listing,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        ),
    ]


class IntentionsModule(Module):
    name = "intentions"
    instructions = "intentions.md"
    settings_model = IntentionSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.intentions: Intentions | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        self.intentions = Intentions(
            plate.journal, plate.memory, self.settings, self.config.agent.timezone
        )
        await self.intentions.sync()
        return [
            grouped(
                "intention",
                "The owner's intentions: what he wants to become or do lastingly (run more, "
                "spend less, see his mother more), not one-off tasks.",
                intention_tools(self.intentions),
                {
                    "intention_set": "set",
                    "intention_note": "note",
                    "intention_challenge": "challenge",
                    "intention_drop": "drop",
                    "intentions": "list",
                },
            )
        ]

    async def serve(self, daemon: Daemon) -> None:
        if self.intentions is not None:
            daemon.alerts.append(self.intentions)
