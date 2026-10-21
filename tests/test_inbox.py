from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import structlog.testing
from pydantic import BaseModel

from orion.channels.inbox import Inbound, OwnerInbox
from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.guardian.policy import ActionProposal, Approval, Guardian
from orion.runtime import Orion
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolRegistry, ToolResult
from tests.fakes import ScriptedProvider, call_tools, reply
from tests.test_runtime_cache import assert_prefix_stable


class Outlet:
    def __init__(self, name: str, up: bool = True) -> None:
        self.name = name
        self.up = up
        self.sent: list[str] = []
        self.shown: list[str] = []

    async def available(self) -> bool:
        return self.up

    async def deliver(self, text: str, correlation_id: str | None) -> None:
        self.sent.append(text)

    @asynccontextmanager
    async def working(self, item: Inbound) -> AsyncGenerator[None]:
        self.shown.append(item.text)
        yield


async def until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not condition():  # noqa: ASYNC110 - polling plain test fakes
            await asyncio.sleep(0.01)


@pytest.fixture
def provider() -> ScriptedProvider:
    return ScriptedProvider()


@pytest.fixture
async def inbox(journal: SqliteJournal, provider: ScriptedProvider) -> AsyncIterator[OwnerInbox]:
    orion = Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry([]),
        guardian=Guardian(),
        compiler=ContextCompiler("C"),
        agent=AgentConfig(),
    )
    inbox = OwnerInbox(orion, coalesce_seconds=0.05)
    worker = asyncio.create_task(inbox.run())
    yield inbox
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker


async def test_bursts_coalesce_per_channel_and_answer_where_they_came_from(
    inbox: OwnerInbox, provider: ScriptedProvider
) -> None:
    imessage, tui = Outlet("imessage"), Outlet("tui")
    inbox.register(imessage)
    inbox.register(tui)
    provider.extend(reply("pour iMessage"), reply("pour la TUI"))
    inbox.submit(Inbound("imessage", ("a",), "un"))
    inbox.submit(Inbound("imessage", ("b",), "deux"))
    inbox.submit(Inbound("tui", ("c",), "trois"))
    await until(lambda: bool(tui.sent))
    assert imessage.sent == ["pour iMessage"] and tui.sent == ["pour la TUI"]
    assert provider.requests[0].messages[-1]["content"].endswith("un\ndeux")
    assert re.search(r"· tui[] ·]", provider.requests[1].messages[-1]["content"])
    assert imessage.shown == ["deux"]


async def test_call_transcripts_may_need_no_answer(
    inbox: OwnerInbox, provider: ScriptedProvider
) -> None:
    imessage = Outlet("imessage")
    inbox.register(imessage)
    provider.extend(reply("SILENCE"), reply("J'ai réservé pour jeudi, comme dit au tel."))
    inbox.submit(
        Inbound("appel", ("call-1",), "transcription…", reply_via="imessage", allow_silence=True)
    )
    inbox.submit(
        Inbound("appel", ("call-2",), "transcription…", reply_via="imessage", allow_silence=True)
    )
    await until(lambda: bool(imessage.sent))
    assert imessage.sent == ["J'ai réservé pour jeudi, comme dit au tel."]
    assert len(provider.requests) == 2  # transcripts are never merged together


async def test_silence_is_never_sent_as_a_word(
    inbox: OwnerInbox, provider: ScriptedProvider
) -> None:
    """After a voice message, Orion has nothing to add: he must not read « SILENCE »."""
    imessage = Outlet("imessage")
    inbox.register(imessage)
    provider.extend(reply("SILENCE."), reply("Et voilà."))
    inbox.submit(Inbound("imessage", ("a",), "envoie-moi un vocal"))
    await until(lambda: len(provider.requests) == 1)
    inbox.submit(Inbound("imessage", ("b",), "merci"))
    await until(lambda: bool(imessage.sent))
    assert imessage.sent == ["Et voilà."]


async def test_proactive_messages_take_the_first_available_outlet(inbox: OwnerInbox) -> None:
    imessage, tui = Outlet("imessage", up=False), Outlet("tui")
    inbox.register(imessage)
    inbox.register(tui)
    await inbox.notify("coucou", "c1")
    assert tui.sent == ["coucou"] and imessage.sent == []
    tui.up = False
    assert not await inbox.available()


async def test_each_turn_logs_where_the_time_went(
    inbox: OwnerInbox, provider: ScriptedProvider
) -> None:
    imessage = Outlet("imessage")
    inbox.register(imessage)
    provider.extend(reply("ok"))
    with structlog.testing.capture_logs() as logs:
        inbox.submit(Inbound("imessage", ("a",), "salut"))
        await until(lambda: bool(imessage.sent))
        await until(lambda: any(e["event"] == "inbox.turn" for e in logs))
    [turn] = [e for e in logs if e["event"] == "inbox.turn"]
    assert turn["messages"] == 1 and turn["coalesce_s"] >= 0.05
    assert {"think_s", "send_s", "cached_tokens", "cost_usd"} <= turn.keys()


class _Wait(BaseModel):
    pass


async def test_a_message_sent_while_orion_works_joins_the_running_turn(
    journal: SqliteJournal, provider: ScriptedProvider
) -> None:
    holder: list[OwnerInbox] = []

    async def lookup(_args: object, _ctx: ToolContext) -> ToolResult:
        # The owner writes again while the tool runs, then again on another channel.
        holder[0].submit(Inbound("imessage", ("b",), "non laisse tomber"))
        holder[0].submit(Inbound("tui", ("c",), "autre chose"))
        return ToolResult("Paul : +33 6 00 00 00 00")

    tool = Tool(
        name="lookup",
        description="Look someone up.",
        input_model=_Wait,
        handler=lookup,
        effect=Effect.NONE,
        reversibility=Reversibility.REVERSIBLE,
    )
    orion = Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry([tool]),
        guardian=Guardian(),
        compiler=ContextCompiler("C"),
        agent=AgentConfig(),
    )
    inbox = OwnerInbox(orion, coalesce_seconds=0.05)
    holder.append(inbox)
    imessage, tui = Outlet("imessage"), Outlet("tui")
    inbox.register(imessage)
    inbox.register(tui)
    provider.extend(
        call_tools(("lookup", {})), reply("Ok, j'appelle pas Paul."), reply("Pour la TUI.")
    )
    worker = asyncio.create_task(inbox.run())
    try:
        inbox.submit(Inbound("imessage", ("a",), "appelle Paul"))
        await until(lambda: bool(tui.sent))
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    # One turn for both iMessage texts, one answer; the TUI message got its own turn.
    assert imessage.sent == ["Ok, j'appelle pas Paul."] and tui.sent == ["Pour la TUI."]
    second = provider.requests[1].messages
    assert second[-2]["role"] == "tool"
    assert second[-1]["role"] == "user" and "non laisse tomber" in second[-1]["content"]
    assert "autre chose" not in str(second)
    received = [e for e in await journal.read() if e.kind == "message.received"]
    assert [e.payload.get("refs") for e in received] == [["a"], ["b"], ["c"]]
    assert received[1].payload["during_turn"] is True
    assert received[1].correlation_id == received[0].correlation_id
    assert_prefix_stable(provider)


async def test_orion_can_say_a_word_before_it_has_finished(
    journal: SqliteJournal, provider: ScriptedProvider
) -> None:
    """What Orion writes beside a tool call goes out at once; the answer follows."""
    from orion.tools.builtin import builtin_tools

    orion = Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry(builtin_tools()),
        guardian=Guardian(),
        compiler=ContextCompiler("C"),
        agent=AgentConfig(),
    )
    inbox = OwnerInbox(orion, coalesce_seconds=0.01)
    imessage = Outlet("imessage")
    inbox.register(imessage)
    provider.extend(
        call_tools(("current_time", {}), text="Je regarde l'heure."),
        call_tools(("current_time", {})),  # nothing written beside it: nothing sent
        reply("Il est 17h."),
    )
    worker = asyncio.create_task(inbox.run())
    inbox.submit(Inbound("imessage", ("a",), "il est quelle heure ?"))
    await until(lambda: len(imessage.sent) == 2)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert imessage.sent == ["Je regarde l'heure.", "Il est 17h."]
    # Sent once, kept once: the conversation replays it as the model wrote it.
    assert_prefix_stable(provider)


async def test_a_turn_that_may_stay_silent_says_nothing_midway(
    inbox: OwnerInbox, provider: ScriptedProvider
) -> None:
    imessage = Outlet("imessage")
    inbox.register(imessage)
    provider.extend(call_tools(("nope", {}), text="Hmm, voyons."), reply("SILENCE"))
    inbox.submit(Inbound("appel", ("c",), "…", reply_via="imessage", allow_silence=True))
    await until(lambda: len(provider.requests) == 2)
    await asyncio.sleep(0.05)
    assert imessage.sent == []


class Empty(BaseModel):
    pass


async def nothing(_args: Any, _ctx: ToolContext) -> ToolResult:
    return ToolResult("")


class FileOutletFake(Outlet):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.files: list[str] = []
        self.asked: list[str] = []

    async def deliver_file(self, path: Path, caption: str, correlation_id: str | None) -> None:
        self.files.append(path.name)

    async def approve(self, proposal: ActionProposal) -> Approval:
        self.asked.append(proposal.tool.name)
        return Approval(True)


async def test_files_and_questions_go_where_he_last_wrote_from(
    inbox: OwnerInbox, provider: ScriptedProvider
) -> None:
    """D88: one way out for everything, whatever the channel; a channel is just a module."""
    imessage, telegram, tui = FileOutletFake("imessage"), FileOutletFake("telegram"), Outlet("tui")
    for outlet in (tui, imessage, telegram):
        inbox.register(outlet)
    await inbox.deliver_file(Path("vocal.m4a"), "", None)
    assert imessage.files == ["vocal.m4a"]  # the first that carries files: not the terminal
    provider.extend(reply("ok"))
    inbox.submit(Inbound("telegram", ("t",), "salut"))
    await until(lambda: bool(telegram.sent))
    await inbox.deliver_file(Path("image.png"), "", None)
    note = Tool(name="note", description="n", input_model=Empty, handler=nothing)
    assert await inbox.approve(ActionProposal(note, Empty(), Trust.WEB, "owner"))
    assert telegram.files == ["image.png"] and telegram.asked == ["note"]
    telegram.up = False  # gone: the next one up
    await inbox.deliver_file(Path("b.pdf"), "", None)
    assert imessage.files == ["vocal.m4a", "b.pdf"]
