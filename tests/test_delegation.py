from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler
from orion.core.events import Provenance
from orion.core.journal import SqliteJournal
from orion.delegation.claude_code import ClaudeCodeAgent, child_env
from orion.delegation.jobs import JOB_FINISHED, JobService, delegation_tools
from orion.guardian.policy import Guardian
from orion.initiative.engine import InitiativeEngine, InitiativeSettings
from orion.initiative.timers import TimerProjection, Timers
from orion.runtime import Orion
from orion.tools.base import Effect, ToolRegistry
from tests.fakes import ScriptedProvider, call_tools, reply

PARIS = ZoneInfo("Europe/Paris")

FAKE_CLAUDE = r"""
import json, os, sys
args = sys.argv[1:]
task = args[args.index("-p") + 1]
if "boom" in task:
    print("fatal: not logged in", file=sys.stderr)
    sys.exit(1)
with open("hello.py", "w") as f:
    f.write("print('hello')\n")
with open("env.json", "w") as f:
    json.dump(sorted(os.environ), f)
with open("args.json", "w") as f:
    json.dump(args, f)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "J'ai écrit hello.py.", "total_cost_usd": 0.12}))
"""


@pytest.fixture
def fake_claude(tmp_path: Path) -> str:
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    launcher = tmp_path / "claude"
    launcher.write_text(f'#!/bin/sh\nexec {sys.executable} {script} "$@"\n', encoding="utf-8")
    launcher.chmod(0o755)
    return str(launcher)


async def test_agent_runs_in_its_workspace_without_orion_secrets(
    fake_claude: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere")
    workdir = tmp_path / "ws" / "j1"
    outcome = await ClaudeCodeAgent(fake_claude).run("écris un hello world", workdir)
    assert outcome.ok and outcome.result == "J'ai écrit hello.py." and outcome.cost_usd == 0.12
    assert "hello.py" in outcome.files
    env = json.loads((workdir / "env.json").read_text())
    assert "OPENROUTER_API_KEY" not in env and "CLAUDE_CONFIG_DIR" in env
    args = json.loads((workdir / "args.json").read_text())
    assert args[args.index("--tools") + 1] == "Read,Write,Edit,Glob,Grep"
    assert "--no-session-persistence" in args and "--strict-mcp-config" in args


async def test_agent_failures_are_reported_not_raised(fake_claude: str, tmp_path: Path) -> None:
    failed = await ClaudeCodeAgent(fake_claude).run("boom", tmp_path / "w")
    assert not failed.ok and failed.error is not None and "not logged in" in failed.error
    missing = await ClaudeCodeAgent(str(tmp_path / "nope")).run("x" * 20, tmp_path / "w2")
    assert not missing.ok and "cannot start" in (missing.error or "")


def test_env_stripping() -> None:
    env = child_env({"PATH": "/bin", "INKBOX_API_KEY": "k", "ANTHROPIC_MODEL": "m"})
    assert env == {"PATH": "/bin", "ANTHROPIC_MODEL": "m"}


def test_a_shell_makes_delegation_an_external_effect(
    tmp_path: Path, journal: SqliteJournal
) -> None:
    safe = delegation_tools(JobService(journal, ClaudeCodeAgent(), tmp_path))[0]
    shell = delegation_tools(JobService(journal, ClaudeCodeAgent(allow_bash=True), tmp_path))[0]
    assert safe.effect is Effect.INTERNAL and shell.effect is Effect.EXTERNAL


class Inbox:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def available(self) -> bool:
        return True

    async def notify(self, text: str, correlation_id: str) -> None:
        self.messages.append(text)


async def test_delegated_job_round_trip(
    fake_claude: str, tmp_path: Path, journal: SqliteJournal
) -> None:
    jobs = JobService(journal, ClaudeCodeAgent(fake_claude), tmp_path / "ws")
    provider = ScriptedProvider(
        call_tools(("delegate", {"title": "Hello", "task": "écris un hello world en Python"})),
        reply("C'est lancé, je te dis quand c'est prêt."),
    )
    orion = Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry(delegation_tools(jobs)),
        guardian=Guardian(),
        compiler=ContextCompiler("C"),
        agent=AgentConfig(),
    )
    turn = await orion.handle_owner_message("owner", "fais-moi un hello world", "terminal")
    assert turn.text.startswith("C'est lancé")
    await jobs.wait()

    provider.extend(reply("Ton hello world est prêt : hello.py."))
    inbox = Inbox()
    engine = InitiativeEngine(
        orion,
        journal,
        Timers(journal, TimerProjection()),
        inbox,
        InitiativeSettings(digests=()),
        jobs=jobs,
        clock=lambda: datetime(2026, 10, 19, 23, 30, tzinfo=PARIS),  # quiet hours: still sent
    )
    keys = await engine.tick()
    assert len(keys) == 1 and keys[0].startswith("job:")
    assert inbox.messages == ["Ton hello world est prêt : hello.py."]
    notice = provider.requests[-1].messages[-1]["content"]
    assert "J'ai écrit hello.py." in notice and "hello.py" in notice
    assert await engine.tick() == []


async def test_jobs_cut_by_a_restart_are_marked_failed(
    tmp_path: Path, journal: SqliteJournal
) -> None:
    jobs = JobService(journal, ClaudeCodeAgent(), tmp_path)
    await jobs._append(  # a job.started whose process died with the server
        "job.started",
        {"job_id": "j_1", "title": "t", "task": "x", "workdir": str(tmp_path / "j_1")},
        Provenance(source="thread:owner", actor="orion:core"),
        None,
    )
    restarted = JobService(journal, ClaudeCodeAgent(), tmp_path)
    await restarted.recover()
    [finished] = [e for e in await journal.read() if e.kind == JOB_FINISHED]
    assert finished.payload["error"] == "interrupted by a restart"
