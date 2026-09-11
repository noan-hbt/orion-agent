from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tool_packages.terminal import tool as terminal_tool


def _context(root: Path, **terminal_settings):
    return SimpleNamespace(root_dir=root, config={"terminal": terminal_settings})


def _python_env_command(name: str) -> str:
    import json
    import sys

    code = f"import os; print(os.environ.get({name!r}, 'MISSING'))"
    return f'"{sys.executable}" -c {json.dumps(code)}'


def test_terminal_does_not_inherit_process_secrets_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "super-secret")
    monkeypatch.setenv("TAVILY_API_KEY", "also-secret")

    first = terminal_tool.run_terminal(
        _python_env_command("OPENROUTER_API_KEY"),
        _context=_context(tmp_path),
    )
    second = terminal_tool.run_terminal(
        _python_env_command("TAVILY_API_KEY"),
        _context=_context(tmp_path),
    )

    assert first["exit_code"] == 0
    assert second["exit_code"] == 0
    assert first["stdout"].strip() == "MISSING"
    assert second["stdout"].strip() == "MISSING"


def test_terminal_inherits_only_explicitly_allowlisted_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("ORION_TERMINAL_TEST_VALUE", "visible")

    result = terminal_tool.run_terminal(
        _python_env_command("ORION_TERMINAL_TEST_VALUE"),
        _context=_context(tmp_path, env_allowlist=["ORION_TERMINAL_TEST_VALUE"]),
    )

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "visible"


def test_terminal_simple_command_compatibility(tmp_path):
    result = terminal_tool.run_terminal(
        "echo terminal-ok",
        _context=_context(tmp_path),
    )

    assert result["timed_out"] is False
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "terminal-ok"


def test_terminal_output_bounds_are_preserved(tmp_path):
    import sys

    result = terminal_tool.run_terminal(
        f'"{sys.executable}" -c "print(\'x\' * 500)"',
        _context=_context(tmp_path, max_output_chars=100),
    )

    assert result["exit_code"] == 0
    assert len(result["stdout"]) == 100
    assert result["truncated"] is True


def test_terminal_cwd_still_cannot_escape_root(tmp_path):
    outside = tmp_path.parent

    with pytest.raises(ValueError, match="rester sous"):
        terminal_tool.run_terminal(
            "echo blocked",
            cwd=str(outside),
            _context=_context(tmp_path),
        )


def test_timeout_uses_process_group_and_tree_cleanup(monkeypatch, tmp_path):
    class FakeProcess:
        pid = 4321
        returncode = None

        def __init__(self):
            self.communicate_calls = 0

        def communicate(self, timeout=None):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise subprocess.TimeoutExpired("cmd", timeout)
            self.returncode = -9
            return ("partial output", "")

        def poll(self):
            return None

        def kill(self):
            self.returncode = -9

    fake = FakeProcess()
    popen_kwargs = {}
    cleanup_calls = []

    def fake_popen(*args, **kwargs):
        popen_kwargs.update(kwargs)
        return fake

    monkeypatch.setattr(terminal_tool.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        terminal_tool,
        "_terminate_process_tree",
        lambda process: cleanup_calls.append(process.pid),
    )

    result = terminal_tool.run_terminal(
        "slow command",
        timeout=1,
        _context=_context(tmp_path),
    )

    assert result["timed_out"] is True
    assert result["stdout"] == "partial output"
    assert cleanup_calls == [4321]
    if os.name == "nt":
        assert popen_kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert popen_kwargs["start_new_session"] is True


def test_windows_cleanup_requests_recursive_forced_taskkill(monkeypatch):
    calls = []

    class FakeProcess:
        pid = 2468

        def kill(self):
            raise AssertionError("fallback kill should not be needed")

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(terminal_tool.subprocess, "run", fake_run)

    terminal_tool._terminate_windows_tree(FakeProcess(), grace_seconds=0.5)

    args, kwargs = calls[0]
    assert args == ["taskkill", "/PID", "2468", "/T", "/F"]
    assert kwargs["timeout"] == 0.5
    assert kwargs["check"] is False
    assert "OPENROUTER_API_KEY" not in kwargs["env"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
def test_posix_cleanup_targets_process_group(monkeypatch):
    calls = []

    class FakeProcess:
        pid = 9876

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            calls.append(("terminate", self.pid))

        def kill(self):
            calls.append(("kill", self.pid))

    monkeypatch.setattr(terminal_tool.os, "killpg", lambda pid, sig: calls.append((pid, sig)))

    terminal_tool._terminate_process_tree(FakeProcess())

    assert calls[0] == (9876, terminal_tool.signal.SIGTERM)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
def test_posix_cleanup_still_targets_group_after_shell_leader_exits(monkeypatch):
    calls = []

    class ExitedShell:
        pid = 1357

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            raise AssertionError("leader is already exited")

    def fake_killpg(pid, sig):
        calls.append((pid, sig))
        if sig == 0:
            # Simulate a descendant that is still alive in the process group.
            return None

    monkeypatch.setattr(terminal_tool.os, "killpg", fake_killpg)

    terminal_tool._terminate_process_tree(ExitedShell())

    assert (1357, terminal_tool.signal.SIGTERM) in calls
    assert (1357, terminal_tool.signal.SIGKILL) in calls
