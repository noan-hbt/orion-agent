"""Telegram run split (D89): the guardian holds the bot's token and checks every request."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from orion.channels.inbox import OwnerInbox
from orion.channels.telegram import YES, TelegramChannel, TelegramError, TelegramSettings
from orion.channels.telegram_module import TelegramModule
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.guardian.client import GuardianUnavailableError
from orion.guardian.policy import ActionProposal
from orion.guardian.secrets import exposed
from orion.guardian.service import GuardianService
from orion.guardian.telegram import TelegramGate, answer, decided
from orion.tools.base import Tool
from tests.fake_guardian import Running, guardian_config, running_guardian
from tests.fake_inkbox import OWNER as OWNER_NUMBER
from tests.fake_telegram import OWNER, STRANGER, TOKEN, FakeTelegram
from tests.fakes import ScriptedProvider, reply
from tests.test_imessage import make_orion, until
from tests.test_telegram import Empty, nothing

SETTINGS = TelegramSettings(owner_id=OWNER, approval_timeout_seconds=5.0)
ANGELE = 5151


def split_config(tmp_path: Path) -> Any:
    return guardian_config(
        tmp_path,
        channels={
            "imessage": {"enabled": True, "identity": "orion-test", "owner_number": OWNER_NUMBER},
            "telegram": {"enabled": True, "owner_id": OWNER},
        },
        modules={
            "secretary": {
                "enabled": True,
                "contacts": [{"name": "Angèle", "telegram_id": ANGELE, "about": "sa copine"}],
            }
        },
    )


@dataclass
class Split:
    running: Running
    tg: FakeTelegram
    channel: TelegramChannel
    provider: ScriptedProvider


@pytest.fixture
async def split(journal: SqliteJournal, tmp_path: Path) -> AsyncIterator[Split]:
    tg = FakeTelegram()

    def gate(service: GuardianService) -> None:
        TelegramGate(service, tg.client()).install()

    async with running_guardian(split_config(tmp_path), prepare=gate) as running:
        provider = ScriptedProvider()
        orion = make_orion(journal, provider)
        await orion.start()
        inbox = OwnerInbox(orion, coalesce_seconds=0.05)
        client = running.client.telegram()
        channel = TelegramChannel(client, journal, inbox, SETTINGS, desk=running.client)
        await channel.start()
        worker = asyncio.create_task(inbox.run())
        yield Split(running, tg, channel, provider)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        await client.aclose()


async def hello(split: Split) -> None:
    split.provider.extend(reply("Salut."))
    split.tg.write("salut")
    await split.channel.poll_once()
    await until(lambda: bool(split.tg.sent()))


async def test_the_core_talks_to_telegram_without_the_token(split: Split) -> None:
    await hello(split)
    assert [m["text"] for m in split.tg.sent()] == ["Salut."]
    assert TOKEN not in repr(vars(split.running.client.telegram()))
    split.tg.send_photo("regarde", b"\xff\xd8" + b"0" * 50)
    await split.channel.poll_once()
    [message] = [m for m in split.channel.state.owner_messages.values() if m.media]
    attachment = split.channel._attachment(message.media[0])  # pyright: ignore[reportPrivateUsage]
    assert attachment.fetch is not None and len(await attachment.fetch()) == 52


async def test_the_guardian_writes_only_to_the_owner(split: Split, tmp_path: Path) -> None:
    client = split.running.client.telegram()
    attempts: tuple[tuple[str, dict[str, Any]], ...] = (
        ("sendMessage", {"chat_id": STRANGER, "text": "coucou"}),
        ("setWebhook", {"url": "https://evil.example"}),
        ("forwardMessage", {"chat_id": OWNER, "from_chat_id": OWNER, "message_id": 1}),
    )
    for method, params in attempts:
        with pytest.raises(TelegramError, match="guardian"):
            await client.call(method, **params)
    picture = tmp_path / "renard.png"
    picture.write_bytes(b"\x89PNG" + b"0" * 20)
    with pytest.raises(TelegramError, match="owner's chat"):
        await client.send_file(STRANGER, "sendPhoto", "photo", "renard.png", b"x", "")
    await client.send_file(OWNER, "sendPhoto", "photo", "renard.png", b"\x89PNG", "le renard")
    assert split.tg.sent("sendPhoto") and not split.tg.sent()
    await client.aclose()


async def test_an_approval_is_asked_and_checked_by_the_guardian(split: Split) -> None:
    await hello(split)
    tool = Tool(name="send", description="s", input_model=Empty, handler=nothing)
    proposal = ActionProposal(tool, Empty(), Trust.OWNER, "o")
    asking = asyncio.create_task(split.channel.approve(proposal))
    await until(lambda: any(m.get("reply_markup") for m in split.tg.sent()))
    [question] = [m for m in split.tg.sent() if m.get("reply_markup")]
    assert question["text"].startswith("Je fais send") or "send" in question["text"]
    asked = max(split.channel._approvals)  # pyright: ignore[reportPrivateUsage]

    # Before he answers, the grant opens nothing.
    service = split.running.service
    grant = next(iter(service.ledger.state.approvals))
    assert await service.use_grant(grant, "send", lambda _p: True, consume=False) == (
        "the owner has not said yes"
    )
    split.tg.press(asked, YES, sender=STRANGER)  # not him: ignored
    split.tg.press(asked, YES)
    await split.channel.poll_once()
    approval = await asking
    assert approval and approval.grant == grant
    assert await service.use_grant(grant, "send", lambda _p: True, consume=True) is None
    assert await service.use_grant(grant, "send", lambda _p: True, consume=True) == (
        "that approval was already used"
    )


def test_a_word_answers_only_the_last_question_before_it() -> None:
    questions = {10, 20}
    word = answer(
        {"message": {"message_id": 21, "from": {"id": OWNER}, "chat": {"type": "private"},
                     "text": "oui"}},
        OWNER,
        questions,
    )  # fmt: skip
    assert word is not None
    assert word == {"message_id": 21, "answer": True, "how": "word"}
    assert decided(20, [word], questions) and not decided(10, [word], questions)
    no = answer(
        {"callback_query": {"from": {"id": OWNER}, "data": "no", "message": {"message_id": 20}}},
        OWNER,
        questions,
    )
    assert no is not None and not decided(20, [word, no], questions)
    stranger = {"callback_query": {"from": {"id": STRANGER}, "data": "yes",
                                   "message": {"message_id": 20}}}  # fmt: skip
    assert answer(stranger, OWNER, questions) is None


def test_the_bot_token_belongs_to_the_guardian(tmp_path: Path) -> None:
    config = split_config(tmp_path)
    assert "TELEGRAM_BOT_TOKEN" in exposed(config, {"TELEGRAM_BOT_TOKEN": TOKEN})


async def test_the_module_reaches_telegram_through_the_guardian(tmp_path: Path) -> None:
    class Plate:
        def __init__(self, guardian: Any) -> None:
            self.guardian = guardian

    async with running_guardian(split_config(tmp_path)) as running:
        module = TelegramModule(split_config(tmp_path))
        assert await module.assemble(Plate(running.client)) == []  # type: ignore[arg-type]
        assert module.doctor() == [f"bot token held by the guardian, owner id {OWNER}"]
        await module.close()


async def test_a_contact_is_answered_only_after_writing(split: Split) -> None:
    client = split.running.client.telegram()
    with pytest.raises(TelegramError, match="has not written"):
        await client.send_message(ANGELE, "coucou")  # she never wrote: Orion never starts
    split.tg.write("il est où Noan ?", sender=ANGELE)
    await split.channel.poll_once()  # the update passes through the guardian
    await client.send_message(ANGELE, "Il est au travail, je lui dis que tu as écrit.")
    assert split.tg.sent()[-1]["chat_id"] == ANGELE
    with pytest.raises(TelegramError, match="owner's chat"):
        await client.send_message(ANGELE, "Oui ?", [("Oui", YES)])  # no buttons for her
    with pytest.raises(TelegramError, match="owner's chat"):
        await client.send_message(STRANGER, "salut")
    await client.aclose()


async def test_writing_first_to_a_contact_needs_his_yes(split: Split) -> None:
    with pytest.raises(GuardianUnavailableError, match="approval"):
        await split.running.client.contact_send("Angèle", f"telegram:{ANGELE}", "J'arrive", None)
    assert not split.tg.sent()


async def test_the_console_button_and_login_go_through_the_guardian(tmp_path: Path) -> None:
    import time

    from tests.test_console import signed

    tg = FakeTelegram()
    config = split_config(tmp_path).model_copy(
        update={"modules": {"console": {"enabled": True, "public_url": "https://o.test"}}}
    )

    def gate(service: GuardianService) -> None:
        TelegramGate(service, tg.client()).install()

    async with running_guardian(config, prepare=gate) as running:
        client = running.client.telegram()
        await client.menu_button(OWNER, "Console", "https://o.test/console/telegram")
        assert [n for n, _ in tg.calls] == ["setChatMenuButton"]
        with pytest.raises(TelegramError, match="console's own address"):
            await client.menu_button(OWNER, "Console", "https://evil.test/console/")
        with pytest.raises(TelegramError, match="owner's chat"):
            await client.menu_button(STRANGER, "Console", "https://o.test/console/telegram")
        now = time.time()
        assert await running.client.telegram_login(signed(TOKEN, OWNER, now)) == OWNER
        assert await running.client.telegram_login(signed("1:other", OWNER, now)) is None
        await client.aclose()
