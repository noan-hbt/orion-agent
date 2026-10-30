"""Background tasks (D110): a helper works alone and reports to Orion, never to the owner."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.guardian.policy import ActionProposal, Guardian, Verdict
from orion.runtime import Orion
from orion.tasks.module import TaskWork, task_tool
from orion.tasks.service import Tasks
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolRegistry, ToolResult
from tests.fakes import ScriptedProvider, call_tools, reply

PARIS = ZoneInfo("Europe/Paris")
OWNER = ToolContext("owner", "c0")


class World:
    def __init__(self, journal: SqliteJournal, provider: ScriptedProvider) -> None:
        self.now = datetime(2026, 10, 22, 14, 0, tzinfo=PARIS)
        self.tasks = Tasks(journal)
        self.tool = task_tool(self.tasks, lambda: self.now, max_running=2)
        self.orion = Orion(
            journal=journal,
            provider=provider,
            registry=ToolRegistry([self.tool]),
            guardian=Guardian(),
            compiler=ContextCompiler("C"),
            agent=AgentConfig(),
        )
        self.work = TaskWork(self.tasks, self.orion, lambda: self.now)

    async def use(self, ctx: ToolContext, **args: object) -> ToolResult:
        return await self.tool.handler(self.tool.parse(json.dumps(args)), ctx)

    async def passes(self) -> None:
        await self.work.start_ready()
        await self.work.settle()


def task(action: str, **args: object) -> tuple[str, dict[str, object]]:
    return ("task", {"action": action, **args})


async def test_a_task_works_on_alone_and_writes_to_orion_only(journal: SqliteJournal) -> None:
    provider = ScriptedProvider()
    world = World(journal, provider)
    started = await world.use(
        OWNER, action="start", goal="Des colliers dorés", brief="Sobres, 150-300 €, photos."
    )
    [running] = await world.tasks.running()
    assert running.id in started.content
    assert world.work._woken.is_set()  # pyright: ignore[reportPrivateUsage]  # it starts now

    provider.extend(reply("Deux colliers : Agatha 180 € (fi_1), Cleor 169 €."))
    await world.passes()
    brief = str(provider.requests[0].messages[-1]["content"])
    assert "Sobres, 150-300 €" in brief and "tu ne lui écris jamais" in brief
    assert provider.requests[0].session_id == f"orion:task:{running.id}"  # its own thread
    [alert] = await world.work.alerts(world.now)
    assert alert.spoken_to and "Agatha 180 €" in alert.notice and "`task` (say)" in alert.notice

    # Unasked, it goes on with a second pass, and ends itself.
    provider.extend(
        call_tools(task("done", summary="4 colliers trouvés")), reply("Fini : 4 colliers.")
    )
    await world.passes()
    assert "Passage n°2" in str(provider.requests[1].messages[-1]["content"])
    assert await world.tasks.running() == []
    alerts = await world.work.alerts(world.now)
    assert len(alerts) == 2 and "4 colliers trouvés" in alerts[1].notice  # its summary, once
    await world.passes()  # nothing runs once done
    assert len(provider.requests) == 3


async def test_a_task_waits_for_orion_and_orion_steers_it(journal: SqliteJournal) -> None:
    provider = ScriptedProvider()
    world = World(journal, provider)
    await world.use(OWNER, action="start", goal="Un hôtel à Lyon", brief="Pour samedi soir.")
    [running] = await world.tasks.running()
    provider.extend(call_tools(task("wait")), reply("Quel budget maximum ?"))
    await world.passes()
    await world.passes()  # it waits: no pass until Orion answers
    assert len(provider.requests) == 2

    told = await world.use(OWNER, action="say", task=running.id, text="120 € la nuit, max")
    assert not told.is_error
    provider.extend(reply("Trois hôtels sous 120 €."))
    await world.passes()
    assert "<orion>\n120 € la nuit, max\n</orion>" in str(
        provider.requests[-1].messages[-1]["content"]
    )

    # He no longer wants it: stopped, it does nothing more.
    await world.use(OWNER, action="stop", task=running.id, why="il a trouvé")
    await world.passes()
    assert len(provider.requests) == 3


async def test_a_task_ends_at_its_limits_and_says_so(journal: SqliteJournal) -> None:
    provider = ScriptedProvider()
    world = World(journal, provider)
    await world.use(OWNER, action="start", goal="Comparer des forfaits", brief="Mobile, < 15 €.")
    provider.extend(reply("Premier tri : Free, Sosh.", cost=1.5))  # beyond its 1 $
    await world.passes()
    assert await world.tasks.running() == []
    notices = [a.notice for a in await world.work.alerts(world.now)]
    assert any("son budget est épuisé" in n for n in notices)

    await world.use(OWNER, action="start", goal="Guetter un billet", brief="Paris-Lyon.", minutes=5)
    world.now += timedelta(minutes=6)
    await world.passes()
    assert await world.tasks.running() == []
    assert len(provider.requests) == 1  # out of time before it began again


class _Args(BaseModel):
    action: str = ""


def _tool(name: str, effect: Effect) -> Tool:
    async def run(_args: Any, _ctx: ToolContext) -> ToolResult:
        return ToolResult("ok")

    return Tool(
        name=name,
        description="x",
        input_model=_Args,
        handler=run,
        effect=effect,
        reversibility=Reversibility.REVERSIBLE,
        accepts_untrusted_context=True,
    )


async def test_a_task_only_looks_things_up(journal: SqliteJournal) -> None:
    guardian = Guardian()
    inside = "task:t_1"

    async def verdict(name: str, effect: Effect, action: str = "", thread: str = inside) -> Verdict:
        proposal = ActionProposal(_tool(name, effect), _Args(action=action), Trust.SYSTEM, thread)
        return (await guardian.decide(proposal)).verdict

    assert await verdict("web_search", Effect.NONE) is Verdict.ALLOW
    assert await verdict("mail", Effect.NONE) is Verdict.ALLOW  # every read, as Orion has
    assert await verdict("conversations_search", Effect.NONE) is Verdict.ALLOW
    assert await verdict("mail_send", Effect.EXTERNAL) is Verdict.DENY
    assert await verdict("file", Effect.INTERNAL, "download") is Verdict.ALLOW
    assert await verdict("file", Effect.INTERNAL, "send") is Verdict.DENY  # nothing to him
    assert await verdict("remind_me", Effect.INTERNAL) is Verdict.DENY
    assert await verdict("peer_message", Effect.EXTERNAL) is Verdict.DENY
    assert await verdict("browser", Effect.NONE, "open") is Verdict.ALLOW
    assert await verdict("browser", Effect.EXTERNAL, "send") is Verdict.DENY  # Orion sends
    assert await verdict("browser", Effect.EXTERNAL, "login") is Verdict.DENY
    assert await verdict("remind_me", Effect.INTERNAL, thread="owner") is Verdict.ALLOW


async def test_a_task_briefed_after_reading_a_page_is_worth_that_page(
    journal: SqliteJournal,
) -> None:
    provider = ScriptedProvider()
    world = World(journal, provider)
    after_the_web = ToolContext("owner", "c1", context_trust=Trust.WEB)
    await world.use(after_the_web, action="start", goal="Comparer", brief="Ce que dit la page.")
    [running] = await world.tasks.running()
    assert running.trust is Trust.WEB
    provider.extend(reply("Rien de plus."))
    await world.passes()
    received = [e for e in await journal.read() if e.kind == "initiative.triggered"]
    assert received and received[-1].trust is Trust.WEB  # its pass reads at the page's trust


async def test_tasks_started_at_once_keep_to_the_limit(journal: SqliteJournal) -> None:
    import asyncio

    world = World(journal, ScriptedProvider())
    started = await asyncio.gather(
        *(
            world.use(OWNER, action="start", goal=f"Recherche {i}", brief="Un brief complet.")
            for i in range(4)
        )
    )
    assert len(await world.tasks.running()) == 2
    assert sum(r.is_error for r in started) == 2
