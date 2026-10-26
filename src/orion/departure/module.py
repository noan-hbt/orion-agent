"""Departures: `[modules.departure]` in orion.toml (on by default).

A good assistant does not wait to be asked the evening before a trip. The night before a
departure (a train, a flight, a trip of several days) or an ordeal (an exam, an interview, a
driving test), Orion prepares it in one message (D77):

- **What it sees.** Events in the calendar whose title or place says so (« Vol pour
  Lisbonne », « Partiel de droit », a trip of several days with a place), and departures the
  owner mentions that are not in his calendar (`departure` plan).
- **What the code checks.** His papers in the vault that have an end of validity (D74, D131):
  valid on the day, or not; Orion judges which ones the trip needs.
- **What Orion looks up** in the turn that writes the message, with its own tools: the ticket,
  the booking or the convocation in his mail, the weather there, the time to get there.
- **What it says**: only what it verified, one line per point, no generic checklist. Nothing
  useful beyond the time: `SILENCE`.

It goes through the initiative engine (quiet hours, the budget, the verifier), once per
departure, the evening before at `evening`, or as soon as it is seen if that time is past.

    departure.planned  departure.cancelled
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.initiative.engine import Alert
from orion.modules.base import Daemon, Module, ModuleSettings, Planned, Plate
from orion.tools.actions import grouped
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.calendar.ics import Occurrence
    from orion.config import OrionConfig
    from orion.papers.module import Paper

PLANNED = "departure.planned"
CANCELLED = "departure.cancelled"

LOOKAHEAD = timedelta(days=2)
LAST_CALL = timedelta(hours=1)
"""No preparation in the last hour: he is already on his way."""
Kind = Literal["voyage", "épreuve"]
_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
TRAVEL_WORDS = (
    "vol",
    "avion",
    "aeroport",
    "flight",
    "train",
    "tgv",
    "ouigo",
    "sncf",
    "eurostar",
    "thalys",
    "gare",
    "voyage",
    "depart",
    "trip",
    "ferry",
    "flixbus",
    "blablacar",
    "hotel",
    "airbnb",
)
ORDEAL_WORDS = (
    "examen",
    "partiel",
    "concours",
    "entretien",
    "oral",
    "soutenance",
    "convocation",
    "permis",
    "interview",
)


def words(text: str) -> set[str]:
    """Lowercase words without accents: « Départ » matches « depart »."""
    plain = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(c if c.isalnum() else " " for c in plain if not unicodedata.combining(c))
    return set(plain.split())


class DepartureSettings(ModuleSettings):
    enabled: bool = True
    evening: time = Field(default=time(19, 0), description="When to prepare the day before")
    travel_words: list[str] = Field(
        default_factory=list[str], description="More words that mean a trip, in his calendar"
    )
    ordeal_words: list[str] = Field(
        default_factory=list[str], description="More words that mean an exam or an interview"
    )


@dataclass(frozen=True)
class Departure:
    key: str
    kind: str
    what: str
    at: datetime
    where: str
    trust: Trust


class Calendar(Protocol):
    def between(self, start: datetime, end: datetime) -> list[tuple[str, Occurrence]]: ...


class Papers(Protocol):
    def papers(self) -> list[Paper]: ...


@dataclass
class DepartureState:
    name: str = "departure"
    planned: dict[str, Departure] = field(default_factory=dict[str, Departure])
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.planned.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind == PLANNED:
            departure = Departure(
                str(payload["departure_id"]),
                str(payload["kind"]),
                str(payload["what"]),
                datetime.fromisoformat(str(payload["at"])),
                str(payload.get("where", "")),
                event.trust,
            )
            self.planned[departure.key] = departure
        elif event.kind == CANCELLED:
            self.planned.pop(str(payload["departure_id"]), None)


def prepared_at(start: datetime, evening: time) -> datetime:
    """The evening before."""
    return datetime.combine(start.date() - timedelta(days=1), evening, start.tzinfo)


def papers_line(papers: list[Paper], on: date) -> str:
    """His own papers in the vault that have an end of validity, and whether each is valid on
    the day: which ones a trip needs (an ID, a passport, an insurance) is Orion's to judge."""
    lines: list[str] = []
    for paper in papers:
        if paper.holder or paper.valid_until is None:
            continue
        name = f"{paper.label} ({paper.about})" if paper.about else paper.label
        if paper.valid_until < on:
            lines.append(f"- {name} : EXPIRÉ depuis le {paper.valid_until:%d/%m/%Y}")
        else:
            lines.append(f"- {name} : valide jusqu'au {paper.valid_until:%d/%m/%Y}")
    return "\n".join(lines)


class Departures:
    def __init__(
        self,
        journal: EventStore,
        settings: DepartureSettings,
        timezone: str,
        calendar: Calendar | None = None,
        papers: Papers | None = None,
        tools: tuple[str, ...] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._journal = journal
        self.settings = settings
        self.zone = ZoneInfo(timezone)
        self.calendar = calendar
        self.papers = papers
        self.tools = set(tools)
        """Tools Orion has to look things up in the turn (`mail`, `web_search`…)."""
        self.clock = clock or (lambda: datetime.now(self.zone))
        self.state = DepartureState()
        self._runner = ProjectionRunner(journal, [self.state])
        self._travel = {*TRAVEL_WORDS, *(w for t in settings.travel_words for w in words(t))}
        self._ordeal = {*ORDEAL_WORDS, *(w for t in settings.ordeal_words for w in words(t))}

    async def sync(self) -> None:
        await self._runner.catch_up()

    def kind_of(self, occurrence: Occurrence) -> str | None:
        said = words(f"{occurrence.title} {occurrence.location}")
        if said & self._ordeal:
            return "épreuve"
        if said & self._travel:
            return "voyage"
        if (
            occurrence.all_day
            and occurrence.location
            and occurrence.end - occurrence.start >= (timedelta(days=2))
        ):
            return "voyage"
        return None

    def from_calendar(self, now: datetime, ahead: timedelta = LOOKAHEAD) -> list[Departure]:
        if self.calendar is None:
            return []
        found: list[Departure] = []
        for account, occurrence in self.calendar.between(now, now + ahead):
            kind = self.kind_of(occurrence)
            if kind is None or occurrence.start < now:
                continue
            start = occurrence.start
            if occurrence.all_day:
                start = datetime.combine(occurrence.start.date(), time(8, 0), self.zone)
            found.append(
                Departure(
                    f"cal:{account}:{occurrence.key}",
                    kind,
                    occurrence.title,
                    start,
                    occurrence.location,
                    Trust.SYSTEM,
                )
            )
        return found

    async def alerts(self, now: datetime) -> list[Alert]:
        await self.sync()
        found: list[Alert] = []
        seen: set[tuple[str, date]] = set()
        for departure in [*self.state.planned.values(), *self.from_calendar(now)]:
            at = departure.at.astimezone(self.zone)
            closes = at - LAST_CALL
            if not prepared_at(at, self.settings.evening) <= now < closes:
                continue
            same = (" ".join(sorted(words(departure.what))), at.date())
            if same in seen:
                continue
            seen.add(same)
            found.append(
                Alert(
                    key=f"departure:{departure.key}",
                    notice=self.notice(departure, at),
                    why="tu pars demain" if departure.kind == "voyage" else "c'est demain",
                    fallback=f"{_DAYS[at.weekday()].capitalize()} à {at:%H:%M} : "
                    f"{departure.what}" + (f" ({departure.where})" if departure.where else ""),
                    trust=departure.trust,
                    expires_at=closes,
                    importance=0.55,
                )
            )
        return found

    def notice(self, departure: Departure, at: datetime) -> str:
        where = f", {departure.where}" if departure.where else ""
        lead = (
            f"Il part {_DAYS[at.weekday()]} à {at:%H:%M} : {departure.what}{where}."
            if departure.kind == "voyage"
            else f"Il a {_DAYS[at.weekday()]} à {at:%H:%M} : {departure.what}{where}."
        )
        parts = [lead]
        if departure.kind == "voyage" and self.papers is not None:
            known = papers_line(self.papers.papers(), at.date())
            parts.append(
                f"Ses papiers rangés qui ont une date de fin (vois ceux qu'il lui faut) :\n{known}"
                if known
                else "Aucun papier avec une date de fin dans ses papiers (pièce d'identité…)."
            )
        checks: list[str] = []
        if "mail" in self.tools:
            checks.append(
                "le billet, la réservation ou la convocation dans ses mails (`mail`) : l'heure "
                "exacte, le numéro, la gare, le terminal ou la salle"
            )
        if "web_search" in self.tools:
            checks.append(
                "la météo là-bas et le temps pour y aller, d'où il est, à cette heure-là "
                "(`web_search`)"
            )
        if checks:
            parts.append("Avant d'écrire, vérifie :\n" + "\n".join(f"- {c}" for c in checks))
        parts.append(
            "Prépare-le en UN message court, comme un bon assistant la veille : une ligne par "
            "point, seulement ce que tu as vérifié, l'heure à laquelle partir s'il faut la "
            "calculer. Une pièce d'identité expirée pour un voyage passe en premier. Pas de "
            "liste générique de choses à ne pas oublier. Si tu ne trouves rien d'utile au-delà "
            "de l'heure, réponds SILENCE."
        )
        return "\n".join(parts)

    async def record(self, kind: str, payload: dict[str, Any], ctx: ToolContext) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=Provenance(
                        source=f"thread:{ctx.thread_id}",
                        actor="orion:core",
                        derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
                    ),
                    trust=ctx.context_trust,
                    correlation_id=ctx.correlation_id,
                )
            ]
        )
        await self.sync()


class PlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what: str = Field(min_length=3, max_length=200, description="« Train pour Lyon », « oral »")
    at: str = Field(description="When it leaves or starts, local time 'YYYY-MM-DD HH:MM'")
    where: str = Field(default="", max_length=200, description="Station, airport, place")
    kind: Kind = Field(default="voyage", description="voyage, or épreuve (exam, interview)")


class CancelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    departure: str = Field(description="The departure id (dp_…)")


class ListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def departure_tools(departures: Departures) -> list[Tool]:
    async def plan(args: Any, ctx: ToolContext) -> ToolResult:
        try:
            moment = datetime.fromisoformat(args.at.strip().replace(" ", "T"))
        except ValueError:
            return ToolResult("`at` must be 'YYYY-MM-DD HH:MM'.", is_error=True)
        at = moment.replace(tzinfo=departures.zone) if moment.tzinfo is None else moment
        if at <= departures.clock():
            return ToolResult("`at` is in the past.", is_error=True)
        departure_id = f"dp_{new_id()}"
        await departures.record(
            PLANNED,
            {
                "departure_id": departure_id,
                "kind": args.kind,
                "what": args.what,
                "at": at.isoformat(),
                "where": args.where,
            },
            ctx,
        )
        when = max(prepared_at(at, departures.settings.evening), departures.clock())
        return ToolResult(
            f"Noté ({departure_id}) : tu le prépareras {_DAYS[when.weekday()]} à {when:%H:%M}."
        )

    async def cancel(args: Any, ctx: ToolContext) -> ToolResult:
        await departures.sync()
        if args.departure not in departures.state.planned:
            return ToolResult(f"No departure {args.departure}.", is_error=True)
        await departures.record(CANCELLED, {"departure_id": args.departure}, ctx)
        return ToolResult("Annulé.")

    async def listing(_args: Any, _ctx: ToolContext) -> ToolResult:
        await departures.sync()
        now = departures.clock()
        upcoming = [*departures.state.planned.values(), *departures.from_calendar(now)]
        lines = [
            f"- ({d.key}) {d.kind} : {d.what}, {_DAYS[d.at.weekday()]} {d.at:%d/%m %H:%M}"
            + (f", {d.where}" if d.where else "")
            for d in sorted(upcoming, key=lambda d: d.at)
            if d.at > now
        ]
        return ToolResult("\n".join(lines) or "Aucun départ prévu dans les deux jours.")

    return [
        Tool(
            name="departure_plan",
            description="A trip or an exam the owner mentions that is not in his calendar (« je "
            "prends le train pour Lyon vendredi à 7 h »).",
            input_model=PlanInput,
            handler=plan,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
        Tool(
            name="departure_cancel",
            description="Drop a departure noted with plan (cancelled, or he would rather not).",
            input_model=CancelInput,
            handler=cancel,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
            accepts_untrusted_context=True,
        ),
        Tool(
            name="departure_list",
            description="The departures you will prepare: noted ones and those in his calendar.",
            input_model=ListInput,
            handler=listing,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        ),
    ]


class DepartureModule(Module):
    name = "departure"
    instructions = "departure.md"
    settings_model = DepartureSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.departures: Departures | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        from orion.calendar.module import CalendarModule
        from orion.papers.module import PapersModule

        calendar = plate.module(CalendarModule)
        papers = plate.module(PapersModule)
        tools = tuple(
            name for name, module in (("mail", "mail"), ("web_search", "web")) if plate.has(module)
        )
        self.departures = Departures(
            plate.journal,
            self.settings,
            self.config.agent.timezone,
            calendar=calendar.calendar if calendar is not None else None,
            papers=papers.vault if papers is not None else None,
            tools=tools,
        )
        await self.departures.sync()
        return [
            grouped(
                "departure",
                "Trips and exams you prepare the evening before (ticket, papers, weather, when "
                "to leave). Those in his calendar are found on their own.",
                departure_tools(self.departures),
                {"departure_plan": "plan", "departure_cancel": "cancel", "departure_list": "list"},
            )
        ]

    async def serve(self, daemon: Daemon) -> None:
        if self.departures is not None:
            daemon.alerts.append(self.departures)

    async def planned(self, now: datetime, until: datetime) -> list[Planned]:
        departures = self.departures
        if departures is None:
            return []
        await departures.sync()
        found: list[Planned] = []
        ahead = until - now + timedelta(days=1)
        for d in [*departures.state.planned.values(), *departures.from_calendar(now, ahead)]:
            at = max(prepared_at(d.at, departures.settings.evening), now)
            if at < until and at < d.at - LAST_CALL:
                undo = f"departure (cancel, {d.key})" if d.key in departures.state.planned else ""
                found.append(Planned(at, f"préparer : {d.what} ({d.at:%d/%m %H:%M})", undo))
        return found
