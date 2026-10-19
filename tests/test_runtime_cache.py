"""The prompt-cache contract: every request must be a strict extension of the previous one.

If any of these tests fails, the provider cache misses on every turn and Orion's cost and
latency multiply. Treat a failure here as a production incident.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler
from orion.context.threads import ThreadProjection
from orion.core.hlc import HybridClock
from orion.core.journal import SqliteJournal
from orion.core.projections import ProjectionRunner
from orion.guardian.policy import Guardian
from orion.runtime import Orion
from orion.tools.base import ToolRegistry
from orion.tools.builtin import builtin_tools
from tests.fakes import ScriptedProvider, call_tools, reply

PARIS = ZoneInfo("Europe/Paris")


def at(minute: int) -> datetime:
    return datetime(2026, 10, 19, 17, minute, tzinfo=PARIS)


def make_orion(journal: SqliteJournal, provider: ScriptedProvider, window: int = 120_000) -> Orion:
    return Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry(builtin_tools()),
        guardian=Guardian(),
        compiler=ContextCompiler("CONSTITUTION", max_window_tokens=window),
        agent=AgentConfig(),
    )


def assert_prefix_stable(provider: ScriptedProvider) -> None:
    for previous, current in zip(provider.requests, provider.requests[1:], strict=False):
        assert current.messages[: len(previous.messages)] == previous.messages
        assert current.tools == previous.tools
        assert current.session_id == previous.session_id


async def test_each_request_extends_the_previous_one(journal: SqliteJournal) -> None:
    provider = ScriptedProvider(
        reply("salut"),
        call_tools(("current_time", {})),
        reply("il est 17h"),
        reply("de rien"),
    )
    orion = make_orion(journal, provider)
    await orion.start()
    await orion.handle_owner_message("t1", "hello", "terminal", now=at(1))
    await orion.handle_owner_message("t1", "quelle heure ?", "terminal", now=at(2))
    await orion.handle_owner_message("t1", "merci", "terminal", now=at(3))

    assert len(provider.requests) == 4
    assert_prefix_stable(provider)
    first = provider.requests[0].messages
    assert first[0] == {"role": "system", "content": "CONSTITUTION"}
    assert first[1]["content"].startswith("[lundi 2026-10-19 17:01 · terminal]\nhello")
    assert provider.requests[0].session_id == "orion:t1"


async def test_thread_survives_restart_byte_for_byte(tmp_path: Path) -> None:
    path = tmp_path / "j.sqlite3"
    provider = ScriptedProvider(reply("un"), reply("deux"))
    async with SqliteJournal(path, HybridClock("a")) as journal:
        orion = make_orion(journal, provider)
        await orion.start()
        await orion.handle_owner_message("t1", "premier", "terminal", now=at(1))

    async with SqliteJournal(path, HybridClock("a")) as journal:
        orion = make_orion(journal, provider)
        await orion.start()
        await orion.handle_owner_message("t1", "second", "terminal", now=at(2))

    assert_prefix_stable(provider)


async def test_rebase_happens_in_one_jump_at_a_user_boundary(journal: SqliteJournal) -> None:
    provider = ScriptedProvider(*[reply("r" * 900) for _ in range(12)])
    orion = make_orion(journal, provider, window=4_000)
    await orion.start()
    for i in range(12):
        await orion.handle_owner_message("t1", f"message {i} " + "x" * 900, "terminal", now=at(i))

    rebases = [
        i
        for i, (prev, cur) in enumerate(zip(provider.requests, provider.requests[1:], strict=False))
        if cur.messages[: len(prev.messages)] != prev.messages
    ]
    assert rebases, "the window should have been rebased at least once"
    assert len(rebases) < len(provider.requests) // 2, "rebasing must not happen on every turn"
    for request in provider.requests:
        assert request.messages[0]["role"] == "system"
        assert request.messages[1]["role"] == "user"  # never starts mid tool exchange

    # Rebuilding the projection from the journal gives the same window.
    rebuilt = ThreadProjection()
    await ProjectionRunner(journal, [rebuilt]).catch_up()
    assert rebuilt.get("t1").window() == orion.threads.get("t1").window()


async def test_turn_is_fully_journaled(journal: SqliteJournal) -> None:
    provider = ScriptedProvider(call_tools(("calculate", {"expression": "2+2"})), reply("4"))
    orion = make_orion(journal, provider)
    await orion.start()
    result = await orion.handle_owner_message("t1", "2+2 ?", "terminal", now=at(1))

    events = await journal.read()
    kinds = [e.kind for e in events]
    assert kinds[0] == "message.received"
    assert kinds.count("model.usage") == 2
    assert kinds.count("tool.executed") == 1
    assert kinds[-1] == "turn.finished"
    assert {e.correlation_id for e in events} == {result.correlation_id}
    assert events[0].provenance.actor == "owner:main"
