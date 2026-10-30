"""Background tasks as a module: `[modules.tasks]` in orion.toml (on by default)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import structlog
from pydantic import BaseModel, ConfigDict, Field

from orion.cognition.loop import StopReason
from orion.core.events import Provenance, Trust
from orion.initiative.engine import Alert
from orion.modules.base import Daemon, Module, ModuleSettings, Plate
from orion.runtime import Orion, is_silence
from orion.tasks.service import Task, Tasks
from orion.tools.actions import grouped
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig

log = structlog.get_logger(__name__)

REPORT_EXPIRES = timedelta(days=1)
LIMITS = {
    "budget": "son budget est épuisé",
    "time": "son temps est écoulé",
    "passes": "elle a fait le nombre de passages maximum",
}


class TaskSettings(ModuleSettings):
    enabled: bool = True
    max_running: int = Field(default=3, ge=1, description="Tasks running at the same time")


class TaskWork:
    """Runs each task pass after pass, and hands what they write to Orion as alerts."""

    def __init__(self, tasks: Tasks, orion: Orion, clock: Callable[[], datetime]) -> None:
        self._tasks = tasks
        self._orion = orion
        self._clock = clock
        self._woken = asyncio.Event()
        self._running: dict[str, asyncio.Task[None]] = {}
        tasks.waking.append(self._woken.set)

    async def run(self, tick: float = 30.0) -> None:
        while True:
            await self.start_ready()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._woken.wait(), tick)
            self._woken.clear()

    async def start_ready(self) -> list[str]:
        started: list[str] = []
        for task in await self._tasks.running():
            if task.ready() and task.id not in self._running:
                self._running[task.id] = asyncio.create_task(self._pass_then_wake(task))
                started.append(task.id)
        return started

    async def settle(self) -> None:
        """For tests: let the passes under way finish."""
        while self._running:
            await asyncio.gather(*self._running.values())

    async def _pass_then_wake(self, task: Task) -> None:
        try:
            await self.one_pass(task)
        except Exception:
            log.exception("task.pass_failed", task=task.id)
            await self._tasks.finish(
                task.id,
                "failed",
                "",
                Provenance(source=task.thread, actor="orion:tasks"),
                Trust.SYSTEM,
            )
        finally:
            self._running.pop(task.id, None)
            self._woken.set()

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
            task.thread, task.brief_for_pass(now), "tâche de fond", task.trust, now, stopped
        )
        current = await self._tasks.get(task.id)
        if current is None:
            return
        await self._tasks.passed(current, turn.usage.cost_usd, read, current.waiting)
        log.info("task.passed", task=task.id, steps=turn.steps, cost=turn.usage.cost_usd)
        if turn.reason is StopReason.HALTED:
            return
        text = turn.text.strip()
        if current.finished == "done":  # its summary went to Orion with `done`
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


class StartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=5, max_length=200)
    brief: str = Field(
        min_length=10,
        max_length=3000,
        description="All it needs; it sees nothing else",
    )
    budget_usd: float = Field(default=1.0, gt=0, le=3.0)
    minutes: int = Field(default=30, ge=5, le=180)


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

    summary: str = Field(min_length=3, max_length=2000)


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
        await tasks.finish(task_id, "done", args.summary, _origin(ctx), ctx.context_trust)
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
        work = TaskWork(self.tasks, daemon.orion, self._clock)
        daemon.alerts.append(work)
        daemon.add_service("tasks.work", work.run)
