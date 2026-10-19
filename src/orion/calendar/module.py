"""The calendar as a module: `[modules.calendar]` in orion.toml."""

from __future__ import annotations

from typing import TYPE_CHECKING

from orion.calendar.service import Calendar, CalendarSettings, Fetch
from orion.calendar.tools import calendar_tools
from orion.modules.base import Daemon, Module, Plate

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.tools.base import Tool


class CalendarModule(Module):
    name = "calendar"
    instructions = "calendar.md"
    settings_model = CalendarSettings

    def __init__(self, config: OrionConfig, fetch: Fetch | None = None) -> None:
        super().__init__(config)
        self._fetch = fetch
        self.calendar: Calendar | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        self.calendar = Calendar(
            plate.journal, self.settings, self.config.agent.timezone, self._fetch
        )
        await self.calendar.start()
        return calendar_tools(self.calendar)

    async def serve(self, daemon: Daemon) -> None:
        calendar = self.calendar
        if calendar is None:  # pragma: no cover - assemble always runs first
            return
        daemon.add_service("calendar.sync", calendar.run)
        daemon.alerts.append(calendar)
        daemon.digests.append(calendar)

    async def close(self) -> None:
        if self.calendar is not None:
            await self.calendar.aclose()

    def doctor(self) -> list[str]:
        settings: CalendarSettings = self.settings
        if not settings.accounts:
            return ["no account: add [[modules.calendar.accounts]]"]
        lines: list[str] = []
        for account in settings.accounts:
            try:
                account.url.resolve()
                lines.append(f"{account.name} ok")
            except LookupError as exc:
                lines.append(f"{account.name}: {exc}")
        if not settings.owner_emails:
            lines.append("owner_emails empty: every invitation stays hidden")
        return lines
