"""Telegram, a second channel through the same inbox (D88)."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from orion.adaptation.signals import FEEDBACK_REACTION
from orion.channels.inbox import Attachment, OwnerInbox
from orion.channels.telegram import (
    INBOUND,
    YES,
    TelegramChannel,
    TelegramSettings,
    _parts,  # pyright: ignore[reportPrivateUsage]
)
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.guardian.policy import ActionProposal
from orion.runtime import Orion, Photo
from orion.tools.base import Tool, ToolContext, ToolResult
from tests.fake_telegram import OWNER, STRANGER, TOKEN, FakeTelegram
from tests.fakes import ScriptedProvider, reply
from tests.test_imessage import make_orion, until

SETTINGS = TelegramSettings(owner_id=OWNER, approval_timeout_seconds=5.0)


@pytest.fixture
async def world(
    journal: SqliteJournal,
) -> AsyncIterator[tuple[TelegramChannel, FakeTelegram, ScriptedProvider, OwnerInbox, Orion]]:
    provider, fake = ScriptedProvider(), FakeTelegram()
    orion = make_orion(journal, provider)
    await orion.start()
    inbox = OwnerInbox(orion, coalesce_seconds=0.05)
    channel = TelegramChannel(fake.client(), journal, inbox, SETTINGS)
    await channel.start()
    worker = asyncio.create_task(inbox.run())
    yield channel, fake, provider, inbox, orion
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker


async def test_the_owner_is_answered_and_a_stranger_is_not(
    world: tuple[TelegramChannel, FakeTelegram, ScriptedProvider, OwnerInbox, Orion],
    journal: SqliteJournal,
) -> None:
    channel, fake, provider, _, _ = world
    provider.extend(reply("Salut Noan."))
    fake.write("Salut", sender=STRANGER)
    fake.write("Salut")
    await channel.poll_once()
    await until(lambda: bool(fake.sent()))
    assert [m["text"] for m in fake.sent()] == ["Salut Noan."]
    assert fake.sent()[0]["chat_id"] == OWNER
    assert any(name == "sendChatAction" for name, _ in fake.calls)  # « en train d'écrire »
    inbound = [e for e in await journal.read() if e.kind == INBOUND]
    assert [e.trust for e in inbound] == [Trust.THIRD_PARTY, Trust.OWNER]
    await channel.poll_once()  # read once: polling resumes after the last update
    assert len(provider.requests) == 1
    offsets = [p["offset"] for name, p in fake.calls if name == "getUpdates"]
    assert offsets == [0, 3]


async def test_a_message_never_answered_is_taken_up_after_a_restart(journal: SqliteJournal) -> None:
    fake = FakeTelegram()
    fake.write("Rappelle-moi le pain")
    crashed = OwnerInbox(make_orion(journal, ScriptedProvider()), coalesce_seconds=0.05)
    await TelegramChannel(fake.client(), journal, crashed, SETTINGS).poll_once()  # no worker

    provider = ScriptedProvider(reply("C'est noté."))
    orion = make_orion(journal, provider)
    await orion.start()
    inbox = OwnerInbox(orion, coalesce_seconds=0.05)
    channel = TelegramChannel(fake.client(), journal, inbox, SETTINGS)
    await channel.start()
    worker = asyncio.create_task(inbox.run())
    await until(lambda: bool(fake.sent()))
    worker.cancel()
    assert "Rappelle-moi le pain" in provider.requests[0].messages[-1]["content"]


async def test_a_photo_is_fetched_by_the_channel_without_its_key(
    world: tuple[TelegramChannel, FakeTelegram, ScriptedProvider, OwnerInbox, Orion],
) -> None:
    channel, fake, provider, inbox, _ = world
    seen: list[Attachment] = []

    async def eyes(attachments: Any, caption: str, source: str) -> list[Photo]:
        seen.extend(attachments)
        assert attachments[0].fetch is not None
        data = await attachments[0].fetch()
        return [Photo("ph_1", f"une photo de {len(data)} octets")]

    inbox.eyes = eyes
    provider.extend(reply("Joli."))
    fake.send_photo("regarde", b"\xff\xd8" + b"0" * 50)
    await channel.poll_once()
    await until(lambda: bool(fake.sent()))
    assert seen[0].url == "telegram:photo-big" and TOKEN not in repr(seen[0])
    assert "une photo de 52 octets" in provider.requests[0].messages[-1]["content"]


class Empty(BaseModel):
    pass


async def nothing(_args: Any, _ctx: ToolContext) -> ToolResult:
    return ToolResult("")


async def test_an_action_is_put_to_him_with_two_buttons(
    world: tuple[TelegramChannel, FakeTelegram, ScriptedProvider, OwnerInbox, Orion],
) -> None:
    channel, fake, provider, *_ = world
    provider.extend(reply("Salut."))
    fake.write("salut")
    await channel.poll_once()
    await until(lambda: bool(fake.sent()))  # the owner's chat is known
    tool = Tool(name="send", description="s", input_model=Empty, handler=nothing)
    asking = asyncio.create_task(channel.approve(ActionProposal(tool, Empty(), Trust.OWNER, "o")))
    await until(lambda: any(m.get("reply_markup") for m in fake.sent()))
    [question] = [m for m in fake.sent() if m.get("reply_markup")]
    buttons = question["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["Oui", "Non"]
    asked = max(channel._approvals)  # pyright: ignore[reportPrivateUsage]
    fake.press(asked, YES)
    await channel.poll_once()
    assert await asking
    assert any(name == "editMessageReplyMarkup" for name, _ in fake.calls)  # buttons gone


async def test_his_reactions_are_signals(
    world: tuple[TelegramChannel, FakeTelegram, ScriptedProvider, OwnerInbox, Orion],
    journal: SqliteJournal,
) -> None:
    channel, fake, provider, _, _ = world
    provider.extend(reply("Voilà."))
    fake.write("salut")
    await channel.poll_once()
    await until(lambda: bool(fake.sent()))
    fake.react(150, "👍")
    fake.react(150, "👍", sender=STRANGER)
    await channel.poll_once()
    [signal] = [e for e in await journal.read() if e.kind == FEEDBACK_REACTION]
    assert signal.payload["reaction"] == "like" and signal.payload["remote_id"] == "150"


async def test_a_file_goes_as_a_photo_or_a_voice_message(
    world: tuple[TelegramChannel, FakeTelegram, ScriptedProvider, OwnerInbox, Orion],
    tmp_path: Path,
) -> None:
    channel, fake, provider, *_ = world
    provider.extend(reply("Salut."))
    fake.write("salut")
    await channel.poll_once()
    await until(lambda: bool(fake.sent()))
    picture = tmp_path / "renard.png"
    picture.write_bytes(b"\x89PNG" + b"0" * 20)
    await channel.deliver_file(picture, "le renard", None)
    assert fake.sent("sendPhoto")
    if shutil.which("ffmpeg"):
        made = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=1", "-y",
            str(tmp_path / "vocal.m4a"),
        )  # fmt: skip
        await made.wait()
        await channel.deliver_file(tmp_path / "vocal.m4a", "", None)
        assert fake.sent("sendVoice")


def test_a_long_answer_is_cut_at_paragraphs() -> None:
    long = "a" * 3000 + "\n\n" + "b" * 3000
    assert [len(p) for p in _parts(long)] == [3000, 3000]
    assert _parts("court") == ["court"]
