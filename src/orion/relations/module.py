"""Relations: `[modules.relations]` in orion.toml (on by default).

Memory knows the people in the owner's life (`orion.memory.people`). This module makes Orion
attentive to them, the way a friend with a good memory is:

- **Before seeing someone** (an event in the calendar whose title names them, or a meeting the
  owner mentioned: `meeting_soon`), Orion reminds the owner of what matters: what is going on in
  their life, what one owes the other, what was said last time. The evening before a morning
  meeting, the morning of a later one.
- **Birthdays** (`person_birthday`): three days before, to think of something; on the day.
- **Staying in touch** (`keep_in_touch`): for the people the owner wants to see often, Orion
  notes when they saw each other (`saw`) and says it when it has been too long, with a
  concrete idea rather than a reproach.

Everything reaches the owner through the initiative engine; the model writing the message gets
what memory knows of the person, and answers `SILENCE` when there is nothing worth saying.

    relation.birthday  relation.rhythm  relation.contact  relation.meeting
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.initiative.engine import Alert
from orion.memory.people import Person, describe, find, people_index
from orion.memory.service import MemoryService
from orion.memory.store import name_key
from orion.modules.base import Daemon, Module, ModuleSettings, Planned, Plate
from orion.tools.actions import grouped
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.calendar.service import Calendar
    from orion.config import OrionConfig

BIRTHDAY = "relation.birthday"
RHYTHM = "relation.rhythm"
CONTACT = "relation.contact"
MEETING = "relation.meeting"
DROPPED = "relation.dropped"
"""A birthday noted wrong, or a meeting that will not happen (D201)."""

_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
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
MIN_NAME = 3
"""Shorter names are not looked for in event titles (« Al » is in too many words)."""


class RelationSettings(ModuleSettings):
    enabled: bool = True
    evening: time = Field(default=time(19, 0), description="Preparing tomorrow morning's meetings")
    morning: time = Field(default=time(9, 0), description="Preparing today's later meetings")
    birthday_days_before: int = Field(default=3, ge=0, le=30)
    nudge_at: time = Field(default=time(18, 0), description="When to say it has been too long")


@dataclass(frozen=True)
class Meeting:
    key: str
    person: str
    at: datetime
    about: str
    trust: Trust


@dataclass
class RelationState:
    name: str = "relations"
    birthdays: dict[str, tuple[int, int, int | None, str]] = field(
        default_factory=dict[str, tuple[int, int, int | None, str]]
    )
    """name key → (month, day, year if known, the name as given)"""
    rhythms: dict[str, tuple[int, str]] = field(default_factory=dict[str, tuple[int, str]])
    """name key → (every N days, the name as given)"""
    contacts: dict[str, datetime] = field(default_factory=dict[str, datetime])
    """name key → last time they were in touch"""
    meetings: dict[str, Meeting] = field(default_factory=dict[str, Meeting])
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.birthdays.clear()
        self.rhythms.clear()
        self.contacts.clear()
        self.meetings.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        person = str(payload.get("person", ""))
        key = name_key(person)
        if event.kind == BIRTHDAY:
            year = payload.get("year")
            self.birthdays[key] = (
                int(payload["month"]),
                int(payload["day"]),
                int(year) if year else None,
                person,
            )
        elif event.kind == RHYTHM:
            every = int(payload["every_days"])
            if every:
                self.rhythms[key] = (every, person)
            else:
                self.rhythms.pop(key, None)
        elif event.kind == CONTACT:
            at = datetime.fromisoformat(str(payload["at"]))
            if key not in self.contacts or at > self.contacts[key]:
                self.contacts[key] = at
        elif event.kind == MEETING:
            meeting = Meeting(
                str(payload["meeting_id"]),
                person,
                datetime.fromisoformat(str(payload["at"])),
                str(payload.get("about", "")),
                event.trust,
            )
            self.meetings[meeting.key] = meeting
        elif event.kind == DROPPED:
            if payload.get("meeting_id"):
                self.meetings.pop(str(payload["meeting_id"]), None)
            else:
                self.birthdays.pop(key, None)


def prepared_at(start: datetime, settings: RelationSettings) -> datetime:
    """When to prepare a meeting: the evening before a morning one, else that morning."""
    if start.time() < time(12, 0):
        return datetime.combine(start.date() - timedelta(days=1), settings.evening, start.tzinfo)
    return datetime.combine(start.date(), settings.morning, start.tzinfo)


def next_birthday(month: int, day: int, today: date) -> date:
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:  # 29 February in a common year
            candidate = date(year, 3, 1)
        if candidate >= today:
            return candidate
    raise AssertionError("unreachable")  # pragma: no cover


class Relations:
    def __init__(
        self,
        journal: EventStore,
        memory: MemoryService | None,
        settings: RelationSettings,
        timezone: str,
        owner_names: list[str],
        calendar: Calendar | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._journal = journal
        self._memory = memory
        self.settings = settings
        self.zone = ZoneInfo(timezone)
        self._owner_names = set(owner_names)
        self.calendar = calendar
        self.clock = clock or (lambda: datetime.now(self.zone))
        self.state = RelationState()
        self._runner = ProjectionRunner(journal, [self.state])

    async def sync(self) -> None:
        await self._runner.catch_up()

    def is_owner(self, name: str) -> bool:
        """The owner himself, by one of his names, alone or followed by his surname."""
        key = name_key(name)
        return any(
            key == own or key.startswith(own + " ")
            for own in (name_key(n) for n in self._owner_names | {"owner"})
        )

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

    async def people(self) -> tuple[dict[str, Person], dict[str, str]]:
        if self._memory is None:
            return {}, {}
        aliases = await self._memory.store.aliases()
        facts = await self._memory.store.facts()
        commitments = await self._memory.store.commitments(status=None)
        return people_index(facts, commitments, aliases, self._owner_names), aliases

    async def dossier(self, name: str) -> tuple[str, Trust]:
        """What memory knows of someone, for the message that mentions them."""
        people, aliases = await self.people()
        found = find(people, name, aliases)
        if len(found) != 1:
            return f"{name} : (rien en mémoire)", Trust.SYSTEM
        person = found[0]
        lines = [describe(person)]
        key = name_key(person.name)
        last = self.state.contacts.get(key) or self.state.contacts.get(name_key(name))
        if last is not None:
            lines.append(f"Dernière fois ensemble (noté) : {last:%d/%m/%Y}")
        trust = min((f.trust for f in person.facts if f.current), default=Trust.SYSTEM)
        return "\n".join(lines), min(trust, Trust.SYSTEM)

    async def calendar_meetings(self, now: datetime) -> list[Meeting]:
        """Upcoming events whose title names someone memory knows."""
        if self.calendar is None:
            return []
        people, _ = await self.people()
        names = {
            n: p.name
            for p in people.values()
            for n in {p.name, *p.aliases, p.name.split()[0]}
            if len(n) >= MIN_NAME
        }
        found: list[Meeting] = []
        for account, event in self.calendar.between(now, now + timedelta(days=2)):
            title = f" {name_key(event.title)} "
            for name, person in names.items():
                if f" {name_key(name)} " in title:
                    found.append(
                        Meeting(
                            f"cal:{account}:{event.key}",
                            person,
                            event.start,
                            event.title,
                            Trust.SYSTEM,
                        )
                    )
                    break
        return found

    async def alerts(self, now: datetime) -> list[Alert]:
        await self.sync()
        found: list[Alert] = []
        meetings = [*self.state.meetings.values(), *await self.calendar_meetings(now)]
        seen: set[tuple[str, date]] = set()
        for meeting in meetings:
            when = (name_key(meeting.person), meeting.at.date())
            if when in seen or not prepared_at(meeting.at, self.settings) <= now < meeting.at:
                continue
            seen.add(when)
            dossier, trust = await self.dossier(meeting.person)
            at = meeting.at.astimezone(self.zone)
            found.append(
                Alert(
                    key=f"meeting:{meeting.key}",
                    notice=f"Il voit {meeting.person} {_DAYS[at.weekday()]} à {at:%H:%M}"
                    + (f" ({meeting.about})" if meeting.about else "")
                    + f". Ce que tu sais :\n{dossier}\n"
                    "Rappelle-lui en deux ou trois phrases ce qui est utile avant leur rencontre : "
                    "ce qui se passe dans sa vie, ce que l'un doit à l'autre, ce dont ils avaient "
                    "parlé. Rien d'inventé. Si tu ne sais rien d'utile, réponds SILENCE.",
                    why=f"il voit {meeting.person}",
                    fallback=f"Tu vois {meeting.person} {_DAYS[at.weekday()]} à {at:%H:%M}.",
                    trust=min(trust, meeting.trust),
                    expires_at=meeting.at,
                    importance=0.45,
                )
            )
        today = now.date()
        for key, (month, day, year, person) in self.state.birthdays.items():
            birthday = next_birthday(month, day, today)
            ahead = self.settings.birthday_days_before
            moments = [(0, self.settings.morning, 0.6)]
            if ahead:
                moments.append((ahead, time(10, 0), 0.35))
            for before, at_time, importance in moments:
                due = datetime.combine(birthday - timedelta(days=before), at_time, self.zone)
                if not due <= now < due + timedelta(hours=12):
                    continue
                age = f" ({birthday.year - year} ans)" if year else ""
                dossier, trust = await self.dossier(person)
                lead = (
                    f"C'est aujourd'hui l'anniversaire de {person}{age}."
                    if before == 0
                    else f"Dans {before} jours ({_DAYS[birthday.weekday()]}), c'est "
                    f"l'anniversaire de {person}{age} : le temps de penser à quelque chose."
                )
                found.append(
                    Alert(
                        key=f"birthday:{key}:{birthday.isoformat()}:{before}",
                        notice=f"{lead}\nCe que tu sais :\n{dossier}\n"
                        "Dis-le-lui simplement, avec une idée concrète tirée de ce que tu sais "
                        "(un cadeau, un message, un appel), sans liste.",
                        why=f"l'anniversaire de {person}",
                        fallback=lead,
                        trust=trust,
                        expires_at=due + timedelta(hours=12),
                        importance=importance,
                    )
                )
        nudge = datetime.combine(today, self.settings.nudge_at, self.zone)
        if nudge <= now < nudge + timedelta(hours=3):
            for key, (every, person) in self.state.rhythms.items():
                last = self.state.contacts.get(key)
                if last is not None and now - last < timedelta(days=every):
                    continue
                week = today.isocalendar()
                since = f"depuis le {last:%d/%m}" if last else "pas noté depuis que tu suis ça"
                dossier, trust = await self.dossier(person)
                found.append(
                    Alert(
                        key=f"touch:{key}:{week.year}-{week.week}",
                        notice=f"Il veut voir {person} environ tous les {every} jours, et ils ne "
                        f"se sont pas vus ou parlé ({since}). Ce que tu sais :\n{dossier}\n"
                        "Dis-le-lui en une phrase, avec une idée concrète (un message à lui "
                        "envoyer, une occasion), jamais un reproche.",
                        why=f"des nouvelles de {person}",
                        fallback=f"Ça fait un moment que tu n'as pas vu {person}.",
                        trust=trust,
                        expires_at=nudge + timedelta(hours=3),
                        importance=0.3,
                    )
                )
        return found


class BirthdayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person: str = Field(min_length=1, max_length=80)
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)
    year: int | None = Field(default=None, ge=1900, le=2100, description="Birth year, if known")


class RhythmInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person: str = Field(min_length=1, max_length=80)
    every_days: int = Field(
        ge=0, le=365, description="About how often he wants to see them; 0: stop"
    )


class ContactInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person: str = Field(min_length=1, max_length=80)
    day: date | None = Field(default=None, description="When (YYYY-MM-DD); empty: today")
    how: str = Field(default="", max_length=200, description="Lunch, a call, a message…")


class DropInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    birthday: str = Field(default="", max_length=80, description="Whose birthday to forget")
    meeting: str = Field(default="", max_length=40, description="A meeting's id (r_…)")


class MeetingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person: str = Field(min_length=1, max_length=80)
    at: str = Field(description="Local time 'YYYY-MM-DD HH:MM'")
    about: str = Field(default="", max_length=200, description="Lunch, drinks, a work meeting…")


def relation_tools(relations: Relations) -> list[Tool]:
    async def birthday(args: Any, ctx: ToolContext) -> ToolResult:
        try:
            date(2000, args.month, args.day)  # a leap year: 29 February is a real birthday
        except ValueError:
            return ToolResult("No such day.", is_error=True)
        if relations.is_owner(args.person):
            return ToolResult(
                f"{args.person} is the owner himself, not someone in his life: his own "
                "birthday goes in memory (remember, category profile).",
                is_error=True,
            )
        await relations.record(
            BIRTHDAY,
            {"person": args.person, "month": args.month, "day": args.day, "year": args.year},
            ctx,
        )
        return ToolResult(
            f"Noté : l'anniversaire de {args.person} est le {args.day} {_MONTHS[args.month - 1]}."
        )

    async def rhythm(args: Any, ctx: ToolContext) -> ToolResult:
        await relations.record(RHYTHM, {"person": args.person, "every_days": args.every_days}, ctx)
        if not args.every_days:
            return ToolResult(f"Tu ne suis plus le rythme avec {args.person}.")
        return ToolResult(
            f"Noté : tu lui diras quand il n'aura pas vu {args.person} depuis {args.every_days} "
            "jours. Note-le quand ils se voient (relations, saw)."
        )

    async def saw(args: Any, ctx: ToolContext) -> ToolResult:
        now = relations.clock()
        day = args.day or now.date()
        at = datetime.combine(day, now.time() if day == now.date() else time(12, 0), relations.zone)
        await relations.record(
            CONTACT, {"person": args.person, "at": at.isoformat(), "how": args.how}, ctx
        )
        return ToolResult(f"Noté : {args.person}, le {at:%d/%m}.")

    async def meeting(args: Any, ctx: ToolContext) -> ToolResult:
        try:
            moment = datetime.fromisoformat(args.at.strip().replace(" ", "T"))
        except ValueError:
            return ToolResult("`at` must be 'YYYY-MM-DD HH:MM'.", is_error=True)
        at = moment.replace(tzinfo=relations.zone) if moment.tzinfo is None else moment
        if at <= relations.clock():
            return ToolResult("`at` is in the past.", is_error=True)
        await relations.record(
            MEETING,
            {
                "meeting_id": f"r_{new_id()}",
                "person": args.person,
                "at": at.isoformat(),
                "about": args.about,
            },
            ctx,
        )
        when = prepared_at(at, relations.settings)
        return ToolResult(
            f"Noté : tu lui rappelleras ce qui compte sur {args.person} "
            f"{_DAYS[when.weekday()]} à {when:%H:%M}."
        )

    async def drop(args: Any, ctx: ToolContext) -> ToolResult:
        await relations.sync()
        if args.meeting:
            if args.meeting not in relations.state.meetings:
                return ToolResult(f"No meeting {args.meeting}.", is_error=True)
            await relations.record(DROPPED, {"meeting_id": args.meeting}, ctx)
            return ToolResult("Rencontre retirée.")
        if name_key(args.birthday) not in relations.state.birthdays:
            return ToolResult(f"No birthday noted for {args.birthday!r}.", is_error=True)
        await relations.record(DROPPED, {"person": args.birthday}, ctx)
        return ToolResult(f"Anniversaire de {args.birthday} retiré.")

    internal: dict[str, Any] = {
        "effect": Effect.INTERNAL,
        "reversibility": Reversibility.REVERSIBLE,
    }
    return [
        Tool(
            name="person_birthday",
            description="Note someone's birthday, when the owner says it or it comes up: you "
            "will remind him in time, with an idea.",
            input_model=BirthdayInput,
            handler=birthday,
            **internal,
        ),
        Tool(
            name="keep_in_touch",
            description="The owner wants to see or talk to someone regularly (« je devrais voir "
            "ma mère plus souvent »): about how often. You will tell him when it has been too "
            "long.",
            input_model=RhythmInput,
            handler=rhythm,
            **internal,
        ),
        Tool(
            name="saw_someone",
            description="Note that the owner saw or talked with someone (« j'ai déjeuné avec "
            "Paul »), so you know when they were last in touch.",
            input_model=ContactInput,
            handler=saw,
            **internal,
        ),
        Tool(
            name="meeting_soon",
            description="The owner will see someone soon and it is not in his calendar (« je "
            "vois Paul jeudi midi »): you will remind him beforehand of what matters about them.",
            input_model=MeetingInput,
            handler=meeting,
            **internal,
        ),
        Tool(
            name="relation_drop",
            description="Forget a birthday noted wrong, or a meeting that will not happen.",
            input_model=DropInput,
            handler=drop,
            **internal,
        ),
    ]


class RelationsModule(Module):
    name = "relations"
    instructions = "relations.md"
    settings_model = RelationSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.relations: Relations | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        self.relations = Relations(
            plate.journal,
            plate.memory,
            self.settings,
            self.config.agent.timezone,
            self.config.memory.owner_names,
        )
        await self.relations.sync()
        return [
            grouped(
                "relations",
                "Birthdays, meetings with someone, staying in touch: note a birthday as soon as "
                "it comes up, a meeting the owner mentions, someone he wants to see more often, "
                "and when they saw each other.",
                relation_tools(self.relations),
                {
                    "meeting_soon": "meeting",
                    "person_birthday": "birthday",
                    "keep_in_touch": "keep_in_touch",
                    "saw_someone": "saw",
                    "relation_drop": "drop",
                },
            )
        ]

    async def review(self, now: datetime) -> list[str]:
        relations = self.relations
        if relations is None:
            return []
        await relations.sync()
        state = relations.state
        # A birthday or a rhythm is known by the person's name: `birthday` or `keep_in_touch`
        # again corrects it, `drop` (or `every_days` 0) removes it.
        lines = [
            f"- ({person}) anniversaire : {day} {_MONTHS[month - 1]}" + (f" {year}" if year else "")
            for month, day, year, person in state.birthdays.values()
        ]
        for key, (every, person) in state.rhythms.items():
            seen = state.contacts.get(key)
            lines.append(
                f"- ({person}) le voir tous les {every} jours"
                + (f" (vus le {seen:%d/%m})" if seen else " (pas vus depuis qu'il l'a demandé)")
            )
        lines += [
            f"- ({m.key}) voir {m.person} le {m.at:%d/%m %H:%M}"
            + (f" : {m.about}" if m.about else "")
            for m in sorted(state.meetings.values(), key=lambda m: m.at)
            if m.at > now
        ]
        return lines

    async def planned(self, now: datetime, until: datetime) -> list[Planned]:
        relations = self.relations
        if relations is None:
            return []
        await relations.sync()
        settings = relations.settings
        found: list[Planned] = []
        for meeting in [
            *relations.state.meetings.values(),
            *await relations.calendar_meetings(now),
        ]:
            at = prepared_at(meeting.at, settings)
            if now <= at < until:
                undo = (
                    f"relations (drop, meeting {meeting.key})"
                    if meeting.key in relations.state.meetings
                    else ""
                )
                found.append(
                    Planned(at, f"te rappeler ce qui compte avant de voir {meeting.person}", undo)
                )
        for month, day, _, person in relations.state.birthdays.values():
            birthday = next_birthday(month, day, now.date())
            for before in sorted({settings.birthday_days_before, 0}, reverse=True):
                at = datetime.combine(
                    birthday - timedelta(days=before),
                    settings.morning if before == 0 else time(10, 0),
                    relations.zone,
                )
                if now <= at < until:
                    what = "aujourd'hui" if before == 0 else f"dans {before} jours"
                    found.append(
                        Planned(at, f"te dire que c'est l'anniversaire de {person} {what}")
                    )
        return found

    async def serve(self, daemon: Daemon) -> None:
        from orion.calendar.module import CalendarModule

        if self.relations is None:  # pragma: no cover - assemble always runs first
            return
        calendar = daemon.plate.module(CalendarModule)
        self.relations.calendar = calendar.calendar if calendar is not None else None
        daemon.alerts.append(self.relations)
