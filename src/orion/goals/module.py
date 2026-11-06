"""Goals: `[modules.goals]` in orion.toml (on by default, D267).

An intention is what the owner wants to become (run more, D53); a mission is Orion working on
something for days (D47); a commitment is one thing owed. A goal is what he is trying to
achieve: find a flat in Lyon by December, pass the exam, launch the site. It ties the rest to
a result he can check:

- each goal has a state (`proposed`, `active`, `blocked`, `achieved`, `abandoned`), a way to
  tell it is reached (`done_when`), a next action, a date to reconsider it, and optionally a
  deadline, a priority, its real constraints, and the goals it waits for;
- what Orion suggests stays `proposed` until the owner takes it on, and so does anything set
  from a context that is not his word;
- milestones and the goal itself are reached only on evidence: his explicit word, or what Orion
  observed and cites (D258). A goal whose milestones are open, or whose prerequisite is not
  reached, is not achieved;
- commitments, tasks, missions, intentions, situations, procedures are linked by id, never
  copied;
- Sunday's look at the week ahead (D256) and the morning line read them; so does the evening
  review, and curiosity (ORION-10).

    goal.set  goal.changed  goal.state  goal.milestone  goal.reached  goal.linked  goal.noted
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.modules.base import Daemon, Module, ModuleSettings, Plate
from orion.tools.actions import NO_ARGS, Action, action_tool
from orion.tools.base import Effect, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig

SET = "goal.set"
CHANGED = "goal.changed"
STATE = "goal.state"
MILESTONE = "goal.milestone"
REACHED = "goal.reached"
LINKED = "goal.linked"
NOTED = "goal.noted"

REVIEW_AFTER = timedelta(days=30)
"""When a goal is reconsidered if he names no date."""
RECENT = timedelta(days=7)
CHANGEABLE = ("title", "domain", "done_when", "next_action", "context", "constraints")


class GoalState(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    BLOCKED = "blocked"
    ACHIEVED = "achieved"
    ABANDONED = "abandoned"


OPEN = (GoalState.PROPOSED, GoalState.ACTIVE, GoalState.BLOCKED)
_SAID = {
    GoalState.PROPOSED: "suggéré par toi, pas encore accepté",
    GoalState.ACTIVE: "actif",
    GoalState.BLOCKED: "bloqué",
    GoalState.ACHIEVED: "atteint",
    GoalState.ABANDONED: "abandonné",
}
LINK_KINDS = ("commitment", "task", "mission", "intention", "situation", "procedure", "workflow")
_LINK_SAID = {
    "commitment": "engagement",
    "task": "tâche",
    "mission": "mission",
    "intention": "intention",
    "situation": "situation",
    "procedure": "procédure",
    "workflow": "travail",
}


class GoalSettings(ModuleSettings):
    enabled: bool = True


@dataclass
class Milestone:
    id: str
    what: str
    due: date | None
    reached: str = ""
    """The evidence it was reached (« il l'a dit : … », « vu : … »); empty while open."""


@dataclass
class Note:
    kind: str
    text: str
    at: datetime


@dataclass
class Goal:
    id: str
    title: str
    domain: str
    done_when: str
    next_action: str
    review_on: date
    due: date | None
    priority: int
    context: str
    constraints: str
    depends_on: list[str]
    state: GoalState
    since: datetime
    trust: Trust
    why: str = ""
    """Why it is in its state, when it was said (blocked by what, abandoned why)."""
    outcome: str = ""
    """The evidence it was reached."""
    milestones: list[Milestone] = field(default_factory=list[Milestone])
    links: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])
    """(kind, id, a few words) of what serves it elsewhere in Orion."""
    notes: list[Note] = field(default_factory=list[Note])
    history: list[tuple[datetime, GoalState]] = field(
        default_factory=list[tuple[datetime, GoalState]]
    )

    @property
    def open(self) -> bool:
        return self.state in OPEN


def _day(value: object) -> date | None:
    return date.fromisoformat(str(value)) if value else None


@dataclass
class GoalBook:
    name: str = "goals"
    goals: dict[str, Goal] = field(default_factory=dict[str, Goal])
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.goals.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind == SET:
            at = datetime.fromisoformat(str(payload["at"]))
            state = GoalState(str(payload["state"]))
            self.goals[str(payload["goal_id"])] = Goal(
                str(payload["goal_id"]),
                str(payload["title"]),
                str(payload.get("domain", "")),
                str(payload["done_when"]),
                str(payload["next_action"]),
                date.fromisoformat(str(payload["review_on"])),
                _day(payload.get("due")),
                int(payload.get("priority", 2)),
                str(payload.get("context", "")),
                str(payload.get("constraints", "")),
                [str(g) for g in payload.get("depends_on", [])],
                state,
                at,
                event.trust,
                history=[(at, state)],
            )
            return
        if not event.kind.startswith("goal."):
            return  # other modules' events may name a goal (curiosity.found): not ours
        goal = self.goals.get(str(payload.get("goal_id", "")))
        if goal is None:
            return
        goal.trust = min(goal.trust, event.trust)
        if event.kind == CHANGED:
            for name, value in payload.items():
                if name == "review_on":
                    goal.review_on = date.fromisoformat(str(value))
                elif name == "due":
                    goal.due = _day(value)
                elif name == "depends_on":
                    goal.depends_on = [str(g) for g in value]
                elif name == "priority":
                    goal.priority = int(value)
                elif name in CHANGEABLE:
                    setattr(goal, name, str(value))
        elif event.kind == STATE:
            goal.state = GoalState(str(payload["state"]))
            goal.why = str(payload.get("why", ""))
            goal.history.append((datetime.fromisoformat(str(payload["at"])), goal.state))
        elif event.kind == MILESTONE:
            goal.milestones.append(
                Milestone(str(payload["milestone_id"]), str(payload["what"]), _day(payload["due"]))
            )
        elif event.kind == REACHED:
            said = "il l'a dit" if payload["basis"] == "owner" else "vu"
            evidence = f"{said} : {payload['evidence']}"
            milestone_id = str(payload.get("milestone_id", ""))
            if not milestone_id:
                goal.outcome = evidence
                goal.state = GoalState.ACHIEVED
                goal.history.append((datetime.fromisoformat(str(payload["at"])), goal.state))
            for m in goal.milestones:
                if m.id == milestone_id:
                    m.reached = evidence
        elif event.kind == LINKED:
            goal.links.append((str(payload["kind"]), str(payload["ref"]), str(payload["what"])))
        elif event.kind == NOTED:
            goal.notes.append(
                Note(
                    str(payload["kind"]),
                    str(payload["text"]),
                    datetime.fromisoformat(str(payload["at"])),
                )
            )


class GoalError(ValueError):
    """A change that contradicts what is known of the goals: said back to the model."""


class Goals:
    def __init__(
        self, journal: EventStore, timezone: str, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._journal = journal
        self.zone = ZoneInfo(timezone)
        self.clock = clock or (lambda: datetime.now(self.zone))
        self.book = GoalBook()
        self._runner = ProjectionRunner(journal, [self.book])

    async def sync(self) -> None:
        await self._runner.catch_up()

    def get(self, goal_id: str) -> Goal:
        goal = self.book.goals.get(goal_id)
        if goal is None:
            raise GoalError(f"No goal {goal_id}.")
        return goal

    def _check_depends(self, goal_id: str, depends_on: list[str]) -> None:
        for other in depends_on:
            self.get(other)
            seen: set[str] = set()
            todo = [other]
            while todo:
                current = todo.pop()
                if current == goal_id:
                    raise GoalError(
                        f"{other} attend déjà {goal_id} : ils ne peuvent pas s'attendre l'un "
                        "l'autre."
                    )
                if current not in seen:
                    seen.add(current)
                    todo += (
                        self.book.goals[current].depends_on if current in self.book.goals else []
                    )

    async def set(
        self,
        fields: dict[str, Any],
        proposed: bool,
        prov: Provenance,
        trust: Trust,
        said_by_owner: bool,
    ) -> str:
        await self.sync()
        title = str(fields["title"]).strip()
        same = [g for g in self.book.goals.values() if g.open and _same(g.title, title)]
        if same:
            raise GoalError(
                f"Déjà suivi : ({same[0].id}) {same[0].title}. Modifie-le (change) plutôt que "
                "d'en ouvrir un second."
            )
        goal_id = f"g_{new_id()}"
        depends: list[str] = list(fields.get("depends_on") or [])
        self._check_depends(goal_id, depends)
        now = self.clock()
        review_on = fields.get("review_on") or (now.date() + REVIEW_AFTER)
        state = GoalState.PROPOSED if proposed or not said_by_owner else GoalState.ACTIVE
        await self._append(
            SET,
            {
                "goal_id": goal_id,
                "title": title,
                "domain": fields.get("domain") or "",
                "done_when": fields["done_when"],
                "next_action": fields["next_action"],
                "review_on": review_on.isoformat(),
                "due": fields["due"].isoformat() if fields.get("due") else None,
                "priority": fields.get("priority") or 2,
                "context": fields.get("context") or "",
                "constraints": fields.get("constraints") or "",
                "depends_on": depends,
                "state": state.value,
                "at": now.isoformat(),
            },
            prov,
            trust,
        )
        return goal_id

    async def change(
        self, goal_id: str, fields: dict[str, Any], prov: Provenance, trust: Trust
    ) -> None:
        await self.sync()
        goal = self.get(goal_id)
        if not goal.open:
            raise GoalError(f"{goal_id} est {_SAID[goal.state]} : rouvre-le d'abord (state).")
        if "depends_on" in fields:
            self._check_depends(goal_id, [str(g) for g in fields["depends_on"]])
        payload: dict[str, Any] = {"goal_id": goal_id}
        for name, value in fields.items():
            payload[name] = value.isoformat() if isinstance(value, date) else value
        await self._append(CHANGED, payload, prov, trust)

    async def move(
        self,
        goal_id: str,
        state: GoalState,
        why: str,
        prov: Provenance,
        trust: Trust,
        said_by_owner: bool,
    ) -> None:
        """Take on, block, resume, abandon or reopen, on his word. Reaching goes through
        `reach`."""
        await self.sync()
        goal = self.get(goal_id)
        if state is GoalState.ACHIEVED:
            raise GoalError("Un objectif est atteint sur preuve : reached, sans milestone.")
        if not said_by_owner:
            raise GoalError(
                "Seul le propriétaire, dans sa conversation, change l'état d'un objectif."
            )
        if state is GoalState.PROPOSED:
            raise GoalError("Un objectif n'est proposé qu'à sa création.")
        await self._append(
            STATE,
            {"goal_id": goal.id, "state": state.value, "why": why, "at": self.clock().isoformat()},
            prov,
            trust,
        )

    async def milestone(
        self, goal_id: str, what: str, due: date | None, prov: Provenance, trust: Trust
    ) -> str:
        await self.sync()
        goal = self.get(goal_id)
        if not goal.open:
            raise GoalError(f"{goal_id} est {_SAID[goal.state]}.")
        milestone_id = f"j_{new_id()}"
        await self._append(
            MILESTONE,
            {
                "goal_id": goal.id,
                "milestone_id": milestone_id,
                "what": what,
                "due": due.isoformat() if due else None,
            },
            prov,
            trust,
        )
        return milestone_id

    async def reach(
        self,
        goal_id: str,
        milestone_id: str,
        evidence: str,
        basis: str,
        prov: Provenance,
        trust: Trust,
        said_by_owner: bool,
    ) -> str:
        """Mark a milestone, or the goal itself, reached: on his word in his conversation, or on
        what Orion observed (D258). Returns what is said back."""
        await self.sync()
        goal = self.get(goal_id)
        if basis == "owner" and not said_by_owner:
            raise GoalError(
                "Il ne l'a pas dit ici : demande-le-lui, ou cite ce que tu as vu (basis observed)."
            )
        if goal.state is not GoalState.ACTIVE and goal.state is not GoalState.BLOCKED:
            raise GoalError(f"{goal_id} est {_SAID[goal.state]}.")
        if milestone_id:
            milestone = next((m for m in goal.milestones if m.id == milestone_id), None)
            if milestone is None:
                raise GoalError(f"No milestone {milestone_id} in {goal_id}.")
            if milestone.reached:
                return f"Déjà atteint ({milestone.reached})."
        else:
            left = [m.what for m in goal.milestones if not m.reached]
            if left:
                raise GoalError(
                    "Des jalons restent ouverts : " + " ; ".join(left) + ". Marque-les atteints "
                    "sur preuve, ou retire-les avec lui, avant de dire l'objectif atteint."
                )
            waiting = [self.book.goals[d] for d in goal.depends_on if d in self.book.goals]
            waiting = [d for d in waiting if d.state is not GoalState.ACHIEVED]
            if waiting:
                raise GoalError(
                    "Il attend encore : " + " ; ".join(f"({d.id}) {d.title}" for d in waiting) + "."
                )
        await self._append(
            REACHED,
            {
                "goal_id": goal.id,
                "milestone_id": milestone_id,
                "evidence": evidence,
                "basis": basis,
                "at": self.clock().isoformat(),
            },
            prov,
            trust,
        )
        return "Objectif atteint." if not milestone_id else "Jalon atteint."

    async def link(
        self, goal_id: str, kind: str, ref: str, what: str, prov: Provenance, trust: Trust
    ) -> bool:
        """Point to what serves the goal elsewhere; False if it already does."""
        await self.sync()
        goal = self.get(goal_id)
        if any(r == ref for _, r, _ in goal.links):
            return False
        await self._append(
            LINKED, {"goal_id": goal.id, "kind": kind, "ref": ref, "what": what}, prov, trust
        )
        return True

    async def note(
        self, goal_id: str, kind: str, text: str, prov: Provenance, trust: Trust
    ) -> None:
        await self.sync()
        goal = self.get(goal_id)
        await self._append(
            NOTED,
            {"goal_id": goal.id, "kind": kind, "text": text, "at": self.clock().isoformat()},
            prov,
            trust,
        )

    async def _append(
        self, kind: str, payload: dict[str, Any], prov: Provenance, trust: Trust
    ) -> None:
        await self._journal.append(
            [NewEvent(kind=kind, payload=payload, provenance=prov, trust=trust)]
        )
        await self.sync()

    # -- what is read ---------------------------------------------------------------------------

    def active(self) -> list[Goal]:
        """The goals he is working on, most important first: what curiosity serves."""
        return sorted(
            (
                g
                for g in self.book.goals.values()
                if g.state in (GoalState.ACTIVE, GoalState.BLOCKED)
            ),
            key=lambda g: (g.priority, g.due or date.max, g.since),
        )

    def describe(self, goal: Goal, now: datetime) -> str:
        """One goal, as the weekly review reads it: where it stands, what moved, what is in the
        way, the next action and what it waits for."""
        head = f"- ({goal.id}) {goal.title} [{_SAID[goal.state]}, priorité {goal.priority}"
        head += f", {goal.domain}]" if goal.domain else "]"
        parts = [head, f"  réussi quand : {goal.done_when}"]
        if goal.due:
            parts.append(f"  échéance : {goal.due:%d/%m/%Y}")
        if goal.why:
            parts.append(f"  {_SAID[goal.state]} : {goal.why}")
        if goal.outcome:
            parts.append(f"  atteint — {goal.outcome}")
        if goal.milestones:
            done = sum(1 for m in goal.milestones if m.reached)
            parts.append(
                f"  jalons {done}/{len(goal.milestones)} : "
                + " ; ".join(
                    f"{m.what} ({m.id}, " + (m.reached if m.reached else _due(m.due)) + ")"
                    for m in goal.milestones
                )
            )
        recent = [n for n in goal.notes if n.at >= now - RECENT]
        for kind, said in (("progress", "avancées"), ("obstacle", "obstacles")):
            found = [n.text for n in recent if n.kind == kind]
            if found:
                parts.append(f"  {said} cette semaine : " + " ; ".join(found))
        if goal.constraints:
            parts.append(f"  contraintes : {goal.constraints}")
        if goal.open:
            parts.append(f"  prochaine action : {goal.next_action}")
            waits = [self.book.goals[d] for d in goal.depends_on if d in self.book.goals]
            waits = [d for d in waits if d.state is not GoalState.ACHIEVED]
            if waits:
                parts.append("  attend : " + " ; ".join(f"({d.id}) {d.title}" for d in waits))
            late = " (dépassé)" if goal.review_on < now.date() else ""
            parts.append(f"  à réévaluer le {goal.review_on:%d/%m}{late}")
        if goal.links:
            parts.append(
                "  lié à : "
                + " ; ".join(f"{_LINK_SAID.get(k, k)} {r} ({w})" for k, r, w in goal.links)
            )
        if goal.context:
            parts.append(f"  contexte : {goal.context}")
        return "\n".join(parts)

    async def lines(self, now: datetime) -> list[str]:
        """Each open goal, and those reached or dropped this week, for the weekly review and the
        evening one. Trusted content only: a goal touched by outside content is left to `list`."""
        await self.sync()
        shown = [
            g
            for g in self.book.goals.values()
            if g.trust >= Trust.SYSTEM and (g.open or g.history[-1][0] >= now - RECENT)
        ]
        shown.sort(key=lambda g: (g.state is GoalState.PROPOSED, g.priority, g.since))
        return [self.describe(g, now) for g in shown]

    async def soon(self, now: datetime, days: int = 2) -> list[str]:
        """For the morning line: what falls due, or is to be reconsidered, in the next days."""
        await self.sync()
        until = now.date() + timedelta(days=days)
        found: list[str] = []
        for goal in self.active():
            if goal.trust < Trust.SYSTEM:
                continue
            if goal.due is not None and goal.due < until:
                found.append(f"- objectif « {goal.title} » : échéance le {goal.due:%d/%m}")
            if goal.review_on < until:
                found.append(f"- objectif « {goal.title} » : à réévaluer ({goal.next_action} ?)")
            found += [
                f"- jalon « {m.what} » ({goal.title}) : prévu le {m.due:%d/%m}"
                for m in goal.milestones
                if not m.reached and m.due is not None and m.due < until
            ]
        return found


def _same(a: str, b: str) -> bool:
    return " ".join(a.lower().split()) == " ".join(b.lower().split())


def _due(day: date | None) -> str:
    return f"prévu le {day:%d/%m}" if day else "à faire"


# -- the tool ------------------------------------------------------------------------------------


class SetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=3, max_length=200)
    done_when: str = Field(min_length=3, max_length=300, description="How to tell it is reached")
    next_action: str = Field(min_length=3, max_length=300)
    review_on: date | None = Field(default=None, description="Reconsider on (+30 d)")
    due: date | None = None
    priority: int = Field(default=2, ge=1, le=3)
    domain: str = Field(default="", max_length=60)
    context: str = Field(default="", max_length=500)
    constraints: str = Field(default="", max_length=300)
    depends_on: list[str] = Field(default_factory=list[str], description="Goal ids")
    suggested: bool = Field(default=False, description="Your idea, not his")


class ChangeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(description="g_…")
    title: str | None = Field(default=None, min_length=3, max_length=200)
    done_when: str | None = Field(default=None, min_length=3, max_length=300)
    next_action: str | None = Field(default=None, min_length=3, max_length=300)
    review_on: date | None = None
    due: date | None = None
    priority: int | None = Field(default=None, ge=1, le=3)
    domain: str | None = Field(default=None, max_length=60)
    context: str | None = Field(default=None, max_length=500)
    constraints: str | None = Field(default=None, max_length=300)
    depends_on: list[str] | None = None


class StateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str
    state: Literal["active", "blocked", "abandoned"] = Field(description="blocked: also suspended")
    why: str = Field(default="", max_length=300)


class MilestoneInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str
    what: str = Field(min_length=3, max_length=200)
    due: date | None = None


class ReachedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str
    milestone: str = Field(default="", description="j_…; empty: the goal")
    basis: Literal["owner", "observed"] = Field(description="observed: you saw it, cite it")
    evidence: str = Field(min_length=3, max_length=300)


class LinkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str
    kind: Literal[
        "commitment", "task", "mission", "intention", "situation", "procedure", "workflow"
    ]
    ref: str = Field(min_length=2, max_length=80)
    what: str = Field(default="", max_length=120)


class NoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str
    kind: Literal["progress", "obstacle"]
    text: str = Field(min_length=3, max_length=300)


def goal_tool(goals: Goals) -> Tool:
    def where(ctx: ToolContext) -> Provenance:
        return Provenance(
            source=f"thread:{ctx.thread_id}",
            actor="orion:core",
            derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
        )

    def his_word(ctx: ToolContext) -> bool:
        return ctx.context_trust >= Trust.OWNER or ctx.unrestricted

    async def guarded(run: Callable[[], Any]) -> ToolResult:
        try:
            return ToolResult(str(await run()))
        except GoalError as exc:
            return ToolResult(str(exc), is_error=True)

    async def set_(args: Any, ctx: ToolContext) -> ToolResult:
        fields = args.model_dump(exclude={"suggested"})

        async def run() -> str:
            goal_id = await goals.set(
                fields, args.suggested, where(ctx), ctx.context_trust, his_word(ctx)
            )
            if goals.get(goal_id).state is GoalState.PROPOSED:
                return f"Proposé ({goal_id}) : il l'accepte, et state active."
            return f"Objectif suivi ({goal_id})."

        return await guarded(run)

    async def change(args: Any, ctx: ToolContext) -> ToolResult:
        fields = args.model_dump(exclude={"goal"}, exclude_none=True)

        async def run() -> str:
            await goals.change(args.goal, fields, where(ctx), ctx.context_trust)
            return "Modifié."

        return await guarded(run)

    async def state(args: Any, ctx: ToolContext) -> ToolResult:
        async def run() -> str:
            await goals.move(
                args.goal,
                GoalState(args.state),
                args.why,
                where(ctx),
                ctx.context_trust,
                his_word(ctx),
            )
            return f"Objectif {_SAID[GoalState(args.state)]}."

        return await guarded(run)

    async def milestone(args: Any, ctx: ToolContext) -> ToolResult:
        async def run() -> str:
            found = await goals.milestone(
                args.goal, args.what, args.due, where(ctx), ctx.context_trust
            )
            return f"Jalon ajouté ({found})."

        return await guarded(run)

    async def reached(args: Any, ctx: ToolContext) -> ToolResult:
        async def run() -> str:
            return await goals.reach(
                args.goal,
                args.milestone,
                args.evidence,
                args.basis,
                where(ctx),
                ctx.context_trust,
                his_word(ctx),
            )

        return await guarded(run)

    async def link(args: Any, ctx: ToolContext) -> ToolResult:
        async def run() -> str:
            new = await goals.link(
                args.goal, args.kind, args.ref, args.what, where(ctx), ctx.context_trust
            )
            return "Relié." if new else "Déjà relié."

        return await guarded(run)

    async def note(args: Any, ctx: ToolContext) -> ToolResult:
        async def run() -> str:
            await goals.note(args.goal, args.kind, args.text, where(ctx), ctx.context_trust)
            return "Noté."

        return await guarded(run)

    async def listing(_args: Any, _ctx: ToolContext) -> ToolResult:
        await goals.sync()
        now = goals.clock()
        shown = [g for g in goals.book.goals.values() if g.open]
        if not shown:
            return ToolResult("Aucun objectif ouvert.")
        return ToolResult(
            "\n".join(goals.describe(g, now) for g in shown), trust=min(g.trust for g in shown)
        )

    return action_tool(
        "goal",
        "The owner's goals: results he is after (find a flat, pass an exam, launch a site), "
        "with milestones. Not habits (intention), not your own work (mission).",
        [
            Action("set", "Follow a new goal (yours to suggest: suggested).", SetInput, set_),
            Action("change", "Change fields.", ChangeInput, change),
            Action("state", "Take on, suspend (blocked), resume, abandon.", StateInput, state),
            Action("milestone", "Add a step on the way.", MilestoneInput, milestone),
            Action(
                "reached",
                "A milestone, or the goal, is reached: only on his word or what you saw.",
                ReachedInput,
                reached,
            ),
            Action("link", "Tie a commitment, task, mission… to it (by id).", LinkInput, link),
            Action("note", "Progress or an obstacle he mentions.", NoteInput, note),
            Action(
                "list",
                "His open goals, where each stands.",
                NO_ARGS,
                listing,
                effect=Effect.NONE,
                accepts_untrusted_context=True,
            ),
        ],
    )


class GoalsModule(Module):
    name = "goals"
    instructions = "goals.md"
    settings_model = GoalSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.goals: Goals | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        self.goals = Goals(plate.journal, self.config.agent.timezone)
        await self.goals.sync()
        return [goal_tool(self.goals)]

    async def review(self, now: datetime) -> list[str]:
        if self.goals is None:
            return []
        return await self.goals.lines(now)

    async def serve(self, daemon: Daemon) -> None:
        """Sunday's week ahead and the morning line read the goals (D256)."""
        from orion.chronicle.module import ChronicleModule
        from orion.thinking.module import ThinkingModule

        goals = self.goals
        if goals is None:  # pragma: no cover - assemble always runs first
            return
        chronicle = daemon.plate.module(ChronicleModule)
        if chronicle is not None and chronicle.chronicle is not None:
            chronicle.chronicle.ahead.append((Trust.SYSTEM, goals.lines))
        thinking = daemon.plate.module(ThinkingModule)
        if thinking is not None and thinking.thinking is not None:
            thinking.thinking.ahead.append((Trust.SYSTEM, goals.soon))
