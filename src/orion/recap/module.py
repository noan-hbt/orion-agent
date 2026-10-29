"""The evening recap: `[modules.recap]` in orion.toml (on by default).

The day's last point (`initiative.digests`, 19:00 by default) opens with what Orion did for
the owner today: the messages it wrote to his contacts, the mails it sent, what it added to
his calendar, the reminders it set, the settings it changed. The point already carries the
rest (what his contacts said, his mail, tomorrow's calendar, what he still owes), so the
evening message reads as the day's recap.

Read from the journal (`tool.executed`, the calls that went through), nothing else is stored:
the projection rebuilds from the journal alone.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from orion.core.events import Event
from orion.core.hlc import Timestamp
from orion.core.projections import ProjectionRunner
from orion.initiative.engine import DigestLine
from orion.modules.base import Daemon, Module, ModuleSettings, Plate
from orion.tools.base import Tool

if TYPE_CHECKING:
    from orion.config import OrionConfig

EXECUTED = "tool.executed"
KEEP_DAYS = 3


def _quote(text: object, size: int = 80) -> str:
    said = " ".join(str(text or "").split())
    return f"« {said[:size]}{'…' if len(said) > size else ''} »"


def _names(value: object) -> str:
    items = cast(list[object], value) if isinstance(value, list) else [value]
    return ", ".join(str(v) for v in items if v)


RENDER: dict[str, Callable[[dict[str, Any]], str]] = {
    "contact_message": lambda a: f"écrit à {a.get('contact')} : {_quote(a.get('text'))}",
    "mail_send": lambda a: f"envoyé un mail à {_names(a.get('to'))} : {_quote(a.get('subject'))}",
    "calendar_add": lambda a: f"ajouté à ton agenda : {_quote(a.get('title'), 60)}",
    "calendar_change": lambda a: (
        f"{'annulé' if a.get('cancel') else 'modifié'} dans ton agenda : "
        f"{_quote(a.get('title'), 60)}"
    ),
    "remind_me": lambda a: f"posé un rappel : {_quote(a.get('what'), 60)}",
    "configure_change": lambda a: (
        "changé mes réglages : "
        + ", ".join(str(c.get("path")) for c in cast(list[dict[str, Any]], a.get("changes") or []))
    ),
    "call_someone": lambda a: f"appelé {a.get('name')}",
    "mail_tidy": lambda _a: "rangé ta boîte mail",
    "delegate": lambda a: f"confié une tâche à l'agent de code : {_quote(a.get('title'), 60)}",
}
"""What Orion did, in a few words, by tool."""


@dataclass
class RecapState:
    """Per local day, what Orion did (time, tool, arguments)."""

    zone: ZoneInfo
    name: str = "recap"
    days: dict[date, list[tuple[datetime, str, dict[str, Any]]]] = field(
        default_factory=dict[date, list[tuple[datetime, str, dict[str, Any]]]]
    )
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.days.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        payload = event.payload
        if event.kind != EXECUTED or payload.get("is_error") or event.redacted:
            return
        tool = str(payload.get("tool", ""))
        if tool not in RENDER:
            return
        try:
            loaded: object = json.loads(str(payload.get("arguments") or "{}"))
        except json.JSONDecodeError:
            return
        if not isinstance(loaded, dict):
            return
        at = datetime.fromtimestamp(Timestamp.decode(event.hlc).wall_ms / 1000, UTC)
        local = at.astimezone(self.zone)
        self.days.setdefault(local.date(), []).append((local, tool, cast(dict[str, Any], loaded)))
        for old in [d for d in self.days if (local.date() - d).days >= KEEP_DAYS]:
            del self.days[old]


class Recap:
    def __init__(self, runner: ProjectionRunner, state: RecapState) -> None:
        self._runner = runner
        self.state = state

    async def lines(self, day: date) -> list[str]:
        await self._runner.catch_up()
        done: list[str] = []
        for _at, tool, arguments in self.state.days.get(day, []):
            try:
                line = RENDER[tool](arguments)
            except (AttributeError, TypeError):
                continue  # arguments the model wrote oddly: the line is skipped, not invented
            if line not in done:
                done.append(line)
        return done

    async def digest(
        self, now: datetime, since: datetime, first_slot: bool, last_slot: bool
    ) -> list[DigestLine]:
        if not last_slot:
            return []
        done = await self.lines(now.date())
        if not done:
            return []  # nothing done: the point goes, or not, on the rest alone
        listed = "\n".join(f"- {line}" for line in done)
        return [
            DigestLine(
                "Récap du jour (ouvre le point du soir par là, en quelques mots) — ce que j'ai "
                f"fait pour lui aujourd'hui :\n{listed}",
                plain=f"Aujourd'hui, j'ai :\n{listed}",
            )
        ]


class RecapSettings(ModuleSettings):
    enabled: bool = True


class RecapModule(Module):
    name = "recap"
    settings_model = RecapSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.recap: Recap | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        state = RecapState(ZoneInfo(self.config.agent.timezone))
        self.recap = Recap(ProjectionRunner(plate.journal, [state]), state)
        return []

    async def serve(self, daemon: Daemon) -> None:
        if self.recap is not None:
            daemon.digests.append(self.recap)
