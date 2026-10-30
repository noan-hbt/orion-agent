from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.guardian.policy import Guardian
from orion.memory.model import Category
from orion.memory.service import MemoryService, Origin
from orion.memory.store import MemoryStore
from orion.memory.tools import memory_tools
from orion.runtime import Orion
from orion.tools.base import ToolRegistry
from orion.tools.builtin import builtin_tools
from tests.fakes import ScriptedProvider, call_tools, reply
from tests.test_runtime_cache import assert_prefix_stable

PARIS = ZoneInfo("Europe/Paris")
OWNER = Origin(actor="orion:core", source="terminal:t1", trust=Trust.OWNER)


def at(minute: int) -> datetime:
    return datetime(2026, 10, 19, 18, minute, tzinfo=PARIS)


@pytest.fixture
async def memory(journal: SqliteJournal) -> AsyncIterator[MemoryService]:
    store = MemoryStore(":memory:")
    await store.open()
    yield MemoryService(journal, store)
    await store.close()


def make_orion(
    journal: SqliteJournal,
    memory: MemoryService,
    provider: ScriptedProvider,
    agent: AgentConfig | None = None,
) -> Orion:
    return Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry([*builtin_tools(), *memory_tools(memory)]),
        guardian=Guardian(),
        compiler=ContextCompiler("CONSTITUTION"),
        agent=agent or AgentConfig(),
        memory=memory,
    )


async def test_core_memory_opens_the_window_and_relevant_facts_follow(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    await memory.remember("Noan est végétarien.", OWNER, category=Category.PREFERENCE)
    await memory.remember(
        "Paul est le comptable de Noan.", OWNER, about=["Paul"], category=Category.PERSON
    )
    provider = ScriptedProvider(reply("ok"), reply("ok"), reply("ok"))
    orion = make_orion(journal, memory, provider)
    await orion.start()

    await orion.handle_owner_message("t1", "salut", "terminal", now=at(1))
    await orion.handle_owner_message("t1", "il faut que je rappelle Paul", "terminal", now=at(2))
    await orion.handle_owner_message("t1", "Paul encore lui", "terminal", now=at(3))

    first, second, third = (r.messages[-1]["content"] for r in provider.requests)
    assert "<memoire>" in first and "végétarien" in first
    assert "<memoire>" not in second
    assert "<souvenirs>" in second and "comptable" in second
    assert "comptable" not in third  # already visible in the window: not repeated
    assert_prefix_stable(provider)


async def test_core_memory_comes_back_when_a_pause_cuts_the_window(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    """The next morning the window keeps only the last exchanges (when `fresh_idle_minutes` is
    set; off by default since D200): what opened it yesterday (portrait, his preferences) must
    open it again, or Orion forgets how to talk to him."""
    from datetime import timedelta

    await memory.remember(
        "Noan veut des réponses très courtes.", OWNER, category=Category.PREFERENCE
    )
    provider = ScriptedProvider(*[reply("r" * 3000) for _ in range(11)])
    orion = make_orion(journal, memory, provider, AgentConfig(fresh_idle_minutes=60))
    await orion.start()
    for i in range(10):
        await orion.handle_owner_message("t1", f"message {i} " + "x" * 3000, "terminal", now=at(i))

    await orion.handle_owner_message("t1", "salut", "terminal", now=at(10) + timedelta(hours=9))
    sent = provider.requests[-1].messages
    assert "message 0" not in str(sent)  # the window was cut
    assert "<memoire>" in str(sent[-1]["content"]) and "très courtes" in str(sent[-1]["content"])


async def test_his_instructions_come_back_as_a_long_conversation_drifts(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    """Told once to answer in a few words, Orion slid back to long replies as the instruction
    fell far behind in the window: it must be shown again, without breaking the cache."""
    await memory.remember(
        "Noan veut des réponses de 1 à 5 mots.", OWNER, category=Category.PREFERENCE
    )
    provider = ScriptedProvider(*[reply("r" * 2000) for _ in range(12)])
    orion = make_orion(journal, memory, provider)
    await orion.start()
    for i in range(12):
        await orion.handle_owner_message("t1", f"message {i} " + "x" * 2000, "terminal", now=at(i))

    contents = [str(r.messages[-1]["content"]) for r in provider.requests]
    reminded = [c for c in contents[1:] if "<consignes>" in c]
    assert reminded and "1 à 5 mots" in reminded[0]
    assert len(reminded) < len(contents) - 1  # not on every message
    assert_prefix_stable(provider)


async def test_his_instructions_come_back_in_a_short_conversation_too(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    """With short replies, 6k tokens took dozens of exchanges: the drift started long before
    the reminder came (D184)."""
    await memory.remember(
        "Noan veut des réponses de 1 à 5 mots.", OWNER, category=Category.PREFERENCE
    )
    provider = ScriptedProvider(*[reply("ok") for _ in range(8)])
    orion = make_orion(journal, memory, provider)
    await orion.start()
    for i in range(8):
        await orion.handle_owner_message("t1", f"message {i}", "terminal", now=at(i))

    contents = [str(r.messages[-1]["content"]) for r in provider.requests]
    assert any("<consignes>" in c and "1 à 5 mots" in c for c in contents[1:])
    assert_prefix_stable(provider)


async def test_a_long_playbook_opens_the_window_whole(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    """With 36 rules, the newest (his style) fell past the size cap of <memoire> and came back
    in <manuel> as changes to mention, not as his instructions (D185)."""
    for i in range(30):
        await memory.learn_rule(f"Règle numéro {i} : " + "détail " * 20, OWNER)
    await memory.learn_rule("Réponds en 1 à 5 mots.", OWNER)
    provider = ScriptedProvider(reply("ok"))
    orion = make_orion(journal, memory, provider)
    await orion.start()
    await orion.handle_owner_message("t1", "salut", "terminal", now=at(0))

    first = str(provider.requests[0].messages[-1]["content"])
    memoire = first[first.index("<memoire>") : first.index("</memoire>")]
    assert "1 à 5 mots" in memoire and "<manuel>" not in first


async def test_remember_tool_writes_memory_derived_from_the_owner_message(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    provider = ScriptedProvider(
        call_tools(
            ("remember", {"statement": "Noan court le dimanche.", "category": "preference"})
        ),
        reply("Noté."),
    )
    orion = make_orion(journal, memory, provider)
    await orion.start()
    turn = await orion.handle_owner_message("t1", "je cours le dimanche", "terminal", now=at(1))

    [fact] = await memory.store.facts()
    assert fact.statement == "Noan court le dimanche."
    events = await journal.read()
    asserted = next(e for e in events if e.kind == "memory.fact.asserted")
    assert asserted.provenance.derived_from == (turn.origin_event_id,)
    assert asserted.correlation_id == turn.correlation_id


async def test_forgetting_a_conversation_erases_its_memories(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    provider = ScriptedProvider(
        call_tools(("remember", {"statement": "Le code de la porte est 4512."})),
        reply("Noté."),
    )
    orion = make_orion(journal, memory, provider)
    await orion.start()
    await orion.handle_owner_message("t1", "le code de la porte c'est 4512", "terminal", now=at(1))

    report = await orion.forget_source("terminal:t1", reason="oops")
    assert report.verified and report.facts_removed == 1
    remaining = [e for e in await journal.read() if not e.redacted]
    assert not any("4512" in str(e.payload) for e in remaining)
    # The live conversation no longer carries it either: the next request is clean.
    provider.extend(reply("ok"))
    await orion.handle_owner_message("t1", "on en était où ?", "terminal", now=at(2))
    assert "4512" not in str(provider.requests[-1].messages)
