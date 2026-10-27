"""Autonomy that is earned: action by action, granted by the owner, lost at the first doubt.

An action that needs the owner's approval (adding to the calendar…) can become one Orion does
alone, with three rules:

- only actions listed in `[guardian] autonomable` can ever be granted, any action but adding
  code to Orion when it holds `*` (D167, the default); once granted, it holds even with outside
  content in the conversation (D168), but it is granted only from a clean one;
- it is offered, not taken: after `offer_after` approvals in a row, Orion asks once, in a
  monthly ritual rather than an interruption, for an action listed by name; any other is
  granted only when the owner asks for it. His yes is checked by the guardian process like any
  approval (D28), which then records the grant itself;
- the owner takes it back with a word (`autonomy_revoke`), and Orion gives it back at the
  first sign the owner did not want what it did.

    autonomy.offered  autonomy.granted  autonomy.revoked
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.guardian.client import GuardianClient, GuardianUnavailableError
from orion.guardian.limits import REQUIRED, Limits, missing, within
from orion.guardian.policy import ANY_ACTION, ActionProposal, Approver, may_be_alone
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

log = structlog.get_logger(__name__)

OFFERED = "autonomy.offered"
GRANTED = "autonomy.granted"
REVOKED = "autonomy.revoked"
APPROVAL_REQUESTED = "approval.requested"
APPROVAL_DECIDED = "approval.decided"
GRANT_ACTION = "autonomy_grant"
RITUAL_EVERY = timedelta(days=30)

LABELS = {
    "calendar_add": "ajouter des événements à ton agenda",
    "delegate": "confier une tâche à l'agent de code",
    "mail_send": "envoyer des mails",
    "mail_tidy": "archiver tes mails et te désabonner des newsletters",
    "configure_change": "changer tes propres réglages",
    "contact_message": "écrire à tes proches",
    "shell": "lancer des commandes sur mon serveur (en root)",
}


def label(action: str) -> str:
    return LABELS.get(action, action)


@dataclass
class AutonomyState:
    name: str = "autonomy"
    granted: set[str] = field(default_factory=set[str])
    limits: dict[str, Limits] = field(default_factory=dict[str, Limits])
    streak: dict[str, int] = field(default_factory=dict[str, int])
    """Approvals in a row per action, since the last refusal or revocation."""
    last_offer: datetime | None = None
    _asked: dict[str, str] = field(default_factory=dict[str, str])
    """approval id → action, until the answer arrives."""
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.granted.clear()
        self.limits.clear()
        self.streak.clear()
        self.last_offer = None
        self._asked.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind == APPROVAL_REQUESTED:
            self._asked[str(payload["approval_id"])] = str(payload["tool"])
        elif event.kind == APPROVAL_DECIDED:
            action = self._asked.pop(str(payload["approval_id"]), None)
            if action is None or action == GRANT_ACTION:
                return
            if payload.get("approved"):
                self.streak[action] = self.streak.get(action, 0) + 1
            elif payload.get("how") != "timeout":
                self.streak[action] = 0
        elif event.kind == OFFERED:
            self.last_offer = datetime.fromisoformat(str(payload["at"]))
        elif event.kind == GRANTED:
            self.granted.add(str(payload["action"]))
            self.limits[str(payload["action"])] = Limits.model_validate(payload.get("limits") or {})
        elif event.kind == REVOKED:
            action = str(payload["action"])
            self.granted.discard(action)
            self.limits.pop(action, None)
            self.streak[action] = 0


class GrantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(description="The tool Orion would use alone from now on")
    approved_times: int = Field(default=0, ge=0, description="How many times the owner said yes")
    recipients: list[str] = Field(
        default_factory=list[str],
        description="mail_send: only to these addresses, or '@domain' (required for mail); "
        "contact_message: the contacts' names (required; give them all, it replaces the "
        "earlier list)",
    )


class RevokeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    reason: str = Field(default="", max_length=200)


class StatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Autonomy:
    def __init__(
        self,
        journal: EventStore,
        autonomable: list[str],
        guardian: GuardianClient | None = None,
        offer_after: int = 5,
        timezone: str = "Europe/Paris",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._journal = journal
        self.autonomable = list(autonomable)
        self._guardian = guardian
        self._offer_after = offer_after
        self._zone = ZoneInfo(timezone)
        self._clock = clock or (lambda: datetime.now(self._zone))
        self.state = AutonomyState()
        self._runner = ProjectionRunner(journal, [self.state])

    async def start(self) -> None:
        await self._runner.catch_up()

    def may(self, action: str) -> bool:
        """Whether the owner may let Orion do this alone."""
        return may_be_alone(action, self.autonomable)

    def shown(self) -> list[str]:
        """The actions to show him: those listed by name, then those he granted besides."""
        listed = [a for a in self.autonomable if a != ANY_ACTION]
        return listed + sorted(a for a in self.state.granted if a not in listed and self.may(a))

    async def allows(self, action: str, params: dict[str, Any] | None = None) -> bool:
        """Granted, and these parameters stay within the limits it was granted with."""
        await self._runner.catch_up()
        if not self.may(action) or action not in self.state.granted:
            return False
        bounds = self.state.limits.get(action)
        return within(action, None if bounds is None or bounds.empty else bounds, params or {})

    async def candidates(self) -> list[tuple[str, int]]:
        await self._runner.catch_up()
        return [
            (action, self.state.streak.get(action, 0))
            for action in self.autonomable
            if action != ANY_ACTION
            and self.may(action)
            and action not in self.state.granted
            and action not in REQUIRED  # those are granted with limits the owner states
            and self.state.streak.get(action, 0) >= self._offer_after
        ]

    # -- changes ------------------------------------------------------------------------------

    async def grant(
        self,
        action: str,
        grant: str | None,
        correlation_id: str | None,
        limits: Limits | None = None,
    ) -> None:
        if not self.may(action):
            raise ValueError(f"{action} can never be done without asking")
        bounds = limits or Limits()
        unbounded = missing(action, bounds)
        if unbounded is not None:
            raise ValueError(unbounded)
        if self._guardian is not None:
            await self._guardian.post(
                "/v1/autonomy/grant",
                {"action": action, "limits": bounds.model_dump()},
                headers=_grant_header(grant),
            )
        await self._record(
            GRANTED, {"action": action, "limits": bounds.model_dump()}, Trust.OWNER, correlation_id
        )

    async def revoke(self, action: str, reason: str, correlation_id: str | None) -> None:
        if self._guardian is not None:
            await self._guardian.post("/v1/autonomy/revoke", {"action": action})
        await self._record(
            REVOKED, {"action": action, "reason": reason}, Trust.OWNER, correlation_id
        )

    async def ritual(
        self, approver: Approver, grant_tool: Tool, quiet: Callable[[time], bool]
    ) -> str | None:
        """At most once a month, at a calm hour: offer one earned autonomy. Returns the action
        granted, if any."""
        now = self._clock()
        if quiet(now.time()) or not time(10, 0) <= now.time() < time(20, 0):
            return None
        last = self.state.last_offer
        if last is not None and now - last < RITUAL_EVERY:
            return None
        found = await self.candidates()
        if not found:
            return None
        action, times = found[0]
        await self._record(OFFERED, {"action": action, "at": now.isoformat()}, Trust.SYSTEM, None)
        arguments = GrantInput(action=action, approved_times=times)
        approval = await approver.approve(
            ActionProposal(grant_tool, arguments, Trust.OWNER, "owner")
        )
        if not approval:
            return None
        try:
            await self.grant(action, approval.grant, None)
        except GuardianUnavailableError:
            log.warning("autonomy.grant_refused", action=action, exc_info=True)
            return None
        return action

    async def run(
        self,
        approver: Approver,
        grant_tool: Tool,
        quiet: Callable[[time], bool],
        every: float = 3600.0,
    ) -> None:
        import asyncio

        while True:
            await self.ritual(approver, grant_tool, quiet)
            await asyncio.sleep(every)

    async def _record(
        self, kind: str, payload: dict[str, object], trust: Trust, correlation_id: str | None
    ) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=Provenance(source="autonomy", actor="orion:core"),
                    trust=trust,
                    correlation_id=correlation_id,
                )
            ]
        )
        await self._runner.catch_up()


def _grant_header(grant: str | None) -> dict[str, str] | None:
    from orion.channels.inkbox import GRANT_HEADER

    return {GRANT_HEADER: grant} if grant else None


def autonomy_tools(autonomy: Autonomy) -> list[Tool]:
    async def grant(args: Any, ctx: ToolContext) -> ToolResult:
        try:
            await autonomy.grant(
                args.action,
                ctx.grant,
                ctx.correlation_id,
                Limits(recipients=args.recipients),
            )
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True)
        except GuardianUnavailableError as exc:
            return ToolResult(f"Refused: {exc}", is_error=True)
        bounds = Limits(recipients=args.recipients).describe()
        return ToolResult(
            f"Désormais je peux {label(args.action)}{f' ({bounds})' if bounds else ''} sans "
            "lui demander."
        )

    async def revoke(args: Any, ctx: ToolContext) -> ToolResult:
        await autonomy.revoke(args.action, args.reason, ctx.correlation_id)
        return ToolResult(f"Je lui redemanderai avant de {label(args.action)}.")

    async def status(_args: Any, _ctx: ToolContext) -> ToolResult:
        await autonomy.start()
        lines = [
            f"- {label(a)} ({a}) : "
            + (
                "sans demander"
                + (
                    f" ({autonomy.state.limits[a].describe()})"
                    if a in autonomy.state.limits and not autonomy.state.limits[a].empty
                    else ""
                )
                if a in autonomy.state.granted
                else f"je demande (oui {autonomy.state.streak.get(a, 0)} fois de suite)"
            )
            for a in autonomy.shown()
        ]
        if ANY_ACTION in autonomy.autonomable:
            lines.append(
                "- toute autre action (sauf ajouter du code à Orion) : je demande, et il peut "
                "me dire de ne plus demander (autonomy_grant)"
            )
        return ToolResult("\n".join(lines) or "Rien ne peut se faire sans lui demander.")

    return [
        Tool(
            name=GRANT_ACTION,
            description="When the owner asks you to stop asking before a kind of action (« ne "
            "me demande plus », « autorise définitivement »): ask them (they approve once) to "
            "let you do it alone from now on. `action` is the tool's name (shell, calendar_add…); "
            "`autonomy` says which ones can be.",
            input_model=GrantInput,
            handler=grant,
            effect=Effect.EXTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
        Tool(
            name="autonomy",
            description="Which actions you may do without asking, and which you ask for.",
            input_model=StatusInput,
            handler=status,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        ),
        Tool(
            name="autonomy_revoke",
            description="Ask again before an action you did alone: at once when the owner says "
            "so, or as soon as they undo or regret something you did without asking.",
            input_model=RevokeInput,
            handler=revoke,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
            accepts_untrusted_context=True,
        ),
    ]
