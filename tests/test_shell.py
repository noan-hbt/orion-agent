"""The shell: each command approved, no key in its environment, stopped in time (D96)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orion.configure.paths import tier
from orion.core.events import Trust
from orion.shell.module import shell_tool
from orion.tools.base import Effect, ToolContext


async def test_a_command_runs_bare_in_the_workspace_and_stops_in_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")
    tool = shell_tool(tmp_path / "ws", max_seconds=2)
    ctx = ToolContext("owner", "c")

    def call(command: str, timeout: int = 5) -> object:
        return tool.parse(json.dumps({"command": command, "why": "test", "timeout": timeout}))

    assert tool.effect is Effect.EXTERNAL  # he approves each command
    done = await tool.handler(call("echo hi > a.txt; cat a.txt; env"), ctx)
    assert "[exit 0]" in done.content and "hi" in done.content
    assert "sk-secret" not in done.content and done.trust is Trust.THIRD_PARTY
    assert (tmp_path / "ws" / "a.txt").exists()
    failed = await tool.handler(call("exit 3"), ctx)
    assert failed.is_error and "[exit 3]" in failed.content
    slow = await tool.handler(call("sleep 30", timeout=30), ctx)  # capped at max_seconds
    assert "stopped after 2 s" in slow.content
    assert tier("modules.shell.enabled", True) == "owner"  # turned on with his yes only
