"""Background tasks (D110): work Orion hands to a helper of its own, who reports to Orion.

A task is Orion itself, in a thread of its own (`task:<id>`), given a brief: it searches, reads,
downloads, pass after pass, until it is done or its budget (money, time, passes) runs out. It
never talks to the owner: what it has to say goes to Orion, who decides what reaches him, answers
the task, asks him when it needs to, or stops it. So a long search no longer swells the owner's
conversation, and « je continue » is true: the task carries on by itself.

    task.started  task.said (Orion → task)  task.reported (task → Orion)  task.waiting
    task.passed  task.finished
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner

STARTED = "task.started"
SAID = "task.said"
REPORTED = "task.reported"
WAITING = "task.waiting"
PASSED = "task.passed"
FINISHED = "task.finished"
MAX_PASSES = 20


@dataclass
class Task:
    id: str
    goal: str
    brief: str
    budget_usd: float
    minutes: int
    started_at: datetime
    said: list[str] = field(default_factory=list[str])
    """What Orion wrote to it, in order."""
    read: int = 0
    """How many of those its passes have taken in."""
    reports: list[tuple[int, datetime, str, Trust]] = field(
        default_factory=list[tuple[int, datetime, str, Trust]]
    )
    """(number, when, what it wrote to Orion, the trust of what it read)."""
    passes: int = 0
    spent: float = 0.0
    waiting: bool = False
    finished: str | None = None
    """How it ended (done, stopped, out of budget…), once ended."""
    summary: str = ""
    trust: Trust = Trust.SYSTEM
    """The lowest trust of what it was told (its brief, Orion's messages): a brief written
    after reading a page is worth that page."""

    @property
    def thread(self) -> str:
        return f"task:{self.id}"

    def unread(self) -> list[str]:
        return self.said[self.read :]

    def ready(self) -> bool:
        """Whether a pass may run now: running, and not waiting for Orion's answer."""
        return self.finished is None and (not self.waiting or bool(self.unread()))

    def out_of(self, now: datetime) -> str:
        """Which limit it reached, if any."""
        if self.spent >= self.budget_usd:
            return "budget"
        if (now - self.started_at).total_seconds() >= self.minutes * 60:
            return "time"
        if self.passes >= MAX_PASSES:
            return "passes"
        return ""

    def brief_for_pass(self, now: datetime) -> str:
        if self.passes == 0:
            head = (
                f"Tâche de fond ({self.id}) confiée par Orion : {self.goal}\n\n{self.brief}\n\n"
                f"Budget : {self.budget_usd:.2f} $, {self.minutes} min."
            )
        else:
            left = self.minutes - int((now - self.started_at).total_seconds() // 60)
            head = (
                f"Passage n°{self.passes + 1} de ta tâche ({self.id}) : reprends où tu en étais. "
                f"Il reste {max(self.budget_usd - self.spent, 0):.2f} $ et {max(left, 0)} min."
            )
        unread = self.unread()
        if unread:
            head += "\n\nOrion t'a écrit :\n" + "\n".join(f"<orion>\n{t}\n</orion>" for t in unread)
        return head + (
            "\n\nTu travailles pour Orion, pas pour son propriétaire : tu ne lui écris jamais. "
            "Cherche, lis, télécharge (`file` download) : chaque fichier a un identifiant. Puis "
            "réponds à Orion : tes trouvailles, avec liens et identifiants de fichiers, ou une "
            "question si tu ne peux pas avancer sans lui (`task` wait). SILENCE si rien ne "
            "vaut encore un message. Quand c'est fait, `task` done avec un bilan."
        )


@dataclass
class TaskState:
    name: str = "tasks"
    tasks: dict[str, Task] = field(default_factory=dict[str, Task])
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.tasks = {}
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind == STARTED:
            self.tasks[str(payload["task_id"])] = Task(
                id=str(payload["task_id"]),
                goal=str(payload["goal"]),
                brief=str(payload.get("brief", "")),
                budget_usd=float(payload["budget_usd"]),
                minutes=int(payload["minutes"]),
                started_at=datetime.fromisoformat(str(payload["at"])),
                trust=min(Trust.SYSTEM, event.trust),
            )
            return
        task = self.tasks.get(str(payload.get("task_id", "")))
        if task is None:
            return
        if event.kind == SAID:
            task.said.append(str(payload["text"]))
            task.trust = min(task.trust, event.trust)
        elif event.kind == REPORTED:
            at = datetime.fromisoformat(str(payload["at"]))
            task.reports.append(
                (len(task.reports) + 1, at, str(payload["text"]), Trust[str(payload["trust"])])
            )
        elif event.kind == WAITING:
            task.waiting = True
        elif event.kind == PASSED:
            task.passes += 1
            task.spent += float(payload.get("cost_usd", 0.0))
            task.read = int(payload.get("read", task.read))
            task.waiting = bool(payload.get("waiting"))  # what Orion writes next wakes it
        elif event.kind == FINISHED:
            task.finished = str(payload.get("how", "done"))
            task.summary = str(payload.get("summary", ""))


class Tasks:
    def __init__(self, journal: EventStore) -> None:
        self._journal = journal
        self.state = TaskState()
        self._runner = ProjectionRunner(journal, [self.state])
        self.waking: list[Callable[[], None]] = []
        """Told when a task may run now (started, written to): its pass comes at once."""

    async def sync(self) -> None:
        await self._runner.catch_up()

    async def get(self, task_id: str) -> Task | None:
        await self.sync()
        return self.state.tasks.get(task_id)

    async def running(self) -> list[Task]:
        await self.sync()
        return [t for t in self.state.tasks.values() if t.finished is None]

    async def start(
        self,
        goal: str,
        brief: str,
        budget_usd: float,
        minutes: int,
        now: datetime,
        provenance: Provenance,
        correlation_id: str | None,
        trust: Trust = Trust.OWNER,
    ) -> str:
        task_id = f"t_{new_id()}"
        await self._record(
            STARTED,
            {
                "task_id": task_id,
                "goal": goal,
                "brief": brief,
                "budget_usd": budget_usd,
                "minutes": minutes,
                "at": now.isoformat(),
            },
            provenance,
            trust,
            correlation_id,
        )
        self._wake()
        return task_id

    async def say(
        self,
        task_id: str,
        text: str,
        provenance: Provenance,
        trust: Trust,
        correlation_id: str | None,
    ) -> None:
        await self._record(
            SAID, {"task_id": task_id, "text": text}, provenance, trust, correlation_id
        )
        self._wake()

    async def report(self, task: Task, text: str, now: datetime, trust: Trust) -> None:
        await self._record(
            REPORTED,
            {"task_id": task.id, "text": text, "at": now.isoformat(), "trust": trust.name},
            Provenance(source=f"task:{task.id}", actor="orion:tasks"),
            trust,
            None,
        )

    async def wait(self, task_id: str, provenance: Provenance) -> None:
        await self._record(WAITING, {"task_id": task_id}, provenance, Trust.SYSTEM, None)

    async def passed(self, task: Task, cost_usd: float, read: int, waiting: bool) -> None:
        await self._record(
            PASSED,
            {"task_id": task.id, "cost_usd": cost_usd, "read": read, "waiting": waiting},
            Provenance(source=f"task:{task.id}", actor="orion:tasks"),
            Trust.SYSTEM,
            None,
        )

    async def finish(
        self, task_id: str, how: str, summary: str, provenance: Provenance, trust: Trust
    ) -> None:
        await self._record(
            FINISHED, {"task_id": task_id, "how": how, "summary": summary}, provenance, trust, None
        )

    def _wake(self) -> None:
        for tell in self.waking:
            tell()

    async def _record(
        self,
        kind: str,
        payload: dict[str, object],
        provenance: Provenance,
        trust: Trust,
        correlation_id: str | None,
    ) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=provenance,
                    trust=trust,
                    correlation_id=correlation_id,
                )
            ]
        )
        await self._runner.catch_up()
