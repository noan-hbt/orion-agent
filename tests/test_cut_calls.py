"""ORI-007: a call cut by a crash is UNCERTAIN, told once, never replayed."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler
from orion.core.journal import SqliteJournal
from orion.guardian.policy import ActionProposal, Approval, Guardian
from orion.runtime import OWNER_THREAD, Orion
from orion.serve import CutCallReports
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolRegistry, ToolResult
from tests.fakes import ScriptedProvider, call_tools, reply


class Yes:
    async def approve(self, proposal: ActionProposal) -> Approval:
        return Approval(True)


class Booking(BaseModel):
    what: str


def _orion(journal: SqliteJournal, tool: Tool, provider: ScriptedProvider) -> Orion:
    return Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry([tool]),
        guardian=Guardian(Yes()),
        compiler=ContextCompiler("CONSTITUTION"),
        agent=AgentConfig(),
    )


async def test_a_call_cut_by_a_crash_is_uncertain_told_once_and_never_redone(
    journal: SqliteJournal,
) -> None:
    runs: list[ToolContext] = []
    running = asyncio.Event()

    async def book(_args: Any, ctx: ToolContext) -> ToolResult:
        runs.append(ctx)
        running.set()
        await asyncio.Event().wait()  # the process dies here
        raise AssertionError("never")

    tool = Tool(
        name="book",
        description="Books it.",
        input_model=Booking,
        handler=book,
        effect=Effect.NONE,
        reversibility=Reversibility.REVERSIBLE,
        acts=True,
    )
    first = _orion(journal, tool, ScriptedProvider(call_tools(("book", {"what": "table"}))))
    await first.start()
    turn = asyncio.create_task(first.handle_owner_message(OWNER_THREAD, "Réserve.", "t"))
    async with asyncio.timeout(10):
        await running.wait()
    turn.cancel()  # the crash: no `tool.executed`

    again = _orion(journal, tool, ScriptedProvider(reply("ok")))
    await again.start()
    settled = await again.settle_cut_calls()
    assert [c.tool for c in settled] == ["book"]
    executed = [e for e in await journal.read(0, 10_000) if e.kind == "tool.executed"]
    assert [e.payload["outcome"] for e in executed] == ["UNCERTAIN"]
    assert len(runs) == 1  # nothing replayed

    told = await CutCallReports(again).reports()
    assert len(told) == 1 and "book" in told[0].notice
    assert await again.settle_cut_calls() == []  # a second restart settles nothing more
    assert len(await CutCallReports(again).reports()) == 1  # same key: said once


async def test_the_same_call_asked_twice_carries_the_same_idempotency_key(
    journal: SqliteJournal,
) -> None:
    keys: list[str] = []

    async def book(args: Any, ctx: ToolContext) -> ToolResult:
        keys.append(ctx.idempotency_key)
        return ToolResult("fait")

    tool = Tool(
        name="book",
        description="Books it.",
        input_model=Booking,
        handler=book,
        effect=Effect.NONE,
        reversibility=Reversibility.REVERSIBLE,
    )
    provider = ScriptedProvider(
        call_tools(("book", {"what": "table"})),
        call_tools(("book", {"what": "table"}), ("book", {"what": "taxi"})),
        reply("Réservé."),
    )
    orion = _orion(journal, tool, provider)
    await orion.start()
    await orion.handle_owner_message(OWNER_THREAD, "Réserve.", "t")
    assert len(keys) == 3 and keys[0] == keys[1] != keys[2]
