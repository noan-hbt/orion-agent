"""The prompt-cache contract: every request must be a strict extension of the previous one.

If any of these tests fails, the provider cache misses on every turn and Orion's cost and
latency multiply. Treat a failure here as a production incident.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler
from orion.context.threads import MESSAGE_KIND, ThreadProjection
from orion.core.hlc import HybridClock
from orion.core.journal import SqliteJournal
from orion.core.projections import ProjectionRunner
from orion.guardian.policy import Guardian
from orion.providers.base import ModelRequest, ModelResponse
from orion.runtime import Orion
from orion.tools.base import ToolRegistry
from orion.tools.builtin import builtin_tools
from tests.fakes import ScriptedProvider, call_tools, echo_tool, reply

PARIS = ZoneInfo("Europe/Paris")


def at(minute: int) -> datetime:
    return datetime(2026, 10, 19, 17, minute, tzinfo=PARIS)


def make_orion(journal: SqliteJournal, provider: ScriptedProvider, window: int = 120_000) -> Orion:
    return Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry([*builtin_tools(), echo_tool()]),
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
    provider = ScriptedProvider(call_tools(("echo", {"text": "4"})), reply("4"))
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


async def test_untrusted_content_leaves_the_window_after_a_pause(journal: SqliteJournal) -> None:
    from datetime import timedelta

    from orion.core.events import Trust

    provider = ScriptedProvider(reply("Paul t'a écrit."), reply("ok"), reply("ok"), reply("salut"))
    orion = make_orion(journal, provider)
    await orion.start()
    await orion.handle_initiative(
        "owner", "Mail de Paul : …", "nouveau mail", Trust.THIRD_PARTY, now=at(0)
    )
    await orion.handle_owner_message("owner", "merci", "imessage", now=at(30))
    thread = orion.threads.get("owner")
    assert thread.window_trust() is Trust.THIRD_PARTY and thread.base == 0  # still talking

    await orion.handle_owner_message("owner", "re", "imessage", now=at(31) + timedelta(hours=2))
    thread = orion.threads.get("owner")
    assert thread.window_trust() is Trust.SYSTEM  # started afresh after the pause: clean
    assert [m["role"] for m in thread.window()] == ["user", "assistant"]
    rebased = [e for e in await journal.read() if e.kind == "thread.rebased"]
    assert len(rebased) == 1


class _Summarizing(ScriptedProvider):
    """Answers summary requests itself; the conversation's requests follow the script."""

    def __init__(self, *script: Any) -> None:
        super().__init__(*script)
        self.summaries: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.model == "summary-model":
            self.summaries.append(request)
            return reply("Tu cherchais un T2 à Lyon ; il attend ta réponse sur le n°3.")(request)
        return await super().complete(request)


async def test_a_rebase_carries_a_summary_of_what_it_drops(journal: SqliteJournal) -> None:
    provider = _Summarizing(*[reply("r" * 900) for _ in range(12)])
    orion = make_orion(journal, provider, window=4_000)
    orion._summary_model = "summary-model"  # pyright: ignore[reportPrivateUsage]
    await orion.start()
    for i in range(12):
        await orion.handle_owner_message("t1", f"message {i} " + "x" * 900, "terminal", now=at(i))

    assert provider.summaries, "a rebase happened, so what it dropped was summarized"
    assert "Personne : " in str(provider.summaries[0].messages[1]["content"])
    # The summary rides in the message that opened the new window, after the turns kept.
    carried = [
        str(m["content"])
        for m in provider.requests[-1].messages
        if "<plus-tot-dans-la-conversation>" in str(m["content"])
    ]
    assert carried and "T2 à Lyon" in carried[0]
    assert_prefix_stable_between_rebases(provider)


def assert_prefix_stable_between_rebases(provider: ScriptedProvider) -> None:
    jumps = 0
    for previous, current in zip(provider.requests, provider.requests[1:], strict=False):
        if current.messages[: len(previous.messages)] != previous.messages:
            jumps += 1
    assert jumps < len(provider.requests) // 2


async def test_untrusted_turns_are_not_summarized(journal: SqliteJournal) -> None:
    from orion.core.events import Trust

    provider = _Summarizing(*[reply("r" * 900) for _ in range(9)])
    orion = make_orion(journal, provider, window=4_000)
    orion._summary_model = "summary-model"  # pyright: ignore[reportPrivateUsage]
    await orion.start()
    await orion.handle_initiative("t1", "Page : " + "y" * 900, "web", Trust.WEB, now=at(0))
    for i in range(1, 9):
        await orion.handle_owner_message("t1", f"message {i} " + "x" * 900, "terminal", now=at(i))
    assert [e for e in await journal.read() if e.kind == "thread.rebased"]
    assert not provider.summaries


async def test_old_bulky_results_are_shortened_only_after_a_pause(journal: SqliteJournal) -> None:
    """Like a rebase, pruning changes the prefix once; it waits until the cache is gone."""
    page = "annonce " * 600
    provider = ScriptedProvider(
        call_tools(("echo", {"text": page})),
        reply("trois annonces"),
        reply("ok"),
        reply("ok"),
        reply("ok"),
    )
    orion = make_orion(journal, provider)
    await orion.start()
    for minute in (0, 1, 2):  # a lively conversation: nothing changes under the cache
        await orion.handle_owner_message("t1", f"m{minute}", "terminal", now=at(minute))
    assert_prefix_stable(provider)
    assert page in str(provider.requests[-1].messages)

    await orion.handle_owner_message("t1", "re", "terminal", now=at(20))  # after a pause
    shown = [str(m["content"]) for m in provider.requests[-1].messages if m["role"] == "tool"]
    assert len(shown) == 1 and "ancien résultat allégé" in shown[0] and len(shown[0]) < 800
    # The journal keeps the page whole, and the projection rebuilds the same window.
    assert any(page in str(e.payload) for e in await journal.read() if e.kind == MESSAGE_KIND)
    rebuilt = ThreadProjection()
    await ProjectionRunner(journal, [rebuilt]).catch_up()
    assert rebuilt.get("t1").window() == orion.threads.get("t1").window()


async def test_memory_gets_a_last_look_at_what_a_rebase_drops(journal: SqliteJournal) -> None:
    """OpenClaw's pre-compaction flush: before turns leave the window, the extractor reads
    them once more (whatever happened to the turn itself)."""
    from datetime import date

    from orion.memory.extractor import ExtractionReport, MemoryExtractor
    from orion.memory.service import MemoryService, Origin
    from orion.memory.store import Fact, MemoryStore

    seen: list[tuple[str, str]] = []

    class Extractor(MemoryExtractor):
        async def consolidate(
            self,
            owner_text: str,
            reply_text: str,
            origin: Origin,
            today: date,
            recorded_this_turn: list[Fact],
        ) -> ExtractionReport:
            seen.append((owner_text, reply_text))
            return ExtractionReport()

    store = MemoryStore(":memory:")
    await store.open()
    memory = MemoryService(journal, store)
    provider = ScriptedProvider(*[reply("r" * 900) for _ in range(12)])
    orion = Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry([*builtin_tools(), echo_tool()]),
        guardian=Guardian(),
        compiler=ContextCompiler("CONSTITUTION", max_window_tokens=4_000),
        agent=AgentConfig(),
        memory=memory,
        extractor=Extractor(provider, memory, "fake"),
    )
    await orion.start()
    turns = [
        await orion.handle_owner_message("t1", f"message {i} " + "x" * 900, "terminal", now=at(i))
        for i in range(12)
    ]
    leaving = [t for t in turns if t.leaving is not None]
    assert leaving, "a rebase happened"
    await orion.consolidate(leaving[0])
    flushed_owner, flushed_orion = seen[0]
    assert "message 0" in flushed_owner and "r" * 50 in flushed_orion
    await store.close()


async def test_what_the_owner_said_in_a_turn_stopped_by_a_limit_is_remembered(
    journal: SqliteJournal,
) -> None:
    """A turn that ran out of steps still answered him: his words are extracted. A turn that
    failed (provider error) was never answered, so nothing is taken from it."""
    from datetime import date

    from orion.cognition.loop import StopReason
    from orion.memory.extractor import ExtractionReport, MemoryExtractor
    from orion.memory.service import MemoryService
    from orion.memory.store import Fact, MemoryStore
    from orion.providers.base import Usage
    from orion.runtime import TurnResult

    seen: list[str] = []

    class Extractor(MemoryExtractor):
        async def consolidate(
            self,
            owner_text: str,
            reply_text: str,
            origin: Any,
            today: date,
            recorded_this_turn: list[Fact],
        ) -> ExtractionReport:
            seen.append(owner_text)
            return ExtractionReport()

    store = MemoryStore(":memory:")
    await store.open()
    memory = MemoryService(journal, store)
    orion = Orion(
        journal=journal,
        provider=ScriptedProvider(reply("unused")),
        registry=ToolRegistry([*builtin_tools(), echo_tool()]),
        guardian=Guardian(),
        compiler=ContextCompiler("CONSTITUTION", max_window_tokens=4_000),
        agent=AgentConfig(),
        memory=memory,
        extractor=Extractor(ScriptedProvider(reply("unused")), memory, "fake"),
    )

    def turn(reason: StopReason, said: str) -> TurnResult:
        return TurnResult(
            text="Je m'arrête là.",
            reason=reason,
            usage=Usage(),
            correlation_id="c1",
            owner_text=said,
        )

    await orion.consolidate(turn(StopReason.MAX_STEPS, "je m'appelle Paul"))
    assert seen == ["je m'appelle Paul"]
    await orion.consolidate(turn(StopReason.PROVIDER_ERROR, "j'habite à Dreux"))
    assert seen == ["je m'appelle Paul"]  # never answered: nothing taken from it
    await store.close()
