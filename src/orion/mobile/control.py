"""What the app shows and lets him change besides the conversation (D125): the controls.

The same things the console shows (D101), read from the same places, as JSON: what Orion knows
(memory, commitments, its manual), what it does alone, what it did, what it will do, what it
costs; and the few things he changes from here: a pause, quiet hours, the daily attention
budget. Every change is his own correction, journaled with trust OWNER, as the console does.
Giving Orion more room (autonomy) is not here: that is asked and signed in the conversation.

The server, read from here (`server`): the version and the last update (D94), every setting of
orion.toml that can be changed by message with whose yes it takes (D92), the modules and the
catalog (D105). Nothing is written from here: a change is asked in the conversation, where
Orion checks it whole and the owner signs it, the same way as by message.

His missions (D47): what each looks for, what it kept and what he turned down; from here he
asks for a pass now, changes its pace, turns an item down or ends it. His machines (D1): which
are online and what each allows; one is cut off from here, and one is added signed with Face ID
(`orion.mobile.upkeep`, D154), as is everything else that used to need the server's shell.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import structlog

from orion import __version__
from orion.awaiting.module import Awaiting
from orion.console.app import ACTIVITY, activity_detail
from orion.console.dashboard import moment
from orion.core.events import Provenance, Trust
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.guardian.autonomy import Autonomy, label
from orion.hands.desk import HandsDesk
from orion.initiative.engine import InitiativeSettings, effective_settings
from orion.memory.model import CommitmentStatus, SettingKey
from orion.memory.portrait import Portrait
from orion.memory.service import MemoryService, Origin
from orion.missions.service import Mission, Missions
from orion.modules.base import Planned
from orion.providers.spending import Spending
from orion.tasks.service import Tasks

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.mobile.upkeep import Upkeep

log = structlog.get_logger(__name__)

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
    config: OrionConfig | None = None
    """The file Orion started with: what the server part shows."""
    missions: Callable[[], Missions | None] = lambda: None
    """His missions, once their module is assembled."""
    hands: Callable[[], HandsDesk | None] = lambda: None
    """His machines (Orion's hands), once their module is assembled."""
    code: Path | None = None
    """Where Orion's code is checked out (the server updates it there, D94)."""
    upkeep: Upkeep | None = None
    """What used to need a shell on the server (D154), once the app is served."""
    _portrait: Portrait = field(default_factory=Portrait, init=False)
    _portrait_runner: ProjectionRunner | None = field(default=None, init=False)

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
        """Quiet hours, the times of his summaries and the daily attention budget, as he set
        them in the app."""
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
        if "digests" in changes:
            try:
                digests = sorted(
                    {time.fromisoformat(str(d)).strftime("%H:%M") for d in changes["digests"]}
                )
            except (TypeError, ValueError) as exc:
                raise ControlError("digests: a list of times") from exc
            if len(digests) > 6:
                raise ControlError("digests: six at most")
            await self.memory.change_setting(
                SettingKey.DIGESTS, digests, "réglé depuis l'app", OWNER
            )
        if "daily_budget" in changes:
            budget = int(changes["daily_budget"])
            if not 0 <= budget <= 50:
                raise ControlError("daily_budget: between 0 and 50")
            await self.memory.change_setting(
                SettingKey.DAILY_BUDGET, budget, "réglé depuis l'app", OWNER
            )

    # -- what it knows ------------------------------------------------------------------------

    async def portrait(self) -> dict[str, str] | None:
        """How Orion sees him, as it wrote it last night (D144)."""
        if self._portrait_runner is None:
            self._portrait_runner = ProjectionRunner(self.journal, [self._portrait])
        await self._portrait_runner.catch_up()
        portrait = self._portrait
        return {"text": portrait.text, "day": portrait.day} if portrait.text else None

    async def situations(self) -> list[dict[str, Any]]:
        """What is going on in his life, as Orion follows it (D149)."""
        await self.memory.sync()
        return [
            {
                "id": s.id,
                "title": s.title,
                "summary": s.summary,
                "until": s.until.isoformat() if s.until else None,
                "news": s.notes[-1] if s.notes else None,
            }
            for s in self.memory.situations.current()
        ]

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
            for action in self.autonomy.shown()
        ]

    async def take_back(self, action: str) -> None:
        if self.autonomy is None or action not in self.autonomy.shown():
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

    # -- the server: its settings and modules, read only --------------------------------------

    async def server(self) -> dict[str, Any]:
        if self.config is None:
            raise ControlError("the server part is not available here")
        shown = server_view(self.config)
        shown["release"] = await asyncio.to_thread(release, self.code) if self.code else None
        return shown

    # -- his missions -------------------------------------------------------------------------

    async def _missions(self) -> Missions:
        missions = self.missions()
        if missions is None:
            raise ControlError("missions are off")
        await missions.sync()
        return missions

    async def mission_list(self) -> list[dict[str, Any]]:
        missions = await self._missions()
        found = sorted(
            missions.state.missions.values(),
            key=lambda m: (m.closed is not None, -m.started_at.timestamp()),
        )
        return [mission_view(m, self.now()) for m in found[:30]]

    async def _open_mission(self, mission_id: str) -> tuple[Missions, Mission]:
        missions = await self._missions()
        mission = await missions.get(mission_id)
        if mission is None or mission.closed is not None:
            raise ControlError("no such mission running")
        return missions, mission

    async def mission_now(self, mission_id: str) -> int:
        missions, mission = await self._open_mission(mission_id)
        now = self.now()
        if not mission.rushes_left(now):
            raise ControlError("no more passes right away today")
        return await missions.rush(mission_id, now, FROM_APP, Trust.OWNER, None)

    async def mission_pace(self, mission_id: str, every_hours: int) -> None:
        missions, _ = await self._open_mission(mission_id)
        if not 1 <= every_hours <= 24 * 14:
            raise ControlError("every_hours: between 1 and 336")
        await missions.update(mission_id, FROM_APP, Trust.OWNER, None, every_hours=every_hours)

    async def mission_reject(self, mission_id: str, key: str, why: str) -> None:
        missions, mission = await self._open_mission(mission_id)
        if key not in mission.seen:
            raise ControlError("the mission did not see that")
        why = why.strip()[:300] or "écarté depuis l'app"
        await missions.reject(mission_id, key, why, FROM_APP, None)

    async def mission_close(self, mission_id: str) -> None:
        missions, _ = await self._open_mission(mission_id)
        await missions.close(mission_id, "arrêtée par toi depuis l'app", FROM_APP, None)

    # -- his machines ---------------------------------------------------------------------------

    async def machines(self) -> list[dict[str, Any]]:
        desk = self.hands()
        if desk is None:
            raise ControlError("hands are off")
        await desk.start()
        return [
            {
                "name": name,
                "online": name in desk.connected,
                "actions": list(desk.connected[name].actions) if name in desk.connected else [],
                "roots": list(desk.connected[name].roots) if name in desk.connected else [],
            }
            for name in sorted(desk.state.devices)
        ]

    async def machine_revoke(self, name: str) -> None:
        desk = self.hands()
        if desk is None:
            raise ControlError("hands are off")
        await desk.start()
        if name not in desk.state.devices:
            raise ControlError("no such machine")
        await desk.revoke(name)


def server_view(config: OrionConfig) -> dict[str, Any]:
    """What `orion.toml` holds that can be changed by message, with whose yes (D92), and the
    modules: on or off, installed from the catalog, or there to install (D105)."""
    from orion.configure.module import editable_sections
    from orion.configure.paths import ConfigureError, tier
    from orion.modules.installer import installed, modules_dir, read_catalog
    from orion.modules.registry import discover

    settings: list[dict[str, Any]] = []

    def walk(prefix: str, value: object) -> None:
        table = cast(dict[str, object], value) if isinstance(value, dict) else None
        if table:
            for key in sorted(table):
                walk(f"{prefix}.{key}", table[key])
            return
        try:
            settings.append(
                {"path": prefix, "value": value, "tier": tier(prefix, cast(object, value))}
            )
        except ConfigureError:
            return  # never changed by message: not shown

    for section, value in editable_sections(config).items():
        walk(section, value)
    added = installed(modules_dir(config))
    catalog = read_catalog()
    modules = [
        {"name": kind.name, "on": kind(config).enabled, "added": kind.name in added}
        for kind in discover(config)
    ]
    return {
        "version": __version__,
        "split": config.guardian.enabled,
        "settings": settings,
        "modules": sorted(modules, key=lambda m: (not m["on"], m["name"])),
        "catalog": [
            {
                "name": entry.name,
                "description": entry.description,
                "accesses": entry.accesses,
                "settings": entry.settings,
                "installed": entry.name in added,
            }
            for entry in catalog.values()
        ],
    }


def mission_view(mission: Mission, now: datetime) -> dict[str, Any]:
    nxt = (mission.last_run or mission.started_at) + timedelta(hours=mission.every_hours)
    return {
        "id": mission.id,
        "goal": mission.goal,
        "criteria": mission.criteria,
        "every_hours": mission.every_hours,
        "started_at": mission.started_at.isoformat(),
        "runs": mission.runs,
        "last_run": mission.last_run.isoformat() if mission.last_run else None,
        "next_run": None if mission.closed else max(nxt, now).isoformat(),
        "rushes_left": mission.rushes_left(now),
        "seen": [
            {"key": k, "label": label, "rejected": mission.rejected.get(k)}
            for k, label in list(mission.seen.items())[-60:]
        ],
        "rejected": [{"key": k, "why": why} for k, why in mission.rejected.items()],
        "notes": mission.notes[-6:],
        "reports": [
            {
                "run": run,
                "at": at.isoformat(),
                "said": said[:1200],
                "trusted": trust >= Trust.SYSTEM,
            }
            for run, at, said, trust in mission.reports[-5:]
        ],
        "closed": mission.closed,
    }


def release(code: Path) -> dict[str, Any] | None:
    """The commit Orion runs and, when the server updates itself (D94), a commit it set aside:
    `update.bad` beside the checkout. None when the code is not a git checkout."""
    if not (code / ".git").exists():
        return None
    try:
        done = subprocess.run(
            ["git", "-C", str(code), "log", "-1", "--format=%h%x00%cI%x00%s"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("control.release_unreadable", error=str(exc))
        return None
    commit, at, subject = [*done.stdout.strip().split("\0"), "", "", ""][:3]
    bad = code.parent / "update.bad"
    try:
        set_aside = bad.read_text("utf-8").strip()[:12] if bad.is_file() else None
    except OSError as exc:
        log.warning("control.update_bad_unreadable", error=str(exc))
        set_aside = None
    return {"commit": commit, "at": at, "subject": subject, "set_aside": set_aside}
