"""Focus: `[modules.focus]` in orion.toml (on by default).

« Je bosse jusqu'à 18 h. » Until then, what Orion would have said unasked waits: the arbiter
lets through only what cannot wait (a meeting in ten minutes), and waiting does not make the
rest more urgent. When the focus ends, what piled up goes out as one message, most important
first (the engine joins messages due together). A contact who writes meanwhile, and may know
when the owner is free, is told he is focused until then (the secretary). Reminders the owner
asked for still ring: he set them.

    focus.started  (until, why)
    focus.ended    (early, or when it ran out: no event needed)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.initiative.arbiter import Moment
from orion.modules.base import Daemon, Module, ModuleSettings, Plate
from orion.tools.actions import grouped
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig

STARTED = "focus.started"
ENDED = "focus.ended"
MAX_FOCUS = timedelta(hours=14)
BREAK = timedelta(minutes=20)
"""After a focus, the held messages go out: the arbiter treats it as a break."""


class FocusSettings(ModuleSettings):
    enabled: bool = True


@dataclass(frozen=True)
class Focus:
    id: str
    since: datetime
    until: datetime
    why: str
    ended: datetime | None = None

    def active(self, now: datetime) -> bool:
        return self.ended is None and self.since <= now < self.until

    def end(self) -> datetime:
        return self.ended or self.until


class FocusState:
    name = "focus"

    def __init__(self) -> None:
        self.last: Focus | None = None
        self._seq = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.last = None
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind == STARTED:
            self.last = Focus(
                str(payload["focus_id"]),
                datetime.fromisoformat(str(payload["since"])),
                datetime.fromisoformat(str(payload["until"])),
                str(payload.get("why", "")),
            )
        elif event.kind == ENDED and self.last is not None and self.last.id == payload["focus_id"]:
            at = datetime.fromisoformat(str(payload["at"]))
            self.last = Focus(self.last.id, self.last.since, self.last.until, self.last.why, at)


def parse_until(given: str, now: datetime) -> datetime:
    """« 18:00 » (today, or tomorrow if already past), or a full ISO date and time."""
    hour, sep, minute = given.strip().lower().replace("h", ":").partition(":")
    try:
        at = (
            time(int(hour), int(minute or 0))
            if sep and hour.isdigit()
            else time.fromisoformat(given)
        )
    except ValueError:
        when = datetime.fromisoformat(given.strip())
        return when if when.tzinfo else when.replace(tzinfo=now.tzinfo)
    when = datetime.combine(now.date(), at, now.tzinfo)
    return when if when > now else when + timedelta(days=1)


class FocusKeeper:
    def __init__(
        self,
        journal: EventStore,
        timezone: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._journal = journal
        self.zone = ZoneInfo(timezone)
        self.clock = clock or (lambda: datetime.now(self.zone))
        self.state = FocusState()
        self._runner = ProjectionRunner(journal, [self.state])

    async def sync(self) -> None:
        await self._runner.catch_up()

    async def current(self, now: datetime) -> Focus | None:
        await self.sync()
        last = self.state.last
        return last if last is not None and last.active(now) else None

    async def start(self, until: datetime, why: str, origin: Provenance) -> Focus:
        now = self.clock()
        focus_id = f"fo_{new_id()}"
        await self._append(
            STARTED,
            {
                "focus_id": focus_id,
                "since": now.isoformat(),
                "until": until.isoformat(),
                "why": why,
            },
            origin,
        )
        assert self.state.last is not None
        return self.state.last

    async def stop(self, origin: Provenance) -> Focus | None:
        now = self.clock()
        focus = await self.current(now)
        if focus is None:
            return None
        await self._append(ENDED, {"focus_id": focus.id, "at": now.isoformat()}, origin)
        return focus

    async def _append(self, kind: str, payload: dict[str, Any], origin: Provenance) -> None:
        await self._journal.append(
            [NewEvent(kind=kind, payload=payload, provenance=origin, trust=Trust.OWNER)]
        )
        await self.sync()

    async def presence(self, now: datetime) -> Moment:
        """For the arbiter: focused until a time, or just out of a focus (a break)."""
        await self.sync()
        last = self.state.last
        if last is None or now < last.since:
            return Moment()
        if last.active(now):
            return Moment(focus_until=last.until)
        if now - last.end() <= BREAK:
            return Moment(free_since=last.end())
        return Moment()

    async def for_contacts(self, now: datetime) -> str | None:
        """What a contact who may know when the owner is free is told: until when, not why."""
        focus = await self.current(now)
        if focus is None:
            return None
        until = focus.until.astimezone(self.zone)
        return f"Il est concentré jusqu'à {until:%H:%M} et ne lira ses messages qu'ensuite."


class StartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    until: str = Field(
        min_length=3,
        max_length=40,
        description="Until when: '18:00' (today, or tomorrow if past) or an ISO date and time",
    )
    why: str = Field(default="", max_length=200, description="What he is doing, if he said")


class StopInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def focus_tools(keeper: FocusKeeper) -> list[Tool]:
    def origin(ctx: ToolContext) -> Provenance:
        return Provenance(
            source=f"thread:{ctx.thread_id}",
            actor="orion:core",
            derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
        )

    async def start(args: Any, ctx: ToolContext) -> ToolResult:
        now = keeper.clock()
        try:
            until = parse_until(args.until, now)
        except ValueError:
            return ToolResult(f"Unreadable time: {args.until!r}.", is_error=True)
        if not now < until <= now + MAX_FOCUS:
            return ToolResult("A focus lasts from now to at most 14 hours.", is_error=True)
        focus = await keeper.start(until, args.why, origin(ctx))
        end = focus.until.astimezone(keeper.zone)
        return ToolResult(
            f"Concentration jusqu'à {end:%H:%M} ({end:%d/%m}). Ce que tu lui aurais dit de "
            "toi-même attend la fin et partira en un seul message ; seul passe ce qui ne peut "
            "pas attendre. Ses rappels sonnent quand même."
        )

    async def stop(_args: Any, ctx: ToolContext) -> ToolResult:
        focus = await keeper.stop(origin(ctx))
        if focus is None:
            return ToolResult("Il n'était pas en concentration.")
        return ToolResult("Concentration terminée : ce qui attendait va partir, en un message.")

    return [
        Tool(
            name="focus_start",
            description="The owner is working and does not want to be disturbed until a time "
            "(« je bosse jusqu'à 18 h », « pas de messages avant midi »).",
            input_model=StartInput,
            handler=start,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
        Tool(
            name="focus_stop",
            description="End the focus early (« c'est bon j'ai fini »).",
            input_model=StopInput,
            handler=stop,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
    ]


class FocusModule(Module):
    name = "focus"
    instructions = "focus.md"
    settings_model = FocusSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.keeper: FocusKeeper | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        self.keeper = FocusKeeper(plate.journal, self.config.agent.timezone)
        await self.keeper.sync()
        return [
            grouped(
                "focus",
                "The owner's focus time: nothing unrequested reaches him until it ends.",
                focus_tools(self.keeper),
                {"focus_start": "start", "focus_stop": "stop"},
            )
        ]

    async def serve(self, daemon: Daemon) -> None:
        from orion.secretary.module import SecretaryModule

        if self.keeper is None:
            return
        daemon.presence.append(self.keeper)
        secretary = daemon.plate.module(SecretaryModule)
        if secretary is not None and secretary.secretary is not None:
            secretary.secretary.focus = self.keeper.for_contacts
