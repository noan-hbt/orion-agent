"""Photos the owner sends: looked at once by a model that sees, read as content, kept."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from orion.channels.imessage import IMessageChannel
from orion.channels.inbox import Attachment, OwnerInbox
from orion.config import VISION_MODEL
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.photos.module import SEEN, Photos, PhotoSettings, photo_tools
from orion.runtime import PHOTO_TRUST
from orion.tools.base import ToolContext
from tests.fake_inkbox import FakeInkbox
from tests.fakes import ScriptedProvider, reply
from tests.test_imessage import SETTINGS, make_orion, until

FLYER = "Une affiche de concert.\n« Feu! Chatterton — Olympia, samedi 14 novembre, 20 h »"
JPEG = b"\xff\xd8\xff" + b"0" * 100


def photos_for(
    journal: SqliteJournal, provider: ScriptedProvider, folder: Path, got: list[str]
) -> Photos:
    async def fetch(attachment: Attachment) -> bytes:
        got.append(attachment.url)
        return JPEG

    return Photos(journal, provider, PhotoSettings(), folder, fetch)


@pytest.fixture
async def seeing(
    journal: SqliteJournal, tmp_path: Path
) -> AsyncIterator[tuple[IMessageChannel, FakeInkbox, ScriptedProvider, Photos]]:
    provider = ScriptedProvider()
    fake = FakeInkbox()
    orion = make_orion(journal, provider)
    await orion.start()
    inbox = OwnerInbox(orion, coalesce_seconds=0.05)
    photos = photos_for(journal, provider, tmp_path / "photos", [])
    inbox.eyes = photos.look
    channel = IMessageChannel(fake.client(), journal, inbox, SETTINGS)
    await channel.start()
    worker = asyncio.create_task(inbox.run())
    yield channel, fake, provider, photos
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker


def with_photo(fake: FakeInkbox, text: str) -> None:
    message = fake.inbound(text)
    message["media"] = [
        {"url": "https://media.inkbox.test/a.jpg", "content_type": "image/jpeg", "size": 103}
    ]


async def test_a_photo_is_described_once_and_read_as_content(
    seeing: tuple[IMessageChannel, FakeInkbox, ScriptedProvider, Photos],
    journal: SqliteJournal,
) -> None:
    channel, fake, provider, photos = seeing
    provider.extend(reply(FLYER), reply("Je te l'ajoute à l'agenda ?"))
    with_photo(fake, "on y va ?")
    await channel.poll_once()
    await until(lambda: len(fake.sent) == 1)

    look, turn = provider.requests
    assert look.model == VISION_MODEL
    image = look.messages[1]["content"][1]["image_url"]["url"]
    assert image.startswith("data:image/jpeg;base64,")
    assert "on y va ?" in look.messages[0]["content"]  # the caption focuses the look

    said = turn.messages[-1]["content"]
    assert "on y va ?" in said and '<photo id="ph_' in said and "Olympia" in said
    assert all("image_url" not in str(m) for m in turn.messages)  # only words reach the model
    events = await journal.read()
    received = next(e for e in events if e.kind == "message.received")
    assert received.trust == PHOTO_TRUST < Trust.SYSTEM
    seen = next(e for e in events if e.kind == SEEN)
    assert seen.payload["description"] == FLYER
    assert (photos.folder / seen.payload["file"]).read_bytes() == JPEG


async def test_orion_can_look_again_and_forgetting_removes_the_file(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    provider = ScriptedProvider(reply(FLYER), reply("20 h, porte à 19 h."))
    photos = photos_for(journal, provider, tmp_path / "photos", [])
    attachment = Attachment("https://media.inkbox.test/a.jpg", "image/jpeg", 103)
    (seen,) = await photos.look([attachment], "", "imessage:owner")
    (tool,) = photo_tools(photos)
    result = await tool.handler(
        tool.parse(f'{{"photo": "{seen.id}", "question": "À quelle heure ?"}}'),
        ToolContext("owner", "c"),
    )
    assert result.content == "20 h, porte à 19 h." and result.trust == PHOTO_TRUST
    assert "À quelle heure ?" in str(provider.requests[1].messages)

    event = next(e for e in await journal.read() if e.kind == SEEN)
    assert event.provenance.source == "imessage:owner"
    await journal.redact([event.id], "forget", "owner:main")
    await photos.sync()
    assert list(photos.folder.iterdir()) == []
    gone = await tool.handler(
        tool.parse(f'{{"photo": "{seen.id}", "question": "Et le prix ?"}}'),
        ToolContext("owner", "c"),
    )
    assert gone.is_error


async def test_what_cannot_be_seen_is_said(journal: SqliteJournal, tmp_path: Path) -> None:
    provider = ScriptedProvider()
    got: list[str] = []
    photos = photos_for(journal, provider, tmp_path / "photos", got)
    video = Attachment("https://media.inkbox.test/v.mov", "video/quicktime", 10)
    huge = Attachment("https://media.inkbox.test/b.jpg", "image/jpeg", 50 * 1024 * 1024)
    first, second = await photos.look([video, huge], "", "imessage:owner")
    assert "video/quicktime" in first.description and not first.id
    assert "illisible" in second.description
    assert got == [] and provider.requests == []  # nothing fetched, no model asked


async def test_a_requeued_photo_is_fetched_again(journal: SqliteJournal, tmp_path: Path) -> None:
    """Inbound links expire after an hour: after a restart, the message is read again."""
    fake = FakeInkbox()
    provider = ScriptedProvider()
    orion = make_orion(journal, provider)
    await orion.start()
    inbox = OwnerInbox(orion)
    first = IMessageChannel(fake.client(), journal, inbox, SETTINGS)
    await first.start()
    with_photo(fake, "")
    await first.poll_once()  # journaled, but the process dies before the turn
    fake.messages["in-1"]["media"][0]["url"] = "https://media.inkbox.test/fresh.jpg"

    queued: list[tuple[Attachment, ...]] = []
    restarted = OwnerInbox(orion)
    restarted.submit = lambda item: queued.append(item.media)  # type: ignore[method-assign]
    await IMessageChannel(fake.client(), journal, restarted, SETTINGS).start()
    assert [a.url for media in queued for a in media] == ["https://media.inkbox.test/fresh.jpg"]
