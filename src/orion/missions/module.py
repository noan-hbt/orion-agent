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
from orion.core.events import Provenance, Trust
from orion.guardian.policy import TRUSTED_FLOOR
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
                try:
                    await self.work(mission, now)
                except Exception:
                    # One mission that breaks does not stop the others; it is tried again at
                    # its next pass.
                    log.exception("missions.pass_failed", mission=mission.id)
                    continue
                done.append(mission.id)
        return done

    async def work(self, mission: Mission, now: datetime) -> str:
        async def closed() -> bool:
            current = await self._missions.get(mission.id)
            return current is None or current.closed is not None

        rushes_seen = len(mission.rushes)
        talks = ""
        if mission.people and self._missions.talks is not None:
            talks = await self._missions.talks(mission.people)
        # What they wrote is theirs: the pass is never more trusted than it.
        trust = min(mission.trust, Trust.THIRD_PARTY) if talks else mission.trust
        turn = await self._orion.handle_initiative(
            mission.thread, mission.brief(talks), "mission", trust, now=now, halted=closed
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

    goal: str = Field(min_length=5, max_length=300, description="In a sentence")
    criteria: str = Field(
        default="",
        max_length=1000,
        description="Budget, place, dates, must-haves, deal-breakers",
    )
    every_hours: int = Field(default=24, ge=1, le=24 * 14, description="Pace")
    people: list[str] = Field(
        default_factory=list[str],
        max_length=10,
        description="A project he shares with some of his contacts: their names. Each pass "
        "moves it forward with them",
    )
    mandate: str = Field(
        default="",
        max_length=1000,
        description="With people: what you may settle with them for him, in his words",
    )


class UpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission: str = Field(description="m_…")
    criteria: str = Field(default="", max_length=1000, description="In full")
    every_hours: int = Field(default=0, ge=0, le=24 * 14)
    people: list[str] | None = Field(default=None, max_length=10, description="In full")
    mandate: str | None = Field(default=None, max_length=1000, description="In full")


class NoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission: str = Field(default="", description="Default: this mission")
    note: str = Field(min_length=3, max_length=500, description="For next time")


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(
        min_length=3, max_length=500, description="Its address (URL) or reference, in full"
    )
    label: str = Field(default="", max_length=200, description="Price, place…")


class SeenInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission: str = Field(default="", description="Default: this mission")
    items: list[Item] = Field(min_length=1, max_length=50)


class RejectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission: str
    key: str = Field(description="As seen")
    why: str = Field(default="", max_length=300, description="His words")


class CloseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission: str
    outcome: str = Field(max_length=300, description="And why")


class NowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission: str = Field(default="", description="Default: this mission")


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

    def unknown(people: list[str] | None) -> ToolResult | None:
        if not people:
            return None
        known = missions.people() if missions.people is not None else []
        missing = [p for p in people if p.lower() not in (k.lower() for k in known)]
        if not missing:
            return None
        return ToolResult(
            f"Not among his contacts: {', '.join(missing)} (contacts: "
            f"{', '.join(known) or 'none, or the secretary is off'}). A project is shared only "
            "with people allowed to write to you: offer to add them first.",
            is_error=True,
        )

    async def start(args: Any, ctx: ToolContext) -> ToolResult:
        if len(await missions.active()) >= max_active:
            return ToolResult(f"Already {max_active} missions: close one first.", is_error=True)
        if (refused := unknown(args.people)) is not None:
            return refused
        mission_id = await missions.start(
            args.goal,
            args.criteria,
            args.every_hours,
            clock(),
            provenance(ctx),
            ctx.correlation_id,
            people=args.people,
            mandate=args.mandate,
        )
        if args.people:
            return ToolResult(
                f"Projet lancé ({mission_id}) avec {', '.join(args.people)} : j'y travaille "
                f"maintenant, puis toutes les {args.every_hours} h, et quand ils m'écrivent. "
                "Pour que tu leur écrives sans qu'il valide chaque message, il doit te le "
                "laisser (`autonomy_grant`, `contact_message`, recipients = ces noms) : "
                "propose-le-lui s'il ne l'a pas fait."
            )
        return ToolResult(
            f"Mission lancée ({mission_id}) : j'y travaille maintenant, puis toutes les "
            f"{args.every_hours} h. Je ne t'écris que s'il y a du neuf."
        )

    async def update(args: Any, ctx: ToolContext) -> ToolResult:
        if await missions.get(args.mission) is None:
            return ToolResult(f"No mission {args.mission}.", is_error=True)
        if (refused := unknown(args.people)) is not None:
            return refused
        await missions.update(
            args.mission,
            provenance(ctx),
            ctx.context_trust,
            ctx.correlation_id,
            criteria=args.criteria,
            every_hours=args.every_hours,
            people=args.people,
            mandate=args.mandate,
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
        await missions.reject(
            args.mission, args.key, args.why, provenance(ctx), ctx.correlation_id, ctx.context_trust
        )
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
                + (f" ; {m.project()}" if m.people else "")
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
            "Now, then every `every_hours`; you write to him only when there is news.",
            StartInput,
            start,
        ),
        tool(
            "mission_update",
            "New criteria, pace, people or mandate, when he says so.",
            UpdateInput,
            update,
        ),
        tool(
            "mission_note",
            "What you learnt (a site that works, a dead end), for next time.",
            NoteInput,
            note,
            accepts_untrusted_context=True,
        ),
        tool(
            "mission_seen",
            "Record the items found; it says which are new.",
            SeenInput,
            seen,
            accepts_untrusted_context=True,
        ),
        tool(
            "mission_reject",
            "He turns an item down: record it with his reason.",
            RejectInput,
            reject,
            accepts_untrusted_context=True,
        ),
        tool(
            "mission_close",
            "End a mission.",
            CloseInput,
            close,
            accepts_untrusted_context=True,
        ),
        tool(
            "mission_now",
            "Another pass right away: he wants it now, or this pass has more to see.",
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
                "look for a slot), when the owner wants you to keep looking, or a project he "
                "shares with some of his contacts, that you move forward with them (`people`, "
                "`mandate`). For a question to answer now, search and answer instead (and offer "
                "a mission if it fits).",
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

    async def situation(self, now: datetime) -> list[str]:
        """What he asked Orion to follow over days (D146)."""
        if self.missions is None:
            return []
        return [f"- tu suis pour lui : {m.goal}" for m in await self.missions.active()]

    async def review(self, now: datetime) -> list[str]:
        if self.missions is None:
            return []
        return [
            f"- ({m.id}) {m.goal} — critères : {m.criteria or '(aucun)'} ; tous les "
            f"{m.every_hours} h, {m.runs} passes"
            # What a page said (in the notes) never steers the review: the goal is his.
            + (f" ; dernière note : {m.notes[-1]}" if m.notes and m.trust >= TRUSTED_FLOOR else "")
            for m in await self.missions.active()
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
        daemon.orion.situation.append(self)
        daemon.add_service("missions.work", work.run)
