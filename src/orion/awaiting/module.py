"""Waiting for an answer: `[modules.awaiting]` in orion.toml (on by default, D109).

Orion sends things that call for an answer: a question to a contact, a message to another Orion,
a mail, a question to the owner himself. An assistant usually forgets them. Orion does not:

- **It decides what it waits for.** `awaiting` (wait) records who should answer, about what,
  and when to come back to it if nothing comes. A later nudge names the one before it
  (`follows`): no more than `max_rounds` on one subject.
- **An answer closes it by itself.** The projection watches the journal: a contact's message
  (the secretary), anything from that Orion, a mail from that address. Not the owner's next
  message, which may be about something else: when the moment comes, Orion rereads the
  conversation and keeps quiet if he answered.
- **When the moment comes with no answer, Orion decides**, in an initiative turn: nudge
  (through the usual tools, so the owner's approvals and the autonomy he gave apply), tell the
  owner, or let it go (`SILENCE`).
- **Some waits open by themselves**: a request for the owner's approval left unanswered (no
  answer meant no, D26) comes back once, a few hours later; a meeting proposed to another Orion
  that has not answered comes back after a day.

Everything is rebuilt from the journal; the only events are the waits Orion opens and those
it cancels. Reaching the owner goes through the initiative engine (quiet hours, budget,
verifier).

    awaiting.opened  awaiting.cancelled
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.hlc import Timestamp
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.initiative.engine import Alert
from orion.modules.base import Daemon, Module, ModuleSettings, Planned, Plate
from orion.tools.actions import grouped
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig

OPENED = "awaiting.opened"
CANCELLED = "awaiting.cancelled"

CONTACT_WROTE = "secretary.replied"
"""Journaled for each message a contact writes, whether or not the secretary answered."""
PEER_WROTE = "peer.message.received"
MAIL_CAME = "mail.card"
APPROVAL_ASKED = "approval.requested"
APPROVAL_DECIDED = "approval.decided"
MEETING_PROPOSED = "meeting.proposed"

Who = Literal["owner", "contact", "orion", "mail"]
EXPIRES = timedelta(hours=36)
MAX_AHEAD = timedelta(days=30)
_WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


class AwaitingSettings(ModuleSettings):
    enabled: bool = True
    max_rounds: int = Field(
        default=2, ge=1, le=5, description="Nudges on one subject before Orion lets it go"
    )
    approval_after_hours: float = Field(
        default=3.0,
        ge=0,
        description="An approval request left unanswered comes back after this long; 0 never",
    )
    meeting_after_hours: float = Field(
        default=24.0,
        ge=0,
        description="A meeting another Orion has not answered comes back after this long; 0 never",
    )


@dataclass(frozen=True)
class Wait:
    id: str
    who: Who
    name: str
    """The contact's name, the Orion's name or instance, the address; empty for the owner."""
    about: str
    since: datetime
    at: datetime
    round: int = 1
    auto: str = ""
    """'approval' or 'meeting' when it opened by itself."""
    trust: Trust = Trust.SYSTEM


def event_time(event: Event) -> datetime:
    return datetime.fromtimestamp(Timestamp.decode(event.hlc).wall_ms / 1000, UTC)


@dataclass
class AwaitingState:
    name: str = "awaiting"
    approval_after: timedelta = timedelta(hours=3)
    meeting_after: timedelta = timedelta(hours=24)
    waits: dict[str, Wait] = field(default_factory=dict[str, Wait])
    open: set[str] = field(default_factory=set[str])
    """Waits no answer, cancellation or later nudge has closed (they may have fired)."""
    labels: dict[str, str] = field(default_factory=dict[str, str])
    """Another Orion's instance → the name the owner paired it under."""
    asked: dict[str, tuple[str, str]] = field(default_factory=dict[str, tuple[str, str]])
    """Approval requests not decided yet: id → (tool, arguments)."""
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.waits.clear()
        self.open.clear()
        self.labels.clear()
        self.asked.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        p: dict[str, Any] = event.payload
        kind = event.kind
        if p.get("peer") and p.get("label"):
            self.labels[str(p["peer"])] = str(p["label"])
        if kind == OPENED:
            wait = Wait(
                str(p["awaiting_id"]),
                cast(Who, p["who"]),
                str(p.get("name", "")),
                str(p["about"]),
                datetime.fromisoformat(str(p["since"])),
                datetime.fromisoformat(str(p["at"])),
                int(p.get("round", 1)),
                trust=event.trust,
            )
            self.open.discard(str(p.get("follows", "")))  # a nudge takes over from its wait
            self._add(wait)
        elif kind == CANCELLED:
            self.open.discard(str(p["awaiting_id"]))
        elif kind == CONTACT_WROTE:
            self._answered("contact", str(p.get("contact", "")))
        elif kind == PEER_WROTE:
            sender = str(p.get("envelope", {}).get("sender", ""))
            self._answered("orion", sender, self.labels.get(sender, ""))
        elif kind == MAIL_CAME:
            self._answered("mail", str(p.get("from_addr", "")))
        elif kind == APPROVAL_ASKED:
            self.asked[str(p["approval_id"])] = (str(p["tool"]), str(p.get("arguments", "")))
        elif kind == APPROVAL_DECIDED:
            self._approval(event, p)
        elif kind == MEETING_PROPOSED and self.meeting_after:
            peer = str(p["peer"])
            since = event_time(event)
            what = "un sondage de disponibilités" if p.get("poll") else "une proposition"
            wait = Wait(
                f"meeting:{p['meeting_id']}",
                "orion",
                peer,
                f"{what} de rendez-vous « {p['title']} »",
                since,
                since + self.meeting_after,
                auto="meeting",
                trust=event.trust,
            )
            self._add(wait)

    def _add(self, wait: Wait) -> None:
        self.waits[wait.id] = wait
        self.open.add(wait.id)

    def pending(self) -> list[Wait]:
        return sorted((self.waits[i] for i in self.open), key=lambda w: w.at)

    def _approval(self, event: Event, p: dict[str, Any]) -> None:
        asked = self.asked.pop(str(p["approval_id"]), None)
        if asked is None:
            return
        tool, arguments = asked
        if p.get("how") != "timeout":
            # He answered something: an earlier unanswered request for the same action is moot.
            for wait in self.pending():
                if wait.auto == "approval" and wait.about.startswith(f"{tool}("):
                    self.open.discard(wait.id)
            return
        if not self.approval_after:
            return
        since = event_time(event)
        wait = Wait(
            f"approval:{p['approval_id']}",
            "owner",
            "",
            f"{tool}({arguments[:400]})",
            since,
            since + self.approval_after,
            auto="approval",
        )
        # Not closed by his next message: a message about something else is not an answer.
        self._add(wait)

    def _answered(self, who: Who, name: str, label: str = "") -> None:
        names = {n.casefold() for n in (name, label) if n}
        for wait in self.pending():
            if wait.who == who and wait.name.casefold() in names:
                self.open.discard(wait.id)


class Awaiting:
    def __init__(
        self,
        journal: EventStore,
        settings: AwaitingSettings,
        timezone: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._journal = journal
        self.settings = settings
        self.zone = ZoneInfo(timezone)
        self.clock = clock or (lambda: datetime.now(self.zone))
        self.state = AwaitingState(
            approval_after=timedelta(hours=settings.approval_after_hours),
            meeting_after=timedelta(hours=settings.meeting_after_hours),
        )
        self._runner = ProjectionRunner(journal, [self.state])

    async def sync(self) -> None:
        await self._runner.catch_up()

    async def wait(
        self,
        who: Who,
        name: str,
        about: str,
        at: datetime,
        follows: str,
        provenance: Provenance,
        trust: Trust,
        cid: str | None,
    ) -> Wait:
        """Raises ValueError when the wait makes no sense (past, too far, too many rounds)."""
        await self.sync()
        now = self.clock()
        if not now < at <= now + MAX_AHEAD:
            raise ValueError("le moment doit être dans le futur, à 30 jours au plus")
        if who != "owner" and not name.strip():
            raise ValueError("dis qui doit répondre (le nom du proche, de l'Orion, l'adresse)")
        previous = self.state.waits.get(follows) if follows else None
        if follows and previous is None:
            raise ValueError(f"aucune attente {follows}")
        rounds = previous.round + 1 if previous else 1
        if rounds > self.settings.max_rounds + 1:
            raise ValueError(
                f"déjà {rounds - 1} relances sur ce sujet : n'insiste plus, dis-le-lui"
            )
        wait_id = f"w_{new_id()}"
        await self._append(
            OPENED,
            {
                "awaiting_id": wait_id,
                "who": who,
                "name": "" if who == "owner" else name.strip(),
                "about": about,
                "since": now.isoformat(),
                "at": at.isoformat(),
                "round": rounds,
                "follows": follows,
            },
            provenance,
            trust,
            cid,
        )
        return self.state.waits[wait_id]

    async def cancel(self, wait_id: str, provenance: Provenance, cid: str | None) -> bool:
        await self.sync()
        if wait_id not in self.state.open:
            return False
        await self._append(CANCELLED, {"awaiting_id": wait_id}, provenance, Trust.SYSTEM, cid)
        return True

    async def _append(
        self,
        kind: str,
        payload: dict[str, Any],
        provenance: Provenance,
        trust: Trust,
        cid: str | None,
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

    async def lines(self, now: datetime) -> list[str]:
        """What Orion waits an answer for, with ids."""
        await self.sync()
        return [
            f"- ({w.id}) {self.describe(w)} : {w.about}, à reprendre le "
            f"{w.at.astimezone(self.zone):%d/%m %H:%M}"
            + (f", relance {w.round - 1}" if w.round > 1 else "")
            for w in self.state.pending()
            if now < w.at + EXPIRES
        ]

    def describe(self, wait: Wait) -> str:
        if wait.who == "owner":
            return "lui"
        if wait.who == "contact":
            return wait.name
        if wait.who == "orion":
            return f"l'Orion de {self.state.labels.get(wait.name, wait.name)}"
        return wait.name

    async def alerts(self, now: datetime) -> list[Alert]:
        await self.sync()
        return [self._alert(w) for w in self.state.pending() if w.at <= now < w.at + EXPIRES]

    def _alert(self, wait: Wait) -> Alert:
        since = wait.since.astimezone(self.zone)
        when = f"{_WEEKDAYS[since.weekday()]} {since:%d/%m à %H:%M}"
        last = wait.round > self.settings.max_rounds
        if wait.auto == "approval":
            notice = (
                f"Le {when}, tu lui as demandé son accord pour {wait.about}. Il n'a pas répondu "
                "(c'était donc non). Si ça compte encore et que vous n'en avez pas reparlé "
                "depuis, demande-lui en une phrase s'il le veut toujours ; s'il dit oui, refais "
                "l'action. Sinon, réponds SILENCE."
            )
            return Alert(
                key=f"awaiting:{wait.id}",
                notice=notice,
                why="une question restée sans réponse",
                fallback="Tu n'as pas répondu à ma demande de tout à l'heure : tu veux toujours ?",
                trust=wait.trust,
                expires_at=wait.at + EXPIRES,
                importance=0.35,
            )
        if wait.who == "owner":
            notice = (
                f"Le {when}, tu attendais sa réponse : {wait.about}. Rien depuis. Si ça compte "
                "encore, relance-le en une phrase, sans reproche ; s'il a répondu autrement ou "
                "que ça ne vaut plus la peine, réponds SILENCE."
            )
            fallback = f"Je reviens sur {wait.about} : tu en dis quoi ?"
        else:
            who = self.describe(wait)
            how = {
                "contact": "`contact_message`",
                "orion": "`peer_message`",
                "mail": "`mail_send`",
            }[wait.who]
            notice = (
                f"Depuis le {when}, tu attends une réponse de {who} : {wait.about}. Rien n'est "
                "arrivé. C'est à toi de décider : relancer avec "
                f"{how} (le message exact, court, sans reproche ; s'il faut son accord, il lui "
                "sera demandé), ou lui dire que ça n'a pas répondu et ce que tu proposes, ou "
                "laisser tomber si ça ne compte plus (réponds alors SILENCE). "
                + (
                    "C'était déjà la dernière relance : ne relance plus, dis-le-lui."
                    if last
                    else "Si tu relances, attends la suite avec `later` (reply_from, "
                    f"follows={wait.id})."
                )
            )
            fallback = f"Pas de réponse de {who} : {wait.about}."
        return Alert(
            key=f"awaiting:{wait.id}",
            notice=notice,
            why="pas de réponse",
            fallback=fallback,
            trust=wait.trust,
            expires_at=wait.at + EXPIRES,
            importance=0.45,
        )


def local_time(text: str, zone: ZoneInfo) -> datetime:
    """'2026-10-23 18:00' (or ISO with a T), in the owner's time zone."""
    moment = datetime.fromisoformat(text.strip().replace(" ", "T"))
    return moment.replace(tzinfo=zone) if moment.tzinfo is None else moment.astimezone(zone)


class WaitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    who: Who = Field(description="owner, contact, orion or mail")
    name: str = Field(
        default="",
        max_length=200,
        description="The contact's or the Orion's name, or the address",
    )
    about: str = Field(
        min_length=3, max_length=300, description="What you are waiting for, in a sentence"
    )
    at: str = Field(
        description="When to come back if nothing came, local 'YYYY-MM-DD HH:MM' (hours for "
        "a message, days for a mail)"
    )
    follows: str = Field(default="", description="For a nudge: the wait it follows (w_…)")


class CancelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    awaiting: str = Field(description="The wait's id")


class ListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def awaiting_tools(awaiting: Awaiting) -> list[Tool]:
    def provenance(ctx: ToolContext) -> Provenance:
        return Provenance(
            source=f"thread:{ctx.thread_id}",
            actor="orion:core",
            derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
        )

    async def wait(args: Any, ctx: ToolContext) -> ToolResult:
        try:
            at = local_time(args.at, awaiting.zone)
            made = await awaiting.wait(
                args.who,
                args.name,
                args.about,
                at,
                args.follows,
                provenance(ctx),
                ctx.context_trust,
                ctx.correlation_id,
            )
        except ValueError as exc:
            return ToolResult(f"Pas enregistré : {exc}", is_error=True)
        day = made.at.astimezone(awaiting.zone)
        return ToolResult(
            f"J'attends ({made.id}). Sans réponse de {awaiting.describe(made)} d'ici "
            f"{_WEEKDAYS[day.weekday()]} {day:%d/%m %H:%M}, tu y reviendras. Pas besoin de le "
            "lui annoncer."
        )

    async def cancel(args: Any, ctx: ToolContext) -> ToolResult:
        if await awaiting.cancel(args.awaiting, provenance(ctx), ctx.correlation_id):
            return ToolResult("Attente retirée.")
        return ToolResult("Aucune attente ouverte avec cet identifiant.", is_error=True)

    async def listing(_args: Any, _ctx: ToolContext) -> ToolResult:
        lines = await awaiting.lines(awaiting.clock())
        return ToolResult("\n".join(lines) or "Rien en attente.")

    return [
        Tool(
            name="awaiting_wait",
            description="Wait for an answer.",
            input_model=WaitInput,
            handler=wait,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
        Tool(
            name="awaiting_cancel",
            description="Stop waiting.",
            input_model=CancelInput,
            handler=cancel,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
            accepts_untrusted_context=True,
        ),
        Tool(
            name="awaiting_list",
            description="The answers you are waiting for.",
            input_model=ListInput,
            handler=listing,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        ),
    ]


class AwaitingModule(Module):
    name = "awaiting"
    instructions = "awaiting.md"
    settings_model = AwaitingSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.awaiting: Awaiting | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        self.awaiting = Awaiting(plate.journal, self.settings, self.config.agent.timezone)
        await self.awaiting.sync()
        return [
            grouped(
                "awaiting",
                "Answers you wait for (from him, a contact, another Orion, a mail), to come "
                "back to if none comes.",
                awaiting_tools(self.awaiting),
                {"awaiting_wait": "wait", "awaiting_cancel": "cancel", "awaiting_list": "list"},
            )
        ]

    async def serve(self, daemon: Daemon) -> None:
        if self.awaiting is None:  # pragma: no cover - assemble always runs first
            return
        daemon.alerts.append(self.awaiting)

    async def review(self, now: datetime) -> list[str]:
        if self.awaiting is None:
            return []
        return await self.awaiting.lines(now)

    async def planned(self, now: datetime, until: datetime) -> list[Planned]:
        awaiting = self.awaiting
        if awaiting is None:
            return []
        await awaiting.sync()
        return [
            Planned(
                w.at,
                f"revenir sur {w.about}, si {awaiting.describe(w)} n'a pas répondu",
                f"later (cancel, {w.id})",
            )
            for w in awaiting.state.pending()
            if now <= w.at < until
        ]

    def doctor(self) -> list[str]:
        settings: AwaitingSettings = self.settings
        return [f"at most {settings.max_rounds} nudges per subject"]
