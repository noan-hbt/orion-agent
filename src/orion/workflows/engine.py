"""A workflow run: steps done in order, each journaled before and after it runs (D257).

    workflow.started → (workflow.step_began → workflow.step_done | step_failed | step_uncertain
    | workflow.waiting)* → workflow.finished

A finished step's result is its checkpoint: after a restart the run picks up at the first step
without one and never redoes the others. A step cut short is redone if it had no effect outside
Orion; if it had one (a message, a command on the server) nobody knows whether it happened, so
the run turns UNCERTAIN and the step's own `check` looks before anything is tried again. Without
a check, or when the check cannot tell, the run stays UNCERTAIN and the owner is told: never a
blind replay. A model out of quota or unreachable puts the run to WAITING without using up an
attempt; it resumes on its own once the wait is over.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import structlog

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.providers.base import ProviderError
from orion.tools.base import Effect

log = structlog.get_logger(__name__)

STARTED = "workflow.started"
STEP_BEGAN = "workflow.step_began"
STEP_DONE = "workflow.step_done"
STEP_FAILED = "workflow.step_failed"
STEP_UNCERTAIN = "workflow.step_uncertain"
WAITING = "workflow.waiting"
FINISHED = "workflow.finished"

QUOTA_WAIT = timedelta(minutes=15)
MAX_BACKOFF = timedelta(hours=1)


class State(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    """Until `Run.until`: a retry after a failure, or a model out of quota."""
    UNCERTAIN = "uncertain"
    """A step with an outside effect was cut short: it may or may not have happened."""
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


DONE_STATES = frozenset({State.SUCCEEDED, State.FAILED, State.CANCELLED})


@dataclass(frozen=True)
class StepContext:
    run_id: str
    step_id: str
    attempt: int
    inputs: dict[str, Any]
    results: dict[str, dict[str, Any]]
    """The checkpoints of the steps already done."""
    correlation_id: str

    @property
    def key(self) -> str:
        """Idempotency key for whatever the step sends out: the same on every attempt."""
        return f"{self.run_id}/{self.step_id}"


@dataclass(frozen=True)
class Verdict:
    """What a step's check found out about an attempt cut short."""

    happened: bool | None
    """True: it took effect; False: it did not (safe to try again); None: cannot tell."""
    result: dict[str, Any] = field(default_factory=dict[str, Any])
    evidence: str = ""


class StepFailed(Exception):
    """Raised by a step that knows for sure its effect did not happen."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


StepRun = Callable[[StepContext], Awaitable[dict[str, Any]]]
StepCheck = Callable[[StepContext], Awaitable[Verdict]]


@dataclass(frozen=True)
class Step:
    id: str
    run: StepRun
    effect: Effect = Effect.INTERNAL
    check: StepCheck | None = None
    """For a step with an outside effect: did an attempt cut short take effect?"""
    attempts: int = 3
    timeout: float | None = None
    """Seconds; past it, the attempt counts as cut short."""


@dataclass(frozen=True)
class Workflow:
    name: str
    steps: tuple[Step, ...]
    ended: Callable[[Run], Awaitable[None]] | None = None
    """Called once a run fails or is cancelled (its owner settles what it started)."""

    def step(self, step_id: str) -> Step:
        return next(s for s in self.steps if s.id == step_id)


@dataclass
class Run:
    id: str
    workflow: str
    goal: str
    inputs: dict[str, Any]
    steps: tuple[str, ...]
    correlation_id: str
    state: State = State.QUEUED
    results: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    attempts: dict[str, int] = field(default_factory=dict[str, int])
    current: str | None = None
    """A step begun and not settled (running, or cut short)."""
    until: datetime | None = None
    blocked: str = ""
    """Why it waits, is uncertain or failed."""

    @property
    def finished(self) -> bool:
        return self.state in DONE_STATES

    @property
    def next_step(self) -> str | None:
        if self.current is not None:
            return self.current
        return next((s for s in self.steps if s not in self.results), None)


class RunProjection:
    name = "workflows"

    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}
        self._seq = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.runs.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted or not event.kind.startswith("workflow."):
            return
        p: dict[str, Any] = event.payload
        if event.kind == STARTED:
            self.runs[p["run_id"]] = Run(
                p["run_id"],
                str(p["workflow"]),
                str(p["goal"]),
                dict(p.get("inputs", {})),
                tuple(p["steps"]),
                event.correlation_id or "",
            )
            return
        run = self.runs.get(str(p.get("run_id")))
        if run is None:
            return
        step = str(p.get("step", ""))
        if event.kind == STEP_BEGAN:
            run.current, run.state, run.until, run.blocked = step, State.RUNNING, None, ""
            run.attempts[step] = int(p["attempt"])
        elif event.kind == STEP_DONE:
            run.results[step] = dict(p.get("result", {}))
            run.current, run.state, run.blocked = None, State.RUNNING, ""
        elif event.kind == STEP_FAILED:
            run.current, run.state, run.blocked = None, State.RUNNING, str(p["error"])
            if not p.get("counts", True):
                run.attempts[step] = run.attempts.get(step, 1) - 1
        elif event.kind == STEP_UNCERTAIN:
            run.current, run.state, run.blocked = step, State.UNCERTAIN, str(p["reason"])
        elif event.kind == WAITING:
            run.state, run.blocked = State.WAITING, str(p["reason"])
            run.until = datetime.fromisoformat(p["until"])
        elif event.kind == FINISHED:
            run.state, run.current, run.until = State(p["state"]), None, None
            run.blocked = str(p.get("error") or "")


class WorkflowEngine:
    """Runs the workflows it is given. `recover` at startup only journals what a restart cut
    short; `run` (the daemon's service) is what resumes the runs."""

    def __init__(
        self,
        journal: EventStore,
        workflows: Sequence[Workflow],
        actor: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._journal = journal
        self._workflows = {w.name: w for w in workflows}
        self._actor = actor
        self._clock = clock
        self.projection = RunProjection()
        self._runner = ProjectionRunner(journal, [self.projection])
        self._flying: dict[str, asyncio.Task[None]] = {}

    async def sync(self) -> None:
        await self._runner.catch_up()

    def mine(self) -> list[Run]:
        return [r for r in self.projection.runs.values() if r.workflow in self._workflows]

    async def start(
        self,
        workflow: str,
        goal: str,
        inputs: dict[str, Any],
        provenance: Provenance,
        correlation_id: str | None = None,
    ) -> str:
        """`inputs` are journaled: never a secret (a `SecretRef` name at most)."""
        run_id = f"w_{new_id()}"
        steps = [s.id for s in self._workflows[workflow].steps]
        payload = {"run_id": run_id, "workflow": workflow, "goal": goal}
        await self._append(
            STARTED, {**payload, "inputs": inputs, "steps": steps}, correlation_id, provenance
        )
        self._spawn(run_id)
        return run_id

    async def recover(self) -> None:
        """A step begun before a restart was cut short: redone if it had no outside effect,
        UNCERTAIN otherwise."""
        await self.sync()
        for run in self.mine():
            if run.state is not State.RUNNING or run.current is None:
                continue
            step = self._workflows[run.workflow].step(run.current)
            log.warning("workflow.interrupted", run_id=run.id, step=step.id)
            await self._cut_short(
                run, step, run.attempts.get(step.id, 1), "interrupted by a restart", pause=False
            )

    async def run(self, tick: float = 60.0) -> None:
        """Daemon service: resume runs left by a restart, wake waiting and uncertain ones."""
        while True:
            await self.resume_all()
            await asyncio.sleep(tick)

    async def resume_all(self) -> None:
        await self.sync()
        for run in self.mine():
            if not run.finished:
                self._spawn(run.id)

    async def wait(self) -> None:
        """For tests and shutdown: wait for the runs under way."""
        while self._flying:
            await asyncio.gather(*self._flying.values())

    async def cancel(self, run_id: str, why: str) -> None:
        await self.sync()
        run = self.projection.runs.get(run_id)
        if run is not None and not run.finished:
            await self._finish(run, State.CANCELLED, why)

    def _spawn(self, run_id: str) -> None:
        if run_id in self._flying:
            return
        task = asyncio.create_task(self._advance_logged(run_id), name=f"workflow:{run_id}")
        self._flying[run_id] = task
        task.add_done_callback(lambda _: self._flying.pop(run_id, None))

    async def _advance_logged(self, run_id: str) -> None:
        try:
            await self.advance(run_id)
        except Exception:
            log.exception("workflow.advance_failed", run_id=run_id)
            raise

    async def advance(self, run_id: str) -> None:
        """Run steps until the run ends, waits, or is left uncertain."""
        while True:
            await self.sync()
            run = self.projection.runs[run_id]
            workflow = self._workflows[run.workflow]
            if run.finished:
                return
            if run.state is State.UNCERTAIN:
                if not await self._reconcile(run, workflow.step(run.current or "")):
                    return
                continue
            if run.state is State.WAITING and run.until is not None and run.until > self._clock():
                return
            if run.current is not None:  # begun by a process that is gone: not recovered yet
                return
            step_id = run.next_step
            if step_id is None:
                await self._finish(run, State.SUCCEEDED, None)
                return
            step = workflow.step(step_id)
            attempt = run.attempts.get(step_id, 0) + 1
            if attempt > step.attempts:
                await self._finish(run, State.FAILED, f"{step_id}: {run.blocked or 'échecs'}")
                return
            await self._run_step(run, step, attempt)

    async def _run_step(self, run: Run, step: Step, attempt: int) -> None:
        base = {"run_id": run.id, "step": step.id}
        await self._append(STEP_BEGAN, {**base, "attempt": attempt}, run.correlation_id)
        ctx = StepContext(
            run.id, step.id, attempt, run.inputs, dict(run.results), run.correlation_id
        )
        try:
            result = await asyncio.wait_for(step.run(ctx), step.timeout)
        except StepFailed as exc:
            await self._append(STEP_FAILED, {**base, "error": str(exc)}, run.correlation_id)
            if not exc.retryable:
                await self._finish(run, State.FAILED, f"{step.id}: {exc}")
            else:
                await self._wait(run, _backoff(attempt), f"{step.id} : nouvel essai ({exc})")
        except ProviderError as exc:
            if not (exc.retryable or exc.used_up):
                await self._cut_short(run, step, attempt, f"model error: {exc}")
                return
            log.warning("workflow.model_unavailable", run_id=run.id, step=step.id, error=str(exc))
            error = f"model unavailable: {exc}"
            await self._append(
                STEP_FAILED, {**base, "error": error, "counts": False}, run.correlation_id
            )
            await self._wait(run, QUOTA_WAIT, "modèle indisponible ou quota épuisé")
        except Exception as exc:
            log.exception("workflow.step_crashed", run_id=run.id, step=step.id)
            await self._cut_short(run, step, attempt, f"crashed: {exc!r}")
        else:
            await self._append(STEP_DONE, {**base, "result": result}, run.correlation_id)

    async def _cut_short(
        self, run: Run, step: Step, attempt: int, why: str, pause: bool = True
    ) -> None:
        """`pause`: wait before the next attempt (not after a restart: not the step's fault)."""
        base = {"run_id": run.id, "step": step.id}
        if step.effect is Effect.EXTERNAL:
            await self._append(STEP_UNCERTAIN, {**base, "reason": why}, run.correlation_id)
            return
        await self._append(STEP_FAILED, {**base, "error": why}, run.correlation_id)
        if pause:
            await self._wait(run, _backoff(attempt), f"{step.id} : {why}")

    async def _reconcile(self, run: Run, step: Step) -> bool:
        """Look before trying an uncertain step again. False: still uncertain."""
        if step.check is None:
            return False
        ctx = StepContext(
            run.id,
            step.id,
            run.attempts.get(step.id, 1),
            run.inputs,
            dict(run.results),
            run.correlation_id,
        )
        try:
            verdict = await step.check(ctx)
        except Exception:
            log.exception("workflow.check_failed", run_id=run.id, step=step.id)
            return False
        base = {"run_id": run.id, "step": step.id, "reconciled": verdict.evidence or True}
        if verdict.happened is True:
            await self._append(STEP_DONE, {**base, "result": verdict.result}, run.correlation_id)
            return True
        if verdict.happened is False:
            error = "checked: had not happened"
            await self._append(STEP_FAILED, {**base, "error": error}, run.correlation_id)
            return True
        return False

    async def _wait(self, run: Run, delay: timedelta, reason: str) -> None:
        until = (self._clock() + delay).isoformat()
        payload = {"run_id": run.id, "until": until, "reason": reason}
        await self._append(WAITING, payload, run.correlation_id)

    async def _finish(self, run: Run, state: State, error: str | None) -> None:
        log.info("workflow.finished", run_id=run.id, state=state, error=error)
        payload = {"run_id": run.id, "state": state.value, "error": error}
        await self._append(FINISHED, payload, run.correlation_id)
        ended = self._workflows[run.workflow].ended
        if ended is not None and state is not State.SUCCEEDED:
            await ended(self.projection.runs[run.id])

    async def _append(
        self,
        kind: str,
        payload: dict[str, Any],
        correlation_id: str | None,
        provenance: Provenance | None = None,
    ) -> None:
        source = f"workflow:{payload['run_id']}"
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=provenance or Provenance(source=source, actor=self._actor),
                    trust=Trust.SYSTEM,
                    correlation_id=correlation_id or new_id(),
                )
            ]
        )
        await self.sync()


def _backoff(attempt: int) -> timedelta:
    return min(timedelta(minutes=1) * 2 ** max(attempt - 1, 0), MAX_BACKOFF)


STATE_LABELS = {
    State.QUEUED: "en attente de démarrage",
    State.RUNNING: "en cours",
    State.WAITING: "en pause",
    State.UNCERTAIN: "issue incertaine",
    State.SUCCEEDED: "terminé",
    State.FAILED: "échoué",
    State.CANCELLED: "annulé",
}


def describe(run: Run) -> str:
    """One line for Orion: state, what blocks it, next step, result."""
    parts = [f"- ({run.id}) {run.goal} : {STATE_LABELS[run.state]}"]
    done = len(run.results)
    parts.append(f"{done}/{len(run.steps)} étapes faites")
    if not run.finished and run.next_step:
        parts.append(f"prochaine étape : {run.next_step}")
    if run.state is State.WAITING and run.until is not None:
        parts.append(f"reprise vers {run.until:%d/%m %H:%M}")
    if run.blocked:
        parts.append(f"bloqué par : {run.blocked[:200]}")
    if run.state is State.SUCCEEDED and run.steps:
        last = run.results.get(run.steps[-1], {})
        if last:
            parts.append(f"résultat : {str(last)[:200]}")
    return " ; ".join(parts)
