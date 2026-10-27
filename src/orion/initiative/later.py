"""One way to say « later » (D157, chantier 3 of the rework).

Orion had five ways to come back to something: `remind_me`, `follow_up`, `think_later`,
`awaiting`, a mission, each with its own words for when and what. The model hesitated between
neighbours, or picked the heavy one (a mission « every 6 h » for what a reminder did). Now it
says only when (a moment, a pace, an answer awaited) and what to do then (tell him, ask him,
think it over, work on it), with `later`; the code picks the mechanism.

`later` is never run: the registry turns each call into the call of the tool it stands for
(`Tool.route`), as the drawer does (D151). The guardian, the journal, the planned list and the
evaluations see `remind_me`, `follow_up`, `awaiting`… as before, and their events do not
change. Those tools leave the model's menu for the drawer, where they stay callable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from orion.initiative.timers import Repeat
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

Then = Literal["tell", "ask", "think", "work", "cancel"]
STANDS_FOR = ("remind_me", "follow_up", "think_later", "awaiting", "reminders")
"""The tools `later` replaces in the model's menu (they go to the drawer)."""


class LaterInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    then: Then = Field(
        description="tell him; ask him how it went; think it over tonight; work on it at a "
        "pace; cancel (`id`)"
    )
    what: str = Field(default="", max_length=1000, description="Self-contained")
    at: str = Field(
        default="",
        description="Local 'YYYY-MM-DD HH:MM'; awaiting: when to come back if nothing came",
    )
    every_hours: int = Field(default=0, ge=0, le=24 * 14, description="work: the pace")
    reply_from: str = Field(
        default="", description="Awaiting: owner, contact:<name>, orion:<name>, mail:<address>"
    )
    context: str = Field(
        default="", max_length=1000, description="Stakes (think), criteria (work), the moment (ask)"
    )
    by_call: bool = Field(default=False, description="tell: by phone (a wake-up call)")
    repeat: Repeat = Field(default="", description="tell: again at that time each day…")
    id: str = Field(
        default="", description="cancel: as `planned` shows it; tell with an r_… and `at`: move it"
    )
    follows: str = Field(default="", description="Awaiting, a nudge: the wait it follows (w_…)")


def _moment(at: str) -> datetime:
    if not at.strip():
        raise ValueError("`at` is needed: when, local 'YYYY-MM-DD HH:MM'")
    try:
        return datetime.fromisoformat(at.strip().replace(" ", "T"))
    except ValueError:
        raise ValueError("`at` must be local 'YYYY-MM-DD HH:MM'") from None


def _needs_what(args: LaterInput) -> str:
    if len(args.what.strip()) < 3:
        raise ValueError("`what` is needed, self-contained")
    return args.what.strip()


def route(args: BaseModel) -> tuple[str, dict[str, Any]]:
    """The call `later` stands for. ValueError says what is wrong with it."""
    assert isinstance(args, LaterInput)
    if args.then == "cancel":
        wanted = args.id.strip()
        why = args.context or "il n'en veut plus"
        if wanted.startswith("r_"):
            return "reminders", {"action": "cancel", "reminder_id": wanted, "reason": why}
        if wanted.startswith("fu_"):
            return "follow_up", {"action": "cancel", "follow_up": wanted}
        if wanted.startswith("w_"):
            return "awaiting", {"action": "cancel", "awaiting": wanted}
        if wanted.startswith("m_"):
            return "mission", {"action": "close", "mission": wanted, "outcome": why}
        raise ValueError("cancel: `id` is one shown by `planned` (r_…, fu_…, w_…, m_…)")
    if args.then == "tell" and args.id.strip().startswith("r_"):
        at = _moment(args.at)
        return "reminders", {
            "action": "move",
            "reminder_id": args.id.strip(),
            "when": f"{at:%Y-%m-%dT%H:%M}",
        }
    if args.repeat and args.then != "tell":
        raise ValueError("`repeat` is for tell; a pace of work is `every_hours`")
    what = _needs_what(args)
    if args.reply_from:
        who, _, name = args.reply_from.partition(":")
        if who not in ("owner", "contact", "orion", "mail"):
            raise ValueError("`reply_from`: 'owner', 'contact:<name>', 'orion:<name>', 'mail:<…>'")
        at = _moment(args.at)
        return "awaiting", {
            "action": "wait",
            "who": who,
            "name": name.strip(),
            "about": what[:300],
            "at": f"{at:%Y-%m-%d %H:%M}",
            "follows": args.follows,
        }
    if args.every_hours or args.then == "work":
        if not args.every_hours:
            raise ValueError(
                "work: give `every_hours` (the pace), or `think` to work on it tonight once"
            )
        return "mission", {
            "action": "start",
            "goal": what[:300],
            "criteria": args.context,
            "every_hours": args.every_hours,
        }
    if args.then == "think":
        return "think_later", {"question": what[:300], "context": args.context}
    at = _moment(args.at)
    if args.then == "ask":
        return "follow_up", {
            "action": "plan",
            "about": (args.context or what)[:200],
            "at": f"{at:%Y-%m-%d %H:%M}",
            "question": what[:300],
        }
    return "remind_me", {
        "when": f"{at:%Y-%m-%dT%H:%M}",
        "what": what,
        "by_call": args.by_call,
        "repeat": args.repeat,
    }


def later_tool() -> Tool:
    async def unresolved(args: Any, ctx: ToolContext) -> ToolResult:
        """Run only when the registry could not turn the call into another."""
        del ctx
        try:
            name, _ = route(args)
        except ValueError as exc:
            return ToolResult(f"Pas prévu : {exc}.", is_error=True)
        return ToolResult(f"Pas prévu : `{name}` n'est pas installé ici.", is_error=True)

    return Tool(
        name="later",
        description="The only way to come back to something later: never promise to remind "
        "him, come back or keep an eye without it. When (`at`, `every_hours`, or `reply_from` "
        "for an answer awaited), then what. « Rappelle-moi jeudi » → tell; « tous les lundis » "
        "→ tell with repeat; how an interview or a doctor went → ask, hours after; « laisse-moi "
        "y réfléchir » → think; following a search over days → work. What can be done now is "
        "done now. Check the weekday it gives back.",
        input_model=LaterInput,
        handler=unresolved,
        effect=Effect.INTERNAL,
        reversibility=Reversibility.REVERSIBLE,
        route=route,
    )
