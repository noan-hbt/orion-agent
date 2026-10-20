"""Places: `[modules.places]` in orion.toml (off by default: it needs the phone set up).

« Rappelle-moi d'acheter du pain quand je passe à la boulangerie. » The owner's iPhone knows
where they are; Orion does not need to. A Shortcuts automation (« Quand j'arrive à … ») tells
Orion only the name of the place the owner chose, and only for the places they set up:
no coordinates, no track.

- `remind_at_place`: a reminder for when the owner arrives at (or leaves) a place;
- the phone posts `{"place": "boulangerie", "event": "arrive"}` to `/places/event`, with the
  token `orion places token` printed (only its digest is journaled);
- the reminders for that place go off at once, quiet hours or not, like the ones the owner set
  by time: they asked for them.

    place.token  place.reminder  place.reminder.cancelled  place.event  place.fired
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import structlog
import uvicorn
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.initiative.engine import Report
from orion.memory.store import name_key
from orion.modules.base import Daemon, Module, ModuleSettings, Plate
from orion.tools.actions import grouped
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig

log = structlog.get_logger(__name__)

TOKEN = "place.token"
REMINDER = "place.reminder"
CANCELLED = "place.reminder.cancelled"
ARRIVED = "place.event"
FIRED = "place.fired"
PATH = "/places/event"
SIGNAL_TRUST = Trust.OWNER_INSTANCE
"""The owner's phone, saying where they are: theirs, but a signal, not their words."""

Moment = Literal["arrive", "leave"]


class PlaceSettings(ModuleSettings):
    host: str = "127.0.0.1"
    port: int = 8793


@dataclass(frozen=True)
class PlaceReminder:
    id: str
    place: str
    when: Moment
    what: str
    trust: Trust


def same_place(a: str, b: str) -> bool:
    """« boulangerie » is « la boulangerie » and « Boulangerie Paul »."""
    x, y = (
        set(name_key(a).split()) - {"la", "le", "les", "l", "du", "de"},
        set(name_key(b).split()) - {"la", "le", "les", "l", "du", "de"},
    )
    return bool(x) and bool(y) and (x <= y or y <= x)


@dataclass
class PlaceState:
    name: str = "places"
    token: str = ""
    """The digest of the phone's current token."""
    reminders: dict[str, PlaceReminder] = field(default_factory=dict[str, PlaceReminder])
    fired: list[tuple[str, PlaceReminder]] = field(default_factory=list[tuple[str, PlaceReminder]])
    known: dict[str, str] = field(default_factory=dict[str, str])
    """Places the phone has reported: key → name as set up."""
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.token = ""
        self.reminders.clear()
        self.fired.clear()
        self.known.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind == TOKEN:
            self.token = str(payload["digest"])
        elif event.kind == REMINDER:
            reminder = PlaceReminder(
                str(payload["reminder_id"]),
                str(payload["place"]),
                "leave" if payload.get("when") == "leave" else "arrive",
                str(payload["what"]),
                event.trust,
            )
            self.reminders[reminder.id] = reminder
        elif event.kind == CANCELLED:
            self.reminders.pop(str(payload["reminder_id"]), None)
        elif event.kind == ARRIVED:
            place = str(payload["place"])
            self.known[name_key(place)] = place
        elif event.kind == FIRED:
            reminder = self.reminders.pop(str(payload["reminder_id"]), None)
            if reminder is not None:
                self.fired.append((event.id, reminder))


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class PlaceEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    place: str = Field(min_length=1, max_length=80)
    event: Moment = "arrive"


class Places:
    def __init__(self, journal: EventStore) -> None:
        self._journal = journal
        self.state = PlaceState()
        self._runner = ProjectionRunner(journal, [self.state])

    async def sync(self) -> None:
        await self._runner.catch_up()

    async def append(
        self,
        kind: str,
        payload: dict[str, Any],
        provenance: Provenance,
        trust: Trust,
        cid: str | None = None,
    ) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=provenance,
                    trust=trust,
                    correlation_id=cid,
                )
            ]
        )
        await self.sync()

    async def new_token(self) -> str:
        """A new token for the phone; the previous one stops working."""
        token = secrets.token_urlsafe(24)
        await self.append(
            TOKEN,
            {"digest": digest(token)},
            Provenance(source="cli:places", actor="owner:main"),
            Trust.OWNER,
        )
        return token

    async def signal(self, token: str, place: str, moment: Moment) -> list[str]:
        """The phone says the owner arrived at or left `place`: the reminders that go off."""
        await self.sync()
        if not self.state.token or not hmac.compare_digest(digest(token), self.state.token):
            raise PermissionError("bad token")
        source = Provenance(source="phone:places", actor="owner:phone")
        await self.append(ARRIVED, {"place": place, "event": moment}, source, SIGNAL_TRUST)
        due = [
            r
            for r in self.state.reminders.values()
            if r.when == moment and same_place(r.place, place)
        ]
        for reminder in due:
            await self.append(
                FIRED, {"reminder_id": reminder.id, "place": place}, source, SIGNAL_TRUST
            )
        log.info("places.event", moment=moment, reminders=len(due))
        return [r.id for r in due]

    async def reports(self) -> list[Report]:
        await self.sync()
        return [
            Report(
                key=f"place:{event_id}",
                notice=f"Il {'arrive à' if r.when == 'arrive' else 'quitte'} « {r.place} », "
                f"et il t'avait demandé de lui rappeler à ce moment-là : {r.what}\n"
                "Rappelle-le-lui en une phrase.",
                why=f"le rappel de « {r.place} »",
                fallback=f"Rappel ({r.place}) : {r.what}",
                trust=min(r.trust, SIGNAL_TRUST),
            )
            for event_id, r in self.state.fired
        ]

    def app(self) -> Starlette:
        async def event(request: Request) -> JSONResponse:
            token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            try:
                signal = PlaceEvent.model_validate_json(await request.body())
            except ValidationError:
                return JSONResponse({"error": "expected {place, event}"}, status_code=400)
            try:
                fired = await self.signal(token, signal.place, signal.event)
            except PermissionError:
                log.warning("places.refused")
                return JSONResponse({"error": "bad token"}, status_code=401)
            return JSONResponse({"ok": True, "reminders": len(fired)})

        return Starlette(routes=[Route(PATH, event, methods=["POST"])])


class RemindInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    place: str = Field(min_length=2, max_length=80, description="The place, as he names it")
    what: str = Field(min_length=2, max_length=300, description="What to remind him of")
    when: Moment = Field(default="arrive", description="'arrive' or 'leave'")


class CancelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reminder: str = Field(description="The reminder id (pl_…)")


class ListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def place_tools(places: Places) -> list[Tool]:
    def provenance(ctx: ToolContext) -> Provenance:
        return Provenance(
            source=f"thread:{ctx.thread_id}",
            actor="orion:core",
            derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
        )

    async def remind(args: Any, ctx: ToolContext) -> ToolResult:
        await places.sync()
        reminder_id = f"pl_{new_id()}"
        await places.append(
            REMINDER,
            {"reminder_id": reminder_id, "place": args.place, "what": args.what, "when": args.when},
            provenance(ctx),
            ctx.context_trust,
            ctx.correlation_id,
        )
        known = [n for n in places.state.known.values() if same_place(n, args.place)]
        hint = (
            ""
            if known
            else " Son téléphone ne m'a encore jamais signalé ce lieu : dis-lui d'ajouter une "
            "automatisation Raccourcis pour lui (voir `places`)."
        )
        verb = "arrivera à" if args.when == "arrive" else "quittera"
        return ToolResult(f"Noté ({reminder_id}) : quand il {verb} « {args.place} ».{hint}")

    async def cancel(args: Any, ctx: ToolContext) -> ToolResult:
        await places.sync()
        if args.reminder not in places.state.reminders:
            return ToolResult(f"No reminder {args.reminder}.", is_error=True)
        await places.append(
            CANCELLED,
            {"reminder_id": args.reminder},
            provenance(ctx),
            ctx.context_trust,
            ctx.correlation_id,
        )
        return ToolResult("Annulé.")

    async def listing(_args: Any, _ctx: ToolContext) -> ToolResult:
        await places.sync()
        lines = [
            f"- ({r.id}) {'en arrivant à' if r.when == 'arrive' else 'en quittant'} "
            f"« {r.place} » : {r.what}"
            for r in places.state.reminders.values()
        ]
        known = ", ".join(sorted(places.state.known.values())) or "aucun encore"
        lines.append(f"Lieux que son téléphone signale : {known}.")
        if not places.state.token:
            lines.append(
                "Le téléphone n'est pas encore relié : `orion places token` sur le serveur."
            )
        lines.append(
            "Pour un nouveau lieu : app Raccourcis, Automatisation, « Arrivée » (ou « Départ »), "
            f"le lieu, puis « Obtenir le contenu de l'URL » en POST vers <adresse>{PATH}, avec "
            'l\'en-tête Authorization: Bearer <jeton> et le JSON {"place": "nom", '
            '"event": "arrive"}.'
        )
        return ToolResult("\n".join(lines))

    return [
        Tool(
            name="remind_at_place",
            description="A reminder for when the owner arrives at, or leaves, a place (« quand je "
            "passe à la boulangerie », « en partant du bureau »), not at a time.",
            input_model=RemindInput,
            handler=remind,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
        Tool(
            name="place_reminder_cancel",
            description="Cancel a reminder set for a place.",
            input_model=CancelInput,
            handler=cancel,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
            accepts_untrusted_context=True,
        ),
        Tool(
            name="places",
            description="The reminders waiting for a place, the places his phone reports, and "
            "how to set up a new one.",
            input_model=ListInput,
            handler=listing,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        ),
    ]


class PlacesModule(Module):
    name = "places"
    instructions = "places.md"
    settings_model = PlaceSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.places: Places | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        self.places = Places(plate.journal)
        await self.places.sync()
        return [
            grouped(
                "places",
                "Reminders for when the owner arrives at, or leaves, a place (« quand je passe à "
                "la boulangerie »), not at a time.",
                place_tools(self.places),
                {"remind_at_place": "remind", "place_reminder_cancel": "cancel", "places": "list"},
            )
        ]

    async def serve(self, daemon: Daemon) -> None:
        if self.places is None:  # pragma: no cover - assemble always runs first
            return
        settings: PlaceSettings = self.settings
        daemon.reports.append(self.places)
        server = uvicorn.Server(
            uvicorn.Config(
                self.places.app(),
                host=settings.host,
                port=settings.port,
                log_config=None,
                access_log=False,
            )
        )
        daemon.add_service("places.http", server.serve)

    def doctor(self) -> list[str]:
        settings: PlaceSettings = self.settings
        return [f"phone events on {settings.host}:{settings.port}{PATH}"]
