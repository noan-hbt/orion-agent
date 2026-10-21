"""Missions as a module: `[modules.missions]` in orion.toml (on by default)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import structlog
from pydantic import BaseModel, ConfigDict, Field

from orion.cognition.loop import StopReason
from orion.core.events import Provenance
from orion.initiative.engine import Alert
from orion.missions.service import Mission, Missions
from orion.modules.base import Daemon, Module, ModuleSettings, Planned, Plate
from orion.runtime import Orion, is_silence
from orion.tools.actions import grouped
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig

log = structlog.get_logger(__name__)

REPORT_EXPIRES = timedelta(days=2)


class MissionSettings(ModuleSettings):
    enabled: bool = True
    tick_minutes: int = Field(default=10, ge=1, description="How often due missions are looked at")
    max_active: int = Field(default=10, ge=1, description="Missions running at the same time")


class MissionWork:
    """Runs due missions, each in its own thread, and offers what they found as alerts."""

    def __init__(
        self, missions: Missions, orion: Orion, clock: Callable[[], datetime], tick: float
    ) -> None:
        self._missions = missions
        self._orion = orion
        self._clock = clock
        self._tick = tick
        self._woken = asyncio.Event()
        missions.waking.append(self._woken.set)

    async def run(self) -> None:
        while True:
            await self.once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._woken.wait(), self._tick)
            self._woken.clear()

    async def once(self) -> list[str]:
        """Work on every mission that is due; returns those worked on."""
        now = self._clock()
        done: list[str] = []
        for mission in await self._missions.active():
            if mission.due(now):
                await self.work(mission, now)
                done.append(mission.id)
        return done

    async def work(self, mission: Mission, now: datetime) -> str:
        async def closed() -> bool:
            current = await self._missions.get(mission.id)
            return current is None or current.closed is not None

        rushes_seen = len(mission.rushes)
        turn = await self._orion.handle_initiative(
            mission.thread, mission.brief(), "mission", mission.trust, now=now, halted=closed
        )
        if turn.reason is StopReason.HALTED:
            log.info("mission.called_off", mission=mission.id, steps=turn.steps)
            return ""
        text = turn.text.strip()
        silent = is_silence(text) or (turn.reason is not StopReason.COMPLETED and not text)
        # What it says is only as trustworthy as what the run read.
        trust = self._orion.threads.get(mission.thread).window_trust()
        await self._missions.ran(mission, now, "" if silent else text, trust, rushes_seen)
        log.info("mission.ran", mission=mission.id, silent=silent, steps=turn.steps)
        return "" if silent else text

    async def alerts(self, now: datetime) -> list[Alert]:
        found: list[Alert] = []
        for mission in await self._missions.active():
            for run, at, said, trust in mission.reports:
                if now - at > REPORT_EXPIRES:
                    continue
                found.append(
                    Alert(
                        key=f"mission:{mission.id}:{run}",
                        notice=f"Ta mission « {mission.goal} » ({mission.id}) a du neuf :\n{said}",
                        why="mission",
                        fallback=said,
                        trust=trust,
                        expires_at=at + REPORT_EXPIRES,
                        importance=0.5,
                    )
                )
        return found


class StartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=5, max_length=300, description="What to achieve, in a sentence")
    criteria: str = Field(
        default="",
        max_length=1000,
        description="What counts: budget, place, dates, must-haves, deal-breakers",
    )
    every_hours: int = Field(default=24, ge=1, le=24 * 14, description="How often to work on it")


class UpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission: str = Field(description="The mission id (m_…)")
    criteria: str = Field(default="", max_length=1000, description="The new criteria, in full")
    every_hours: int = Field(default=0, ge=0, le=24 * 14)


class NoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission: str = Field(default="", description="The mission id; default: this mission")
    note: str = Field(min_length=3, max_length=500, description="What you learnt, for next time")


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(
        min_length=3, max_length=500, description="Its address (URL) or reference, in full"
    )
    label: str = Field(default="", max_length=200, description="A few words: price, place…")


class SeenInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission: str = Field(default="", description="The mission id; default: this mission")
    items: list[Item] = Field(min_length=1, max_length=50)


class RejectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission: str
    key: str = Field(description="The item's address or reference, as seen")
    why: str = Field(default="", max_length=300, description="The owner's reason, in their words")


class CloseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission: str
    outcome: str = Field(max_length=300, description="Done, found, given up…, and why")


class NowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission: str = Field(default="", description="The mission id; default: this mission")


class ListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _which(args_mission: str, ctx: ToolContext) -> str:
    if args_mission:
        return args_mission
    if ctx.thread_id.startswith("mission:"):
        return ctx.thread_id.removeprefix("mission:")
    return ""


def mission_tools(missions: Missions, clock: Callable[[], datetime], max_active: int) -> list[Tool]:
    def provenance(ctx: ToolContext) -> Provenance:
        return Provenance(
            source=f"thread:{ctx.thread_id}",
            actor="orion:core",
            derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
        )

    async def start(args: Any, ctx: ToolContext) -> ToolResult:
        if len(await missions.active()) >= max_active:
            return ToolResult(f"Already {max_active} missions: close one first.", is_error=True)
        mission_id = await missions.start(
            args.goal, args.criteria, args.every_hours, clock(), provenance(ctx), ctx.correlation_id
        )
        return ToolResult(
            f"Mission lancée ({mission_id}) : j'y travaille maintenant, puis toutes les "
            f"{args.every_hours} h. Je ne t'écris que s'il y a du neuf."
        )

    async def update(args: Any, ctx: ToolContext) -> ToolResult:
        if await missions.get(args.mission) is None:
            return ToolResult(f"No mission {args.mission}.", is_error=True)
        await missions.update(
            args.mission,
            provenance(ctx),
            ctx.context_trust,
            ctx.correlation_id,
            criteria=args.criteria,
            every_hours=args.every_hours,
        )
        return ToolResult("Mission mise à jour.")

    async def note(args: Any, ctx: ToolContext) -> ToolResult:
        mission_id = _which(args.mission, ctx)
        if await missions.get(mission_id) is None:
            return ToolResult(f"No mission {mission_id or '(none given)'}.", is_error=True)
        await missions.update(
            mission_id, provenance(ctx), ctx.context_trust, ctx.correlation_id, note=args.note
        )
        return ToolResult("Noté pour les prochains passages.")

    async def seen(args: Any, ctx: ToolContext) -> ToolResult:
        mission_id = _which(args.mission, ctx)
        try:
            fresh, refused = await missions.see(
                mission_id,
                [(i.key, i.label) for i in args.items],
                provenance(ctx),
                ctx.context_trust,
                ctx.correlation_id,
            )
        except KeyError:
            return ToolResult(f"No mission {mission_id or '(none given)'}.", is_error=True)
        lines = [f"Neuf ({len(fresh)}) :", *(f"- {k} {label}" for k, label in fresh)]
        if refused:
            lines.append(f"Déjà refusé par le propriétaire : {', '.join(refused)}")
        if not fresh:
            lines = ["Rien de neuf : tout a déjà été vu."]
        return ToolResult("\n".join(lines))

    async def reject(args: Any, ctx: ToolContext) -> ToolResult:
        if await missions.get(args.mission) is None:
            return ToolResult(f"No mission {args.mission}.", is_error=True)
        await missions.reject(args.mission, args.key, args.why, provenance(ctx), ctx.correlation_id)
        return ToolResult("Écarté ; la mission en tiendra compte.")

    async def close(args: Any, ctx: ToolContext) -> ToolResult:
        if await missions.get(args.mission) is None:
            return ToolResult(f"No mission {args.mission}.", is_error=True)
        await missions.close(args.mission, args.outcome, provenance(ctx), ctx.correlation_id)
        return ToolResult("Mission close.")

    async def now(args: Any, ctx: ToolContext) -> ToolResult:
        mission_id = _which(args.mission, ctx)
        mission = await missions.get(mission_id)
        if mission is None or mission.closed is not None:
            return ToolResult(f"No mission {mission_id or '(none given)'} running.", is_error=True)
        if not mission.rushes_left(clock()):
            return ToolResult(
                "Plus de passage immédiat possible aujourd'hui pour cette mission : le prochain "
                f"vient à son rythme (toutes les {mission.every_hours} h).",
                is_error=True,
            )
        left = await missions.rush(
            mission_id, clock(), provenance(ctx), ctx.context_trust, ctx.correlation_id
        )
        when = "juste après celui-ci" if ctx.thread_id == mission.thread else "tout de suite"
        return ToolResult(f"Un passage repart {when} (encore {left} possible(s) aujourd'hui).")

    async def listing(_args: Any, _ctx: ToolContext) -> ToolResult:
        active = await missions.active()
        if not active:
            return ToolResult("Aucune mission en cours.")
        return ToolResult(
            "\n".join(
                f"- ({m.id}) {m.goal} : {m.runs} passage(s), {len(m.seen)} vu(s), "
                f"{len(m.rejected)} écarté(s), toutes les {m.every_hours} h"
                for m in active
            )
        )

    def tool(name: str, description: str, model: type[BaseModel], handler: Any, **kw: Any) -> Tool:
        return Tool(
            name=name,
            description=description,
            input_model=model,
            handler=handler,
            effect=kw.pop("effect", Effect.INTERNAL),
            reversibility=Reversibility.REVERSIBLE,
            **kw,
        )

    return [
        tool(
            "mission_start",
            "Start a mission: a goal you keep working on alone over days (find a flat, watch a "
            "price, look for a slot), instead of answering once. You work on it now, then "
            "every `every_hours`, and write to the owner only when there is something new.",
            StartInput,
            start,
        ),
        tool(
            "mission_update",
            "Change a mission's criteria or pace, when the owner says so.",
            UpdateInput,
            update,
        ),
        tool(
            "mission_note",
            "During a mission: note what you learnt (a site that works, a dead end) for next time.",
            NoteInput,
            note,
            accepts_untrusted_context=True,
        ),
        tool(
            "mission_seen",
            "During a mission: record the items you found (each with its address); it tells you "
            "which are new, so you never report the same one twice.",
            SeenInput,
            seen,
            accepts_untrusted_context=True,
        ),
        tool(
            "mission_reject",
            "The owner turns down an item of a mission: record it with their reason, so the "
            "next runs leave it and its like out.",
            RejectInput,
            reject,
            accepts_untrusted_context=True,
        ),
        tool(
            "mission_close",
            "End a mission (done, found, no longer wanted).",
            CloseInput,
            close,
            accepts_untrusted_context=True,
        ),
        tool(
            "mission_now",
            "Another pass right away: he wants it now, or this pass has more to see. The only "
            "way to keep working once you have answered.",
            NowInput,
            now,
            accepts_untrusted_context=True,
        ),
        tool(
            "missions",
            "The missions in progress.",
            ListInput,
            listing,
            effect=Effect.NONE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        ),
    ]


class MissionsModule(Module):
    name = "missions"
    instructions = "missions.md"
    settings_model = MissionSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.missions: Missions | None = None

    def _clock(self) -> datetime:
        return datetime.now(ZoneInfo(self.config.agent.timezone))

    async def assemble(self, plate: Plate) -> list[Tool]:
        settings: MissionSettings = self.settings
        self.missions = Missions(plate.journal)
        await self.missions.sync()
        return [
            grouped(
                "mission",
                "Missions: goals you keep working on alone over days (find a flat, watch a price, "
                "look for a slot), when the owner wants you to keep looking. For a question to "
                "answer now, search and answer instead (and offer a mission if it fits).",
                mission_tools(self.missions, self._clock, settings.max_active),
                {
                    "mission_start": "start",
                    "mission_update": "update",
                    "mission_note": "note",
                    "mission_seen": "seen",
                    "mission_reject": "reject",
                    "mission_close": "close",
                    "mission_now": "now",
                    "missions": "list",
                },
            )
        ]

    async def planned(self, now: datetime, until: datetime) -> list[Planned]:
        if self.missions is None:
            return []
        found: list[Planned] = []
        for mission in await self.missions.active():
            at = (mission.last_run or mission.started_at) + timedelta(hours=mission.every_hours)
            at = max(at, now)
            if at < until:
                found.append(
                    Planned(
                        at,
                        f"reprendre la mission : {mission.goal}",
                        f"mission (close, {mission.id})",
                    )
                )
        return found

    async def serve(self, daemon: Daemon) -> None:
        if self.missions is None:  # pragma: no cover - assemble always runs first
            return
        settings: MissionSettings = self.settings
        work = MissionWork(self.missions, daemon.orion, self._clock, settings.tick_minutes * 60.0)
        daemon.alerts.append(work)
        daemon.add_service("missions.work", work.run)
