"""The shell: `[modules.shell]` in orion.toml (off by default, D96).

Orion runs a command on its server: convert a file, check the disk, run a script, use a tool
nothing else covers. What protects:

- every command is put to the owner, exact, before it runs (an external effect), and the shell
  is never something Orion does alone;
- turning it on is the owner's setting (`modules.shell`, D92): his yes, never alone;
- the command runs in Orion's workspace (`<data_dir>/workspace`), as Orion's user, with a
  bare environment: no key of Orion's is in its environment. On a server installed by
  install.sh, Orion's user is root through `sudo -n` (D164): a command can read and change
  anything, the guardian's secrets included. The owner's yes to the exact command is what
  stands between it and the whole machine;
- it stops after `timeout` seconds, the whole process group with it; output is bounded;
- what it prints may come from anywhere (a download, a file): outside content.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import Trust
from orion.modules.base import Module, ModuleSettings, Plate
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    pass

MAX_OUTPUT = 12_000


class ShellSettings(ModuleSettings):
    enabled: bool = False
    max_seconds: int = Field(default=120, ge=1, le=1800, description="Longest a command runs")


class ShellInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1, max_length=4000, description="A POSIX sh command line")
    why: str = Field(min_length=3, max_length=200, description="What it is for, for him")
    timeout: int = Field(default=60, ge=1, le=1800, description="Seconds before it is stopped")


def bare_env(workspace: Path) -> dict[str, str]:
    """What a command sees: no secret, no key of Orion's."""
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(workspace),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TERM": "dumb",
    }


def _bounded(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    return f"{text[:half]}\n[… {len(text) - MAX_OUTPUT} characters cut …]\n{text[-half:]}"


async def run(command: str, workspace: Path, seconds: float) -> tuple[int | None, str]:
    """Exit code (None: stopped after `seconds`) and combined output."""
    await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-c",
        command,
        cwd=workspace,
        env=bare_env(workspace),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), seconds)
    except TimeoutError:
        os.killpg(process.pid, signal.SIGKILL)
        out, _ = await process.communicate()
        return None, out.decode(errors="replace")
    return process.returncode, out.decode(errors="replace")


def shell_tool(workspace: Path, max_seconds: int) -> Tool:
    async def handler(args: Any, _ctx: ToolContext) -> ToolResult:
        timeout = min(args.timeout, max_seconds)
        code, out = await run(args.command, workspace, timeout)
        status = f"stopped after {timeout} s" if code is None else f"exit {code}"
        return ToolResult(
            f"[{status}]\n{_bounded(out) or '(no output)'}",
            is_error=code != 0,
            trust=Trust.THIRD_PARTY,
        )

    return Tool(
        name="shell",
        doing="je lance une commande",
        description="Run a command on your server, in your workspace (files you made or "
        "downloaded are there), when no other tool does it: convert a file, check something, "
        "run a script, read logs (`sudo -n journalctl -u orion-core`). You are root through "
        "`sudo -n` when the server allows it. He approves the exact command first. The "
        "environment holds no key.",
        input_model=ShellInput,
        handler=handler,
        effect=Effect.EXTERNAL,
        reversibility=Reversibility.IRREVERSIBLE,
    )


class ShellModule(Module):
    name = "shell"
    settings_model = ShellSettings

    async def assemble(self, plate: Plate) -> list[Tool]:
        settings: ShellSettings = self.settings
        return [shell_tool(self.config.data_dir / "workspace", settings.max_seconds)]
