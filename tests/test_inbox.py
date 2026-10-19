from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager

import pytest
import structlog.testing

from orion.channels.inbox import Inbound, OwnerInbox
from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler
from orion.core.journal import SqliteJournal
from orion.guardian.policy import Guardian
from orion.runtime import Orion
from orion.tools.base import ToolRegistry
from tests.fakes import ScriptedProvider, reply


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
    assert "· tui]" in provider.requests[1].messages[-1]["content"]
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
