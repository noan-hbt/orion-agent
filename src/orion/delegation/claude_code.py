"""Delegated agent: the official `claude` CLI, unmodified, given one whole task (D5, voie C).

Each job runs in its own empty workspace directory, in print mode, with a fixed tool list (no
shell unless explicitly allowed), no session persistence, no MCP servers, a spend cap, and a
stripped environment: Orion's own secrets are never passed down. Authentication is the CLI's
own (`claude auth login` on the server), so the owner's subscription pays for it normally.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from orion.providers.claude_code import child_env, stop

log = structlog.get_logger(__name__)

FILE_TOOLS = ("Read", "Write", "Edit", "Glob", "Grep")
BRIEF = (
    "You work for Orion, a personal assistant, on a task its owner asked for. Work only inside "
    "the current directory. Finish with a short summary, in the language of the task, of what "
    "you did and which files hold the result."
)


@dataclass(frozen=True)
class AgentOutcome:
    ok: bool
    result: str
    cost_usd: float = 0.0
    files: list[str] = field(default_factory=list[str])
    error: str | None = None


class ClaudeCodeAgent:
    name = "claude-code"

    def __init__(
        self,
        binary: str = "claude",
        model: str | None = None,
        timeout_seconds: float = 1800.0,
        max_budget_usd: float = 2.0,
        allow_bash: bool = False,
    ) -> None:
        self._binary = binary
        self._model = model
        self._timeout = timeout_seconds
        self._budget = max_budget_usd
        self.allow_bash = allow_bash

    def command(self, task: str) -> list[str]:
        tools = [*FILE_TOOLS, *(["Bash"] if self.allow_bash else [])]
        command = [
            self._binary,
            "-p",
            task,
            "--output-format",
            "json",
            "--permission-mode",
            "acceptEdits",
            "--tools",
            ",".join(tools),
            "--allowedTools",
            ",".join(tools),
            "--no-session-persistence",
            "--strict-mcp-config",
            "--max-budget-usd",
            f"{self._budget:.2f}",
            "--append-system-prompt",
            BRIEF,
        ]
        if self._model:
            command += ["--model", self._model]
        return command

    async def run(
        self, task: str, workdir: Path, options: dict[str, Any] | None = None
    ) -> AgentOutcome:
        await asyncio.to_thread(workdir.mkdir, parents=True, exist_ok=True)
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command(task),
                cwd=workdir,
                env=child_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return AgentOutcome(False, "", error=f"cannot start {self._binary}: {exc}")
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self._timeout)
        except TimeoutError:
            return AgentOutcome(False, "", error=f"timed out after {self._timeout:.0f}s")
        finally:
            await stop(process)  # timed out, or the job was cancelled: never left running
        files = await asyncio.to_thread(_list_files, workdir)
        return self._parse(process.returncode, stdout, stderr, files)

    @staticmethod
    def _parse(code: int | None, stdout: bytes, stderr: bytes, files: list[str]) -> AgentOutcome:
        try:
            data = _Result.model_validate_json(stdout)
        except ValidationError:
            detail = (stderr or stdout).decode(errors="replace").strip()[-500:]
            return AgentOutcome(False, "", files=files, error=f"exit {code}: {detail}")
        failed = code != 0 or data.is_error
        error = (data.subtype or f"exit {code}") if failed else None
        return AgentOutcome(not failed, data.result or "", data.total_cost_usd or 0.0, files, error)


def _list_files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


class _Result(BaseModel):
    """The final JSON object of `claude -p --output-format json`."""

    model_config = ConfigDict(extra="ignore")

    type: str
    subtype: str | None = None
    is_error: bool = False
    result: str | None = None
    total_cost_usd: float | None = None
