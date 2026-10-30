"""The calendar as a module: `[modules.calendar]` in orion.toml."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from orion.calendar.service import Calendar, CalendarSettings, Fetch
from orion.calendar.tools import calendar_tools
from orion.modules.base import Daemon, Module, Plate

AHEAD_DAYS = 14
"""How far ahead the night reads his agenda, to prepare what is coming (D201)."""

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.tools.base import Tool


class CalendarModule(Module):
    name = "calendar"
    offer = (
        "Lire ses agendas (Google, iCloud, Outlook) et le prévenir avant chaque rendez-vous : "
        "l'adresse iCal secrète de chaque agenda dans `[modules.calendar]`."
    )

    def offers(self) -> list[str]:
        if not self.enabled:
            return super().offers()
        if any(account.write is not None for account in self.settings.accounts):
            return []
        return [
            "Ajouter, déplacer ou annuler des événements de ses agendas (chacun avec son "
            "accord) : `orion google auth` pour Google, ou un compte CalDAV dans "
            "`[modules.calendar.accounts.write]`."
        ]

    instructions = "calendar.md"
    settings_model = CalendarSettings

    def __init__(
        self,
        config: OrionConfig,
        fetch: Fetch | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(config)
        self._fetch = fetch
        self._clock = clock
        self.calendar: Calendar | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        fetch = self._fetch or (plate.guardian.calendar if plate.guardian else None)
        self.calendar = Calendar(
            plate.journal,
            self.settings,
            self.config.agent.timezone,
            fetch,
            self._clock,
            plate.guardian.calendar_add if plate.guardian else None,
            plate.guardian.calendar_change if plate.guardian else None,
        )
        await self.calendar.start()
        return calendar_tools(self.calendar, plate.memory)

    async def serve(self, daemon: Daemon) -> None:
        calendar = self.calendar
        if calendar is None:  # pragma: no cover - assemble always runs first
            return
        daemon.add_service("calendar.sync", calendar.run)
        daemon.alerts.append(calendar)
        daemon.digests.append(calendar)
        daemon.presence.append(calendar)
        daemon.orion.situation.append(calendar)

    async def review(self, now: datetime) -> list[str]:
        if self.calendar is None:
            return []
        return await self.calendar.ahead(now, AHEAD_DAYS)

    async def close(self) -> None:
        if self.calendar is not None:
            await self.calendar.aclose()

    def doctor(self) -> list[str]:
        settings: CalendarSettings = self.settings
        if not settings.accounts:
            return ["no account: add [[modules.calendar.accounts]]"]
        if self.config.guardian.enabled:
            return [f"{len(settings.accounts)} account(s), read by the guardian"]
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
