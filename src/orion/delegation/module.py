"""Delegation to the official `claude` CLI as a module."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from orion.delegation.claude_code import ClaudeCodeAgent
from orion.delegation.jobs import JobService, delegation_tools
from orion.modules.base import Daemon, Module, Plate

if TYPE_CHECKING:
    from orion.tools.base import Tool


class DelegationModule(Module):
    name = "delegation"
    offer = (
        "Confier de longs travaux (code, scripts, documents) à Claude Code sur le serveur : "
        "`[delegation] enabled = true`, puis `claude auth login` sur le serveur."
    )
    instructions = "delegation.md"

    jobs: JobService | None = None

    @property
    def enabled(self) -> bool:
        return self.config.delegation.enabled

    async def assemble(self, plate: Plate) -> list[Tool]:
        d = self.config.delegation
        agent = ClaudeCodeAgent(
            d.binary, d.model, d.timeout_seconds, d.max_budget_usd, d.allow_bash
        )
        self.jobs = JobService(plate.journal, agent, self.config.workspaces_path)
        await self.jobs.recover()
        return delegation_tools(self.jobs)

    async def serve(self, daemon: Daemon) -> None:
        if self.jobs is not None:
            daemon.reports.append(self.jobs)

    def doctor(self) -> list[str]:
        binary = self.config.delegation.binary
        found = shutil.which(binary)
        return [f"{binary} found at {found}" if found else f"{binary} not found on PATH"]
