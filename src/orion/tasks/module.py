"""Background tasks as a module: `[modules.tasks]` in orion.toml (on by default)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from orion.cognition.loop import StopReason
from orion.core.events import Provenance, Trust
from orion.initiative.engine import Alert
from orion.modules.base import Daemon, Module, ModuleSettings, Plate
from orion.providers.base import ProviderError
from orion.runtime import Orion, is_silence
from orion.tasks.service import Task, Tasks
from orion.tools.actions import grouped
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult
from orion.workflows.engine import Run, Step, StepContext, Workflow, WorkflowEngine

if TYPE_CHECKING:
    from orion.config import Effort, OrionConfig

log = structlog.get_logger(__name__)

REPORT_EXPIRES = timedelta(days=1)
PASS = "task.pass"
LIMITS = {
    "budget": "son budget est épuisé",
    "time": "son temps est écoulé",
    "passes": "elle a fait le nombre de passages maximum",
}


class TaskSettings(ModuleSettings):
    enabled: bool = True
    max_running: int = Field(default=3, ge=1, description="Tasks running at the same time")


class TaskWork:
    """Runs each task pass after pass, and hands what they write to Orion as alerts.

    Each pass is the one step of a workflow run (F-05, D257): journaled before it runs, so a
    pass cut by a restart is redone once, never the passes already done; a model out of quota
    pauses the pass instead of ending the task. The calls a cut pass made are settled
    UNCERTAIN at startup (D271), so the redone pass sees them and never replays them blindly.
    """

    def __init__(
        self,
        tasks: Tasks,
        orion: Orion,
        clock: Callable[[], datetime],
        model: str | None = None,
        effort: Effort | None = None,
    ) -> None:
        self._tasks = tasks
        self._orion = orion
        self._model = model
        """`[models] tasks` (D233); None: the conversation's."""
        self._effort: Effort | None = effort
        self._clock = clock
        self._woken = asyncio.Event()
        self.engine = WorkflowEngine(
            tasks.journal,
            [Workflow(PASS, (Step("pass", self._pass, attempts=2),), ended=self._ended)],
            "orion:tasks",
            clock,
        )
        tasks.waking.append(self._woken.set)

    async def run(self, tick: float = 30.0) -> None:
        while True:
            await self.engine.resume_all()  # passes cut by a restart, or waiting for the model
            await self.start_ready()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._woken.wait(), tick)
            self._woken.clear()

    async def start_ready(self) -> list[str]:
        await self.engine.sync()
        await self._tasks.sync()
        busy = {
            str(r.inputs.get("task_id"))
            for r in self.engine.mine()
            if not r.finished and int(r.inputs.get("pass", 0)) > self._passes(r)
        }
        started: list[str] = []
        for task in await self._tasks.running():
            if task.ready() and task.id not in busy:
                await self.engine.start(
                    PASS,
                    task.goal,
                    {"task_id": task.id, "pass": task.passes + 1},
                    Provenance(source=task.thread, actor="orion:tasks"),
                )
                started.append(task.id)
        return started

    async def settle(self) -> None:
        """For tests: let the passes under way finish."""
        await self.engine.wait()

    def _passes(self, run: Run) -> int:
        task = self._tasks.state.tasks.get(str(run.inputs.get("task_id")))
        return task.passes if task is not None else 1 << 30

    async def _pass(self, ctx: StepContext) -> dict[str, Any]:
        task = await self._tasks.get(str(ctx.inputs["task_id"]))
        # Done already (journaled before a restart cut the run) or ended: nothing to redo.
        if task is not None and task.finished is None and task.passes < int(ctx.inputs["pass"]):
            try:
                await self.one_pass(task)
            finally:
                self._woken.set()
        return {}

    async def _ended(self, run: Run) -> None:
        """A pass that failed for good ends its task, so Orion hears of it."""
        task = await self._tasks.get(str(run.inputs.get("task_id")))
        if task is None or task.finished is not None:
            return
        log.warning("task.pass_failed", task=task.id, error=run.blocked)
        await self._tasks.finish(
            task.id,
            "failed",
            run.blocked,
            Provenance(source=task.thread, actor="orion:tasks"),
            Trust.SYSTEM,
        )
        await self._tasks.report(
            task, f"(Fin de la tâche : échouée, {run.blocked[:300]}.)", self._clock(), Trust.SYSTEM
        )

    async def one_pass(self, task: Task) -> None:
        now = self._clock()
        limit = task.out_of(now)
        if limit:
            await self._end(task, limit, now)
            return

        async def stopped() -> bool:
            current = await self._tasks.get(task.id)
            return current is None or current.finished is not None

        read = len(task.said)
        turn = await self._orion.handle_initiative(
            task.thread,
            task.brief_for_pass(now),
            "tâche de fond",
            task.trust,
            now,
            stopped,
            model=self._model,
            effort=self._effort,
        )
        if turn.reason is StopReason.PROVIDER_ERROR:  # not a pass: the run waits, then redoes it
            raise ProviderError(turn.detail or "model unavailable", retryable=True)
        current = await self._tasks.get(task.id)
        if current is None:
            return
        await self._tasks.passed(current, turn.usage.cost_usd, read, current.waiting)
        log.info("task.passed", task=task.id, steps=turn.steps, cost=turn.usage.cost_usd)
        if turn.reason is StopReason.HALTED:
            return
        text = turn.text.strip()
        if current.finished in ("done", "unproven"):  # its summary went to Orion with `done`
            return
        if text and not is_silence(text):
            trust = self._orion.threads.get(task.thread).window_trust()
            await self._tasks.report(current, text, self._clock(), trust)
        current = await self._tasks.get(task.id)
        if current is not None and current.finished is None:
            limit = current.out_of(self._clock())
            if limit:
                await self._end(current, limit, self._clock())

    async def _end(self, task: Task, limit: str, now: datetime) -> None:
        provenance = Provenance(source=task.thread, actor="orion:tasks")
        await self._tasks.finish(task.id, limit, "", provenance, Trust.SYSTEM)
        await self._tasks.report(
            task,
            f"(Fin de la tâche : {LIMITS.get(limit, limit)}, après {task.passes} passage(s) et "
            f"{task.spent:.2f} $.)",
            now,
            Trust.SYSTEM,
        )

    async def alerts(self, now: datetime) -> list[Alert]:
        await self._tasks.sync()
        found: list[Alert] = []
        for task in self._tasks.state.tasks.values():
            for number, at, text, trust in task.reports:
                if now - at > REPORT_EXPIRES:
                    continue
                ended = f"\n(Elle est terminée : {task.finished}.)" if task.finished else ""
                found.append(
                    Alert(
                        key=f"task:{task.id}:{number}",
                        notice=f'<tache id="{task.id}" but="{task.goal}">\n{text}\n</tache>'
                        f"{ended}\n(Ta tâche de fond t'écrit, pas lui. Si elle attend une "
                        "réponse, `task` (say). Ce que tu écris ensuite part à ton "
                        "propriétaire : ce qui vaut pour lui (ses fichiers avec `file` send), "
                        "ou une question à lui poser ; rien pour lui : SILENCE.)",
                        why="ta tâche de fond t'a écrit",
                        fallback=f"Ta tâche « {task.goal} » : {text}",
                        trust=trust,
                        expires_at=at + REPORT_EXPIRES,
                        importance=0.7,
                        spoken_to=True,
                    )
                )
        return found


SUMMARY_MAX = 4000


class StartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=5, max_length=200)
    brief: str = Field(
        min_length=10,
        max_length=3000,
        description="All it needs; it sees nothing else (3000 characters at most)",
    )
    budget_usd: float = Field(default=1.0, gt=0, le=3.0, description="3 at most")
    minutes: int = Field(default=30, ge=5, le=180)
    done_when: str | None = Field(
        default=None,
        min_length=3,
        max_length=300,
        description="What will show it is done, when its word is not enough (a place still "
        "free, a document found): it then ends done only on a confirmed action showing it",
    )

    @field_validator("budget_usd", "minutes", mode="before")
    @classmethod
    def _capped(cls, value: object, info: Any) -> object:
        """Over the cap, the cap: refusing cost the task its turn twice on 05/11."""
        cap = {"budget_usd": 3.0, "minutes": 180}[info.field_name]
        return min(value, cap) if isinstance(value, int | float) else value


class SayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(description="t_…")
    text: str = Field(min_length=1, max_length=2000)


class StopInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str
    why: str = Field(default="", max_length=300)


class DoneInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=3, description=f"{SUMMARY_MAX} characters at most")
    seen: str = Field(
        default="",
        max_length=200,
        description="If it was given done_when: the action id whose CONFIRMED line shows it",
    )

    @field_validator("summary", mode="before")
    @classmethod
    def _clipped(cls, value: object) -> object:
        """A long report is cut, not refused: refused twice on 05/11, the steps were lost."""
        if isinstance(value, str) and len(value) > SUMMARY_MAX:
            return value[: SUMMARY_MAX - 1] + "…"
        return value


class NoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _origin(ctx: ToolContext) -> Provenance:
    return Provenance(source=f"thread:{ctx.thread_id}", actor="orion:core")


async def running_lines(tasks: Tasks) -> list[str]:
    """The background tasks still running, with their ids."""
    return [
        f"- ({t.id}) {t.goal} : {t.passes} passage(s), {t.spent:.2f}/{t.budget_usd:.2f} $"
        + (", attend ta réponse" if t.waiting else "")
        for t in await tasks.running()
    ]


def task_tools(tasks: Tasks, clock: Callable[[], datetime], max_running: int) -> list[Tool]:
    def inside(ctx: ToolContext) -> str:
        return ctx.thread_id.removeprefix("task:") if ctx.thread_id.startswith("task:") else ""

    starting = asyncio.Lock()  # counted and started as one: two at once never pass the cap

    async def start(args: Any, ctx: ToolContext) -> ToolResult:
        if inside(ctx):
            return ToolResult("A task does not start tasks: ask Orion.", is_error=True)
        async with starting:
            if len(await tasks.running()) >= max_running:
                return ToolResult(f"Already {max_running} tasks running: stop one.", is_error=True)
            task_id = await tasks.start(
                args.goal,
                args.brief,
                args.budget_usd,
                args.minutes,
                clock(),
                _origin(ctx),
                ctx.correlation_id,
                ctx.context_trust,
                args.done_when or "",
            )
        return ToolResult(
            f"Tâche lancée ({task_id}) : elle travaille déjà, et t'écrira ce qu'elle trouve. "
            "Tu lui réponds avec `task` (say)."
        )

    async def say(args: Any, ctx: ToolContext) -> ToolResult:
        if inside(ctx):
            return ToolResult("Write to Orion in your answer instead.", is_error=True)
        task = await tasks.get(args.task)
        if task is None or task.finished is not None:
            return ToolResult(f"No task {args.task} running.", is_error=True)
        await tasks.say(args.task, args.text, _origin(ctx), ctx.context_trust, ctx.correlation_id)
        return ToolResult("Transmis à la tâche : elle en tient compte à son prochain pas.")

    async def stop(args: Any, ctx: ToolContext) -> ToolResult:
        if inside(ctx):
            return ToolResult("Use `task` done to end your own task.", is_error=True)
        task = await tasks.get(args.task)
        if task is None or task.finished is not None:
            return ToolResult(f"No task {args.task} running.", is_error=True)
        await tasks.finish(args.task, "stopped", args.why, _origin(ctx), ctx.context_trust)
        return ToolResult("Tâche arrêtée : elle ne fait plus rien.")

    async def listing(_args: Any, _ctx: ToolContext) -> ToolResult:
        return ToolResult("\n".join(await running_lines(tasks)) or "Aucune tâche en cours.")

    async def wait(_args: Any, ctx: ToolContext) -> ToolResult:
        task_id = inside(ctx)
        if not task_id:
            return ToolResult("Only a task waits for Orion.", is_error=True)
        await tasks.wait(task_id, _origin(ctx))
        return ToolResult("Après ce passage, tu attends qu'Orion te réponde : pose ta question.")

    async def done(args: Any, ctx: ToolContext) -> ToolResult:
        task_id = inside(ctx)
        if not task_id:
            return ToolResult(
                "Only a task says it is done; stop it with `task` (stop).", is_error=True
            )
        task = await tasks.get(task_id)
        if task is None or task.finished is not None:
            return ToolResult("This task is already over.", is_error=True)
        how = await tasks.finish(
            task_id, "done", args.summary, _origin(ctx), ctx.context_trust, args.seen
        )
        if how == "unproven":
            await tasks.report(
                task,
                f"{args.summary}\n\n(Non prouvé : « {task.done_when} » n'est montré par aucune "
                "action confirmée. Ne lui dis pas que c'est fait : vérifie, ou dis-lui ce qui "
                "manque.)",
                clock(),
                ctx.context_trust,
            )
            return ToolResult(
                "Tâche close, non prouvée : aucune action confirmée ne montre « "
                f"{task.done_when} ». Orion le saura. Ne fais plus rien."
            )
        await tasks.report(task, args.summary, clock(), ctx.context_trust)
        return ToolResult("Tâche close : ton bilan part à Orion. Ne fais plus rien.")

    def tool(name: str, description: str, model: type[BaseModel], handler: Any) -> Tool:
        return Tool(
            name=name,
            description=description,
            input_model=model,
            handler=handler,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
            accepts_untrusted_context=True,
        )

    return [
        tool(
            "task_start",
            "Hand long work (a big search) to a helper, with a complete brief.",
            StartInput,
            start,
        ),
        tool("task_say", "Answer or steer a task.", SayInput, say),
        tool(
            "task_stop",
            "Stop a task.",
            StopInput,
            stop,
        ),
        tool("task_list", "The tasks running.", NoInput, listing),
        tool("task_wait", "Wait for Orion's answer.", NoInput, wait),
        tool("task_done", "Your task is done.", DoneInput, done),
    ]


def task_tool(tasks: Tasks, clock: Callable[[], datetime], max_running: int) -> Tool:
    return grouped(
        "task",
        "Background tasks: a helper works alone and reports to you, never to him. In a task, "
        "yours are wait and done.",
        task_tools(tasks, clock, max_running),
        {
            "task_start": "start",
            "task_say": "say",
            "task_stop": "stop",
            "task_list": "list",
            "task_wait": "wait",
            "task_done": "done",
        },
    )


class TasksModule(Module):
    name = "tasks"
    instructions = "tasks.md"
    settings_model = TaskSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.tasks: Tasks | None = None

    def _clock(self) -> datetime:
        return datetime.now(ZoneInfo(self.config.agent.timezone))

    async def assemble(self, plate: Plate) -> list[Tool]:
        settings: TaskSettings = self.settings
        self.tasks = Tasks(plate.journal)
        await self.tasks.sync()
        return [task_tool(self.tasks, self._clock, settings.max_running)]

    async def review(self, now: datetime) -> list[str]:
        if self.tasks is None:
            return []
        return await running_lines(self.tasks)

    async def serve(self, daemon: Daemon) -> None:
        if self.tasks is None:  # pragma: no cover - assemble always runs first
            return
        work = TaskWork(
            self.tasks,
            daemon.orion,
            self._clock,
            self.config.models.tasks.model,
            self.config.effort("tasks"),
        )
        await work.engine.recover()
        daemon.alerts.append(work)
        daemon.add_service("tasks.work", work.run)
