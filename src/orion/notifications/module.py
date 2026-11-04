"""Notifications: `[modules.notifications]` (on by default, D225).

His choices of what rings, how and where (`orion.notifications.policy`), applied by the inbox
to everything Orion sends; set in the app (Réglages > Notifications) or by asking Orion, who
uses `notifications`. And the new kinds, written here without the model when he turns them on:
a background task done, a mission's pass, Orion's night over, Orion stuck, his security.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import structlog
from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import Event, Provenance, Trust
from orion.core.ids import new_id
from orion.hooks.catalog import BY_NAME as TRIGGERS
from orion.initiative.engine import InitiativeSettings, effective_settings
from orion.memory.service import MemoryService
from orion.modules.base import Daemon, Module, ModuleSettings, Plate
from orion.notifications.policy import (
    BY_NAME,
    MUTE,
    NORMAL,
    OFF,
    URGENT,
    NotificationError,
    Notifications,
)
from orion.tools.actions import NO_ARGS, Action, action_tool
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.channels.inbox import OwnerInbox
    from orion.config import OrionConfig

log = structlog.get_logger(__name__)

WATCH_EVERY = 3.0
BATCH = 500
GAP = timedelta(seconds=60)
"""Between two notifications of one new kind: a failing channel must not feed « je bute »
with its own failures."""

DIRECT: dict[str, tuple[tuple[str, ...], frozenset[str], Callable[[str], str]]] = {
    "mission": (("mission",), frozenset(), lambda g: f"Mission : {g}"),
    "nuit": (
        ("night",),
        frozenset({"thinking.night.done"}),
        lambda _g: "Ma nuit est finie : je te raconte quand tu veux.",
    ),
    "erreur": (("turn_failed", "send_failed"), frozenset(), lambda g: f"Je bute : {g}"),
    "securite": (("device", "autonomy", "config"), frozenset(), lambda g: f"Sécurité : {g}"),
}
"""Each new kind: the triggers (`orion.hooks.catalog`) that make one, the event kinds it keeps
(empty: all the triggers'), and its line."""


class NotificationSettings(ModuleSettings):
    enabled: bool = True


def direct_line(kind: str, event: Event, zone: ZoneInfo) -> str | None:
    """The line for this event, if it makes a notification of the new kind `kind`."""
    names, kinds, line = DIRECT[kind]
    if kinds and event.kind not in kinds:
        return None
    for name in names:
        found = TRIGGERS.get(name)
        if found is not None and found.fits(event):
            return line(found.gist(event, zone))
    return None


class _Set(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(description="Which kind (`list`): rappel, initiative, compte_rendu…")
    level: str = Field(default="", description="off, muette, normale or urgente")
    channel: str = Field(default="", description="auto, partout, or a channel (mobile…)")


def notification_tool(notifications: Notifications, channels: Callable[[], list[str]]) -> Tool:
    async def listing(_args: Any, _ctx: ToolContext) -> ToolResult:
        await notifications.sync()
        lines = notifications.lines()
        lines.append(
            "Niveaux : off (rien), muette (sans sonner), normale, urgente (même en heures "
            f"calmes). Canaux : auto (là où il t'a écrit), partout, {', '.join(channels())}."
        )
        return ToolResult("\n".join(lines))

    async def choose(args: _Set, ctx: ToolContext) -> ToolResult:
        if ctx.context_trust < Trust.SYSTEM and not ctx.unrestricted:
            return ToolResult(
                "Ses notifications ne se changent pas après un contenu venu d'ailleurs : "
                "propose-lui de le faire dans l'app (Réglages > Notifications).",
                is_error=True,
            )
        try:
            route = await notifications.choose(
                args.kind,
                args.level or None,
                args.channel or None,
                Provenance(source=f"thread:{ctx.thread_id}", actor="orion:core"),
                ctx.context_trust,
                channels(),
            )
        except NotificationError as exc:
            return ToolResult(str(exc), is_error=True)
        label = BY_NAME[args.kind].label
        return ToolResult(f"« {label} » : {route.level}, {route.channel}.")

    return action_tool(
        "notifications",
        "What rings for him, how, where. Kind by kind: his answers, his reminders, the "
        "points, what you say of your own, his hooks… and new kinds off until he wants them "
        "(a background task done, a mission's pass, your night over, you stuck, his "
        "security). « ne me réveille plus pour… », « préviens-moi quand une tâche finit ».",
        [
            Action("list", "Every kind and his choice.", NO_ARGS, listing, Effect.NONE, True),
            Action("set", "Change one kind's level or channel.", _Set, choose, Effect.INTERNAL),
        ],
        reversibility=Reversibility.REVERSIBLE,
    )


class NotificationsModule(Module):
    name = "notifications"
    settings_model = NotificationSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.notifications: Notifications | None = None
        self.inbox: OwnerInbox | None = None
        self._memory: MemoryService | None = None
        self._sent: dict[str, datetime] = {}

    def channels(self) -> list[str]:
        return self.inbox.outlet_names() if self.inbox is not None else []

    async def assemble(self, plate: Plate) -> list[Tool]:
        self.notifications = Notifications(plate.journal)
        self._memory = plate.memory
        await self.notifications.sync()
        return [notification_tool(self.notifications, self.channels)]

    async def serve(self, daemon: Daemon) -> None:
        notifications = self.notifications
        if notifications is None:  # pragma: no cover - assemble always runs first
            return
        self.inbox = daemon.inbox
        daemon.inbox.policy = notifications

        async def watch() -> None:
            cursor = await daemon.plate.journal.last_seq()
            while True:
                events = await daemon.plate.journal.read(cursor, BATCH)
                if events:
                    cursor = events[-1].seq
                    await notifications.sync()
                    for event in events:
                        await self._direct(daemon.inbox, event)
                else:
                    await asyncio.sleep(WATCH_EVERY)

        daemon.add_service("notifications.watch", watch)

    async def _quiet(self, now: datetime) -> bool:
        """Quiet hours or a pause: what is new rings only if he said urgent."""
        initiative = self.config.initiative
        base = InitiativeSettings(
            timezone=self.config.agent.timezone,
            quiet_start=initiative.quiet_start,
            quiet_end=initiative.quiet_end,
        )
        if self._memory is None:
            return base.is_quiet(now.time())
        settings = effective_settings(base, await self._memory.store.settings())
        return settings.is_quiet(now.time()) or settings.is_paused(now)

    async def _direct(self, inbox: OwnerInbox, event: Event) -> None:
        notifications = self.notifications
        if notifications is None or event.redacted:
            return
        zone = ZoneInfo(self.config.agent.timezone)
        for kind in DIRECT:
            level = notifications.route(kind).level
            if level == OFF:
                continue
            line = direct_line(kind, event, zone)
            if line is None:
                continue
            now = datetime.now(zone)
            last = self._sent.get(kind)
            if last is not None and now - last < GAP:
                log.info("notifications.direct_held", kind=kind, seq=event.seq)
                continue
            self._sent[kind] = now
            if level == NORMAL and await self._quiet(now):
                level = MUTE
            try:
                await inbox.notify_as(line, new_id(), kind, level)
            except Exception:
                log.exception("notifications.direct_failed", kind=kind, seq=event.seq)
            else:
                log.info("notifications.direct", kind=kind, level=level, urgent=level == URGENT)
