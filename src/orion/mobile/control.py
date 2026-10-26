"""What the app shows and lets him change besides the conversation (D125): the controls.

The same things the console shows (D101), read from the same places, as JSON: what Orion knows
(memory, commitments, its manual), what it does alone, what it did, what it will do, what it
costs; and the few things he changes from here: a pause, quiet hours, the daily attention
budget. Every change is his own correction, journaled with trust OWNER, as the console does.
Giving Orion more room (autonomy) is not here: that is asked and signed in the conversation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from orion.awaiting.module import Awaiting
from orion.console.app import ACTIVITY, activity_detail
from orion.console.dashboard import moment
from orion.core.events import Provenance, Trust
from orion.core.journal import EventStore
from orion.guardian.autonomy import Autonomy, label
from orion.initiative.engine import InitiativeSettings, effective_settings
from orion.memory.model import CommitmentStatus, SettingKey
from orion.memory.service import MemoryService, Origin
from orion.modules.base import Planned
from orion.providers.spending import Spending
from orion.tasks.service import Tasks

OWNER = Origin(actor="owner:app", source="app", trust=Trust.OWNER)
FROM_APP = Provenance(source="app", actor="owner:main")
MAX_PAUSE = timedelta(days=30)
ACTIVITY_WINDOW = 3000


class ControlError(ValueError):
    """A change that cannot be made (unknown id, a value out of range)."""


@dataclass
class Control:
    journal: EventStore
    memory: MemoryService
    initiative: InitiativeSettings
    """The config file's settings: what the owner's changes override."""
    autonomy: Autonomy | None = None
    spending: Spending | None = None
    planned: Callable[[datetime, datetime], Awaitable[list[Planned]]] | None = None
    clock: Callable[[], datetime] | None = None
    tasks: Callable[[], Tasks | None] = lambda: None
    """Background tasks, once their module is assembled (after this one)."""
    awaiting: Callable[[], Awaiting | None] = lambda: None
    """The answers Orion waits for, once their module is assembled."""

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.initiative.timezone)

    def now(self) -> datetime:
        return self.clock() if self.clock else datetime.now(self.zone)

    async def settings(self) -> InitiativeSettings:
        return effective_settings(self.initiative, await self.memory.store.settings())

    # -- the overview -------------------------------------------------------------------------

    async def overview(self) -> dict[str, Any]:
        settings = await self.settings()
        now = self.now()
        store = self.memory.store
        if self.autonomy is not None:
            await self.autonomy.start()
        facts, commitments, rules = (
            await store.facts(),
            await store.commitments(),
            await store.rules(),
        )
        cost: dict[str, Any] | None = None
        if self.spending is not None:
            await self.spending.sync()
            day = now.date()
            foreground, background = self.spending.tally.days.get(day.isoformat(), [0.0, 0.0])
            cost = {
                "today": round(foreground + background, 4),
                "alone_today": round(background, 4),
                "month": round(self.spending.tally.month(day), 4),
                "monthly_cap": self.spending.budget.monthly_usd,
                "daily_cap": self.spending.budget.daily_usd,
                "over": self.spending.over(day),
            }
        paused = settings.is_paused(now)
        return {
            "paused_until": settings.paused_until.isoformat()
            if paused and settings.paused_until
            else None,
            "quiet_start": settings.quiet_start.strftime("%H:%M"),
            "quiet_end": settings.quiet_end.strftime("%H:%M"),
            "quiet_now": settings.is_quiet(now.time()),
            "daily_budget": settings.daily_budget,
            "digests": [d.strftime("%H:%M") for d in settings.digests],
            "counts": {"facts": len(facts), "commitments": len(commitments), "rules": len(rules)},
            "alone": [label(a) for a in sorted(self.autonomy.state.granted)]
            if self.autonomy
            else [],
            "cost": cost,
            "next": (await self.upcoming(days=2))[:3],
        }

    # -- the few things he changes here -------------------------------------------------------

    async def pause(self, minutes: int | None) -> str | None:
        """Pause Orion's initiative for `minutes` (None: resume). Until when, or None."""
        if minutes is None or minutes <= 0:
            await self.memory.change_setting(
                SettingKey.PAUSED_UNTIL, "", "reprise depuis l'app", OWNER
            )
            return None
        until = self.now() + min(timedelta(minutes=minutes), MAX_PAUSE)
        await self.memory.change_setting(
            SettingKey.PAUSED_UNTIL, until.isoformat(), "pause depuis l'app", OWNER
        )
        return until.isoformat()

    async def change(self, changes: dict[str, Any]) -> None:
        """Quiet hours and the daily attention budget, as he set them in the app."""
        for key, setting in (
            ("quiet_start", SettingKey.QUIET_START),
            ("quiet_end", SettingKey.QUIET_END),
        ):
            if key in changes:
                try:
                    value = time.fromisoformat(str(changes[key])).strftime("%H:%M")
                except ValueError as exc:
                    raise ControlError(f"{key}: not a time") from exc
                await self.memory.change_setting(setting, value, "réglé depuis l'app", OWNER)
        if "daily_budget" in changes:
            budget = int(changes["daily_budget"])
            if not 0 <= budget <= 50:
                raise ControlError("daily_budget: between 0 and 50")
            await self.memory.change_setting(
                SettingKey.DAILY_BUDGET, budget, "réglé depuis l'app", OWNER
            )

    # -- what it knows ------------------------------------------------------------------------

    async def facts(self, query: str = "", past: bool = False) -> list[dict[str, Any]]:
        """Newest first; matching `query` (best first) when he searches."""
        store = self.memory.store
        if query.strip():
            found = await store.search(query, limit=60, include_past=past)
        else:
            found = list(reversed(await store.facts(include_past=past)))
        return [
            {
                "id": f.id,
                "statement": f.statement,
                "category": f.category,
                "about": f.about,
                "source": f.source,
                "from_owner": f.trust >= Trust.OWNER,
                "trusted": f.trust >= Trust.SYSTEM,
                "current": f.current,
                "why_not": f.invalid_reason,
            }
            for f in found
        ]

    async def forget(self, fact_id: str) -> None:
        fact = await self.memory.store.fact(fact_id)
        if fact is None or not fact.current:
            raise ControlError("no such memory")
        await self.memory.invalidate(fact_id, "retiré par toi depuis l'app", OWNER)

    async def commitments(self) -> list[dict[str, Any]]:
        return [
            {"id": c.id, "what": c.what, "debtor": c.debtor, "creditor": c.creditor, "due": c.due}
            for c in await self.memory.store.commitments()
        ]

    async def close(self, commitment_id: str, done: bool) -> None:
        found = await self.memory.store.commitment(commitment_id)
        if found is None or found.status is not CommitmentStatus.OPEN:
            raise ControlError("no such open commitment")
        status = CommitmentStatus.DONE if done else CommitmentStatus.CANCELLED
        await self.memory.close_commitment(commitment_id, status, OWNER, evidence="depuis l'app")

    async def rules(self) -> list[dict[str, Any]]:
        return [
            {
                "id": r.id,
                "rule": r.rule,
                "basis": r.basis.value,
                "from_owner": r.trust >= Trust.OWNER,
            }
            for r in await self.memory.store.rules()
        ]

    async def retire(self, rule_id: str) -> None:
        found = await self.memory.store.rule(rule_id)
        if found is None or found.retired_seq is not None:
            raise ControlError("no such rule")
        await self.memory.retire_rule(rule_id, "retirée par toi depuis l'app", OWNER)

    # -- what it does alone, did, will do -----------------------------------------------------

    async def autonomy_state(self) -> list[dict[str, Any]]:
        if self.autonomy is None:
            return []
        await self.autonomy.start()
        state = self.autonomy.state
        return [
            {
                "action": action,
                "label": label(action),
                "alone": action in state.granted,
                "streak": state.streak.get(action, 0),
            }
            for action in self.autonomy.autonomable
        ]

    async def take_back(self, action: str) -> None:
        if self.autonomy is None or action not in self.autonomy.autonomable:
            raise ControlError("no such action")
        await self.autonomy.revoke(action, "depuis l'app", None)

    async def activity(self, limit: int = 120) -> list[dict[str, Any]]:
        """What it did and chose not to do, newest first."""
        last = await self.journal.last_seq()
        events = await self.journal.read(max(0, last - ACTIVITY_WINDOW), ACTIVITY_WINDOW)
        asked = {
            str(e.payload["approval_id"]): str(e.payload.get("tool"))
            for e in events
            if e.kind == "approval.requested"
        }
        shown = [e for e in events if e.kind in ACTIVITY and not e.redacted][-limit:]
        return [
            {
                "kind": e.kind,
                "what": ACTIVITY[e.kind],
                "detail": activity_detail(e, asked),
                "at": moment(e, self.zone).isoformat(),
                "silence": e.kind == "initiative.suppressed",
            }
            for e in reversed(shown)
        ]

    async def upcoming(self, days: int = 14) -> list[dict[str, Any]]:
        if self.planned is None:
            return []
        now = self.now()
        return [
            {"at": p.at.astimezone(self.zone).isoformat(), "what": p.what, "undoable": bool(p.undo)}
            for p in await self.planned(now, now + timedelta(days=days))
        ]

    # -- for him: what waits on him, and what Orion has going -----------------------------------

    async def for_you(self) -> dict[str, Any]:
        """The questions Orion waits on him for, the answers it waits for from others, and
        its background tasks (approvals come from the channel itself)."""
        questions: list[dict[str, Any]] = []
        others: list[dict[str, Any]] = []
        awaiting = self.awaiting()
        if awaiting is not None:
            await awaiting.sync()
            for wait in sorted(awaiting.state.pending(), key=lambda w: w.since, reverse=True):
                shown = {
                    "id": wait.id,
                    "about": wait.about,
                    "who": awaiting.describe(wait),
                    "since": wait.since.isoformat(),
                    "back_at": wait.at.isoformat(),
                    "round": wait.round,
                    "auto": wait.auto,
                }
                (questions if wait.who == "owner" else others).append(shown)
        running: list[dict[str, Any]] = []
        tasks = self.tasks()
        if tasks is not None:
            for task in await tasks.running():
                running.append(
                    {
                        "id": task.id,
                        "goal": task.goal,
                        "passes": task.passes,
                        "spent": round(task.spent, 4),
                        "budget": task.budget_usd,
                        "minutes": task.minutes,
                        "started_at": task.started_at.isoformat(),
                        "waiting": task.waiting,
                        "last": task.reports[-1][2][:280] if task.reports else "",
                    }
                )
        return {"questions": questions, "waiting_on": others, "tasks": running}

    async def stop_task(self, task_id: str) -> None:
        tasks = self.tasks()
        task = await tasks.get(task_id) if tasks is not None else None
        if tasks is None or task is None or task.finished is not None:
            raise ControlError("no such running task")
        await tasks.finish(
            task_id, "stopped", "arrêtée par toi depuis l'app", FROM_APP, Trust.OWNER
        )

    async def drop_wait(self, wait_id: str) -> None:
        awaiting = self.awaiting()
        if awaiting is None or not await awaiting.cancel(wait_id, FROM_APP, None):
            raise ControlError("no such wait")
