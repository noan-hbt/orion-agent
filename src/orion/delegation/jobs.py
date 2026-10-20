"""Delegated jobs: long tasks run in the background, reported when done.

    job.started → job.finished

Orion answers right away ("je lance, je te dis quand c'est fini"); the job runs off the
conversation; the initiative engine reports the result in the owner's thread when it lands. A
job interrupted by a restart is marked failed on startup, never silently forgotten.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.delegation.claude_code import AgentOutcome
from orion.initiative.engine import Report
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

log = structlog.get_logger(__name__)

JOB_STARTED = "job.started"
JOB_FINISHED = "job.finished"
MAX_RESULT_CHARS = 4000


class DelegatedAgent(Protocol):
    name: str
    allow_bash: bool

    async def run(
        self, task: str, workdir: Path, options: dict[str, Any] | None = None
    ) -> AgentOutcome:
        """`options`: per-task settings that are never journaled (a site session…)."""
        ...


@dataclass(frozen=True)
class Job:
    id: str
    title: str
    task: str
    workdir: str
    finished: bool = False
    ok: bool = False
    result: str = ""
    files: tuple[str, ...] = ()
    error: str | None = None
    cost_usd: float = 0.0


class JobProjection:
    name = "jobs"

    def __init__(self, agent: str | None = None) -> None:
        self.jobs: dict[str, Job] = {}
        self._agent = agent
        """Only this agent's jobs (several job services share the journal); None: all."""
        self._seq = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.jobs.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind == JOB_STARTED:
            if self._agent is not None and payload.get("agent", self._agent) != self._agent:
                return
            job_id = str(payload["job_id"])
            self.jobs[job_id] = Job(
                job_id, str(payload["title"]), str(payload["task"]), str(payload["workdir"])
            )
        elif event.kind == JOB_FINISHED:
            job = self.jobs.get(str(payload["job_id"]))
            if job is not None:
                self.jobs[job.id] = Job(
                    job.id,
                    job.title,
                    job.task,
                    job.workdir,
                    finished=True,
                    ok=bool(payload["ok"]),
                    result=str(payload.get("result", "")),
                    files=tuple(str(f) for f in payload.get("files", [])),
                    error=payload.get("error"),
                    cost_usd=float(payload.get("cost_usd", 0.0)),
                )

    def running(self) -> list[Job]:
        return [j for j in self.jobs.values() if not j.finished]

    def finished(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.finished]


class JobService:
    def __init__(
        self,
        journal: EventStore,
        agent: DelegatedAgent,
        workspaces: Path,
        result_trust: Trust = Trust.SYSTEM,
    ) -> None:
        self._journal = journal
        self._agent = agent
        self._workspaces = workspaces
        self._result_trust = result_trust
        """What a result is worth: a browsing agent's report is web content."""
        self.projection = JobProjection(agent.name)
        self._runner = ProjectionRunner(journal, [self.projection])
        self._tasks: set[asyncio.Task[None]] = set()
        self._options: dict[str, dict[str, Any]] = {}

    @property
    def agent(self) -> DelegatedAgent:
        return self._agent

    async def sync(self) -> None:
        await self._runner.catch_up()

    async def recover(self) -> None:
        """Jobs started before a restart did not survive it: say so in the journal."""
        await self.sync()
        for job in self.projection.running():
            log.warning("job.interrupted", job_id=job.id)
            await self._finish(job, AgentOutcome(False, "", error="interrupted by a restart"), None)

    async def start(
        self,
        title: str,
        task: str,
        provenance: Provenance,
        correlation_id: str | None,
        options: dict[str, Any] | None = None,
    ) -> str:
        """`options` reach the agent in memory only (a restart fails the job anyway)."""
        job_id = f"j_{new_id()}"
        if options:
            self._options[job_id] = options
        workdir = self._workspaces / job_id
        await self._append(
            JOB_STARTED,
            {
                "job_id": job_id,
                "title": title,
                "task": task,
                "workdir": str(workdir),
                "agent": self._agent.name,
            },
            provenance,
            correlation_id,
        )
        job = self.projection.jobs[job_id]
        task_handle = asyncio.create_task(self._run(job, correlation_id), name=f"job:{job_id}")
        self._tasks.add(task_handle)
        task_handle.add_done_callback(self._tasks.discard)
        return job_id

    async def reports(self) -> list[Report]:
        await self.sync()
        return [_report(job, self._result_trust) for job in self.projection.finished()]

    async def wait(self) -> None:
        """For tests and shutdown: wait for running jobs."""
        if self._tasks:
            await asyncio.gather(*self._tasks)

    async def _run(self, job: Job, correlation_id: str | None) -> None:
        try:
            outcome = await self._agent.run(
                job.task, Path(job.workdir), self._options.pop(job.id, None)
            )
        except Exception as exc:
            log.exception("job.crashed", job_id=job.id)
            outcome = AgentOutcome(False, "", error=f"crashed: {exc!r}")
        await self._finish(job, outcome, correlation_id)

    async def _finish(self, job: Job, outcome: AgentOutcome, correlation_id: str | None) -> None:
        log.info("job.finished", job_id=job.id, ok=outcome.ok, error=outcome.error)
        await self._append(
            JOB_FINISHED,
            {
                "job_id": job.id,
                "ok": outcome.ok,
                "result": outcome.result[:MAX_RESULT_CHARS],
                "files": outcome.files[:50],
                "error": outcome.error,
                "cost_usd": outcome.cost_usd,
            },
            Provenance(source=f"job:{job.id}", actor=f"agent:{self._agent.name}"),
            correlation_id,
            self._result_trust,
        )

    async def _append(
        self,
        kind: str,
        payload: dict[str, object],
        provenance: Provenance,
        correlation_id: str | None,
        trust: Trust = Trust.SYSTEM,
    ) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=provenance,
                    trust=trust,
                    correlation_id=correlation_id or new_id(),
                )
            ]
        )
        await self.sync()


def _report(job: Job, trust: Trust = Trust.SYSTEM) -> Report:
    status = "terminée" if job.ok else f"échouée ({job.error})"
    files = ", ".join(job.files[:10]) or "aucun"
    return Report(
        key=f"job:{job.id}",
        notice=f"Tâche déléguée « {job.title} » {status} ({job.id}).\n"
        f"Compte rendu de l'agent :\n{job.result or '(vide)'}\n"
        f"Fichiers produits, dans {job.workdir} : {files}",
        why="la tâche que tu m'as confiée vient de se terminer",
        fallback=f"Tâche « {job.title} » {status}. "
        + (f"Fichiers dans {job.workdir} : {files}" if job.ok else ""),
        trust=trust,
    )


class DelegateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=80, description="A few words naming the task")
    task: str = Field(
        min_length=10,
        description="Complete, self-contained instructions: the agent sees nothing else. Say "
        "what to produce, in which form, and any constraint the owner gave.",
    )


class JobsStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def delegation_tools(jobs: JobService) -> list[Tool]:
    async def delegate(args: Any, ctx: ToolContext) -> ToolResult:
        job_id = await jobs.start(
            args.title,
            args.task,
            Provenance(
                source=f"thread:{ctx.thread_id}",
                actor="orion:core",
                derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
            ),
            ctx.correlation_id,
        )
        return ToolResult(
            f"Tâche lancée ({job_id}). Le résultat arrivera par message quand elle sera finie.",
            data={"job_id": job_id},
        )

    async def jobs_status(args: Any, ctx: ToolContext) -> ToolResult:
        await jobs.sync()
        lines = [f"- ({j.id}) {j.title} : en cours" for j in jobs.projection.running()]
        lines += [
            f"- ({j.id}) {j.title} : {'terminée' if j.ok else 'échec : ' + (j.error or '?')}"
            for j in jobs.projection.finished()[-10:]
        ]
        return ToolResult("\n".join(lines) if lines else "Aucune tâche déléguée.")

    shell = jobs.agent.allow_bash
    return [
        Tool(
            name="delegate",
            description="Hand a long or technical task (code, a script, a document, research "
            "in files) to a coding agent working in its own folder on the server. It runs in "
            "the background; you get the result later and pass it on. Do not wait for it.",
            input_model=DelegateInput,
            handler=delegate,
            # Without a shell the agent can only write files in its workspace; with one, it
            # runs commands on the server, which needs the owner's approval each time.
            effect=Effect.EXTERNAL if shell else Effect.INTERNAL,
            reversibility=Reversibility.IRREVERSIBLE if shell else Reversibility.REVERSIBLE,
        ),
        Tool(
            name="jobs_status",
            description="List delegated tasks and whether they are running, done or failed.",
            input_model=JobsStatusInput,
            handler=jobs_status,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        ),
    ]
