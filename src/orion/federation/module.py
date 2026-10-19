"""Federation as a module: `[federation]` in orion.toml. Needs the guardian process, which
holds the instance key, and the calendar, which says when the owner is free."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from orion.calendar.module import CalendarModule
from orion.federation.meetings import MeetingDesk
from orion.federation.slots import Hours
from orion.federation.tools import federation_tools
from orion.modules.base import Daemon, Module, ModuleError, Plate

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.tools.base import Tool


class FederationModule(Module):
    name = "federation"
    requires = ("calendar",)
    instructions = "federation.md"

    def __init__(self, config: OrionConfig, clock: Callable[[], datetime] | None = None) -> None:
        super().__init__(config)
        self._clock = clock
        self.desk: MeetingDesk | None = None

    @property
    def enabled(self) -> bool:
        return self.config.federation.enabled

    async def assemble(self, plate: Plate) -> list[Tool]:
        if plate.guardian is None:
            raise ModuleError(
                "federation needs the guardian process, which holds the instance key: "
                "set [guardian] enabled = true"
            )
        calendar = plate.module(CalendarModule)
        if calendar is None or calendar.calendar is None:  # pragma: no cover - `requires`
            raise ModuleError("federation needs the calendar module")
        f = self.config.federation
        self.desk = MeetingDesk(
            plate.journal,
            plate.guardian,
            calendar.calendar,
            plate.memory,
            plate.approver,
            Hours(f.meeting_start, f.meeting_end, f.weekends, self.config.agent.timezone),
            self._clock,
        )
        await self.desk.start()
        return federation_tools(self.desk)

    async def serve(self, daemon: Daemon) -> None:
        if self.desk is None:  # pragma: no cover - assemble always runs first
            return
        daemon.add_service("federation.inbox", self.desk.run)
        daemon.reports.append(self.desk)

    def doctor(self) -> list[str]:
        f = self.config.federation
        lines = [f"public at {f.public_url or '(public_url missing)'}"]
        if not self.config.guardian.enabled:
            lines.insert(0, "needs [guardian] enabled = true")
        return lines
