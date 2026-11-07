from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from orion.core.events import Provenance
from orion.core.journal import SqliteJournal
from orion.delegation.claude_code import AgentOutcome
from orion.delegation.jobs import JobService, delegation_tools
from orion.providers.base import ProviderError
from orion.tools.base import Effect
from orion.workflows.engine import (
    State,
    Step,
    StepCheck,
    StepContext,
    Verdict,
    Workflow,
    WorkflowEngine,
)

OWNER = Provenance(source="thread:owner", actor="orion:core")


async def _began(engine: WorkflowEngine, run_id: str, step: str) -> None:
    for _ in range(200):
        await engine.sync()
        if engine.projection.runs[run_id].current == step:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{step} never began")


async def _kill(engine: WorkflowEngine) -> None:
    """The process dies: the step under way never settles."""
    flying = list(engine._flying.values())  # pyright: ignore[reportPrivateUsage]
    for task in flying:
        task.cancel()
    await asyncio.gather(*flying, return_exceptions=True)


async def _hang(ctx: StepContext) -> dict[str, Any]:
    await asyncio.Event().wait()
    return {}


async def _restart(journal: SqliteJournal, workflow: Workflow) -> WorkflowEngine:
    engine = WorkflowEngine(journal, [workflow], "test")
    await engine.recover()
    await engine.resume_all()
    await engine.wait()
    await engine.sync()
    return engine


async def test_a_run_resumes_after_a_restart_without_redoing_finished_steps(
    journal: SqliteJournal,
) -> None:
    done: list[str] = []

    def step(name: str) -> Step:
        async def run(ctx: StepContext) -> dict[str, Any]:
            done.append(name)
            return {"seen": sorted(ctx.results)}

        return Step(name, run)

    before = WorkflowEngine(
        journal, [Workflow("w", (step("a"), Step("b", _hang), step("c")))], "test"
    )
    run_id = await before.start("w", "trois étapes", {}, OWNER)
    await _began(before, run_id, "b")
    await _kill(before)

    after = await _restart(journal, Workflow("w", (step("a"), step("b"), step("c"))))
    run = after.projection.runs[run_id]
    assert run.state is State.SUCCEEDED and done == ["a", "b", "c"]
    assert run.results["c"] == {"seen": ["a", "b"]}


@pytest.mark.parametrize("killed", ["before the send", "after the send"])
async def test_an_outside_effect_cut_short_is_checked_never_replayed(
    journal: SqliteJournal, killed: str
) -> None:
    sent: list[str] = []

    reached = asyncio.Event()

    async def send_then_die(ctx: StepContext) -> dict[str, Any]:
        if killed == "after the send":
            sent.append(ctx.key)
        reached.set()
        await asyncio.Event().wait()
        return {}

    async def send(ctx: StepContext) -> dict[str, Any]:
        sent.append(ctx.key)
        return {"id": ctx.key}

    async def look(ctx: StepContext) -> Verdict:
        return Verdict(ctx.key in sent, {"id": ctx.key}, "seen in the outbox")

    def workflow(run: Any, check: StepCheck | None = None) -> Workflow:
        return Workflow("mail", (Step("send", run, effect=Effect.EXTERNAL, check=check),))

    before = WorkflowEngine(journal, [workflow(send_then_die)], "test")
    run_id = await before.start("mail", "envoyer le mail", {}, OWNER)
    await reached.wait()
    await _kill(before)

    blind = await _restart(journal, workflow(send))  # nothing to look with: it waits
    assert blind.projection.runs[run_id].state is State.UNCERTAIN
    assert len(sent) == (1 if killed == "after the send" else 0)

    # Checked: sent, it is done; not sent, it is safe to send now. Either way, once.
    checked = await _restart(journal, workflow(send, look))
    assert checked.projection.runs[run_id].state is State.SUCCEEDED
    assert sent == [f"{run_id}/send"]


async def test_a_model_out_of_quota_pauses_the_run_then_it_resumes(
    journal: SqliteJournal,
) -> None:
    now = [datetime(2026, 11, 6, 12, tzinfo=UTC)]
    calls: list[int] = []

    async def think(ctx: StepContext) -> dict[str, Any]:
        calls.append(ctx.attempt)
        if len(calls) == 1:
            raise ProviderError("usage limit", retryable=False, used_up="chatgpt")
        return {"ok": True}

    flow = Workflow("w", (Step("think", think, attempts=1),))
    engine = WorkflowEngine(journal, [flow], "test", lambda: now[0])
    run_id = await engine.start("w", "réfléchir", {}, OWNER)
    await engine.wait()
    await engine.sync()
    assert engine.projection.runs[run_id].state is State.WAITING

    await engine.resume_all()
    await engine.wait()
    assert len(calls) == 1  # still waiting

    now[0] += timedelta(minutes=20)
    await engine.resume_all()
    await engine.wait()
    await engine.sync()
    assert engine.projection.runs[run_id].state is State.SUCCEEDED


class FakeAgent:
    name = "fake"

    def __init__(self, allow_bash: bool) -> None:
        self.allow_bash = allow_bash
        self.runs = 0
        self.hang = True
        self.started = asyncio.Event()
        self.leaves: AgentOutcome | None = None

    async def run(
        self, task: str, workdir: Path, options: dict[str, Any] | None = None
    ) -> AgentOutcome:
        self.runs += 1
        self.started.set()
        if self.hang:
            await asyncio.Event().wait()
        return AgentOutcome(True, "fini")

    def left(self, workdir: Path) -> AgentOutcome | None:
        return self.leaves


@pytest.mark.parametrize("shell", [False, True])
async def test_a_delegated_job_cut_by_a_restart_resumes_unless_it_had_a_shell(
    journal: SqliteJournal, tmp_path: Path, shell: bool
) -> None:
    agent = FakeAgent(shell)
    jobs = JobService(journal, agent, tmp_path)
    job_id = await jobs.start("Rapport", "écris le rapport", OWNER, None)
    await agent.started.wait()
    await _kill(jobs.engine)

    agent.hang = False
    later = datetime.now(UTC) + timedelta(hours=2)
    restarted = JobService(journal, agent, tmp_path, clock=lambda: later)
    await restarted.recover()
    await restarted.engine.resume_all()
    await restarted.wait()
    await restarted.sync()
    job = restarted.projection.jobs[job_id]
    if shell:  # it may have run commands: told as uncertain, never run again
        assert not job.finished and agent.runs == 1
        status = await delegation_tools(restarted)[1].handler(None, None)  # pyright: ignore[reportArgumentType]
        assert "incertaine" in status.content
    else:
        assert job.finished and job.ok and agent.runs == 2


async def test_a_job_with_a_done_when_is_reported_unproven_not_done(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    """D274: the agent's word does not show « la page est en ligne »."""
    agent = FakeAgent(allow_bash=False)
    agent.hang = False
    jobs = JobService(journal, agent, tmp_path)
    await jobs.start("Page", "publie la page", OWNER, None, done_when="la page est en ligne")
    await jobs.wait()
    [report] = await jobs.reports()
    assert "non prouvée" in report.notice and "la page est en ligne" in report.notice


async def test_a_delegated_shell_job_cut_by_a_restart_ends_alone_when_its_result_is_there(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    """ORI-010: the agent finished while Orion was down; its result is in the job's directory."""
    agent = FakeAgent(True)
    jobs = JobService(journal, agent, tmp_path)
    job_id = await jobs.start("Rapport", "écris le rapport", OWNER, None)
    await agent.started.wait()
    await _kill(jobs.engine)

    agent.leaves = AgentOutcome(True, "rapport écrit", files=["rapport.md"])
    restarted = JobService(journal, agent, tmp_path)
    await restarted.recover()
    await restarted.engine.resume_all()
    await restarted.wait()
    await restarted.sync()
    job = restarted.projection.jobs[job_id]
    assert job.finished and job.ok and job.files == ("rapport.md",)
    assert agent.runs == 1  # never run again
