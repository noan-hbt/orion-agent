"""Photos the owner sends: looked at once by a model that sees, read as content, kept."""

from __future__ import annotations

import asyncio
import base64
import io
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from PIL import Image

from orion.channels.imessage import IMessageChannel
from orion.channels.inbox import Attachment, OwnerInbox
from orion.config import VISION_MODEL
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.photos.module import (
    KEPT,
    SEEN,
    VIEW_SIDE,
    Photos,
    PhotoSettings,
    kind_of,
    photo_tools,
)
from orion.providers.base import ProviderError
from orion.runtime import PHOTO_TRUST, photo_block
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

    return Photos(journal, provider, PhotoSettings(direct=False), folder, fetch)


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
        tool.parse(f'{{"id": "{seen.id}", "question": "À quelle heure ?"}}'),
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
        tool.parse(f'{{"id": "{seen.id}", "question": "Et le prix ?"}}'),
        ToolContext("owner", "c"),
    )
    assert gone.is_error


async def test_what_cannot_be_read_is_kept_and_said(journal: SqliteJournal, tmp_path: Path) -> None:
    provider = ScriptedProvider()
    got: list[str] = []
    photos = photos_for(journal, provider, tmp_path / "photos", got)
    video = Attachment("https://media.inkbox.test/u/IMG_0042.MOV", "video/quicktime", 103)
    huge = Attachment("https://media.inkbox.test/b.jpg", "image/jpeg", 50 * 1024 * 1024)
    first, second = await photos.look([video, huge], "", "imessage:owner")
    assert first.tag == "file" and first.id.startswith("at_")
    attributes = dict(first.attributes)
    assert attributes["name"] == "IMG_0042.MOV" and attributes["type"] == "video/quicktime"
    assert _bytes(attributes["path"]) == JPEG
    assert "ne peux pas lire" in first.description
    assert "illisible" in second.description
    assert got == [video.url] and provider.requests == []  # kept; no model asked, huge not fetched
    (tool,) = photo_tools(photos)
    result = await tool.handler(
        tool.parse(f'{{"id": "{first.id}", "question": "Que dit-il ?"}}'),
        ToolContext("owner", "c"),
    )
    assert result.is_error


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


def _bytes(path: str) -> bytes:
    return Path(path).read_bytes()


def files_for(
    journal: SqliteJournal, provider: ScriptedProvider, folder: Path, data: bytes
) -> Photos:
    async def fetch(attachment: Attachment) -> bytes:
        return data

    return Photos(journal, provider, PhotoSettings(direct=False), folder, fetch)


async def test_a_heic_photo_is_turned_into_jpeg_first(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    import io

    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()  # pyright: ignore[reportUnknownMemberType]
    out = io.BytesIO()
    Image.new("RGB", (16, 12), (200, 30, 30)).save(out, "HEIF")
    provider = ScriptedProvider(reply("Un carré rouge."))
    photos = files_for(journal, provider, tmp_path / "photos", out.getvalue())
    attachment = Attachment("https://media.inkbox.test/IMG_1.HEIC", "image/heic", None)
    (seen,) = await photos.look([attachment], "", "imessage:owner")
    image = provider.requests[0].messages[1]["content"][1]["image_url"]["url"]
    assert image.startswith("data:image/jpeg;base64,")  # models refuse HEIC
    event = next(e for e in await journal.read() if e.kind == SEEN)
    assert event.payload["content_type"] == "image/jpeg"
    assert (photos.folder / event.payload["file"]).read_bytes()[:3] == b"\xff\xd8\xff"
    assert seen.description == "Un carré rouge."


async def test_a_document_is_read_once_from_the_file_itself(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    pdf = b"%PDF-1.7 facture"
    bill = "Une facture EDF : 87,40 €, à payer avant le 12 novembre 2026."
    provider = ScriptedProvider(reply(bill), reply("Référence 4471-AB."))
    photos = files_for(journal, provider, tmp_path / "photos", pdf)
    attachment = Attachment("https://media.inkbox.test/x/facture.pdf", "application/pdf", 16)
    (seen,) = await photos.look([attachment], "à payer ?", "imessage:owner")

    part = provider.requests[0].messages[1]["content"][1]
    assert part["type"] == "file" and part["file"]["filename"] == "facture.pdf"
    assert part["file"]["file_data"].startswith("data:application/pdf;base64,")
    assert "à payer ?" in provider.requests[0].messages[0]["content"]
    assert seen.tag == "file" and seen.description == bill
    block = photo_block(seen)
    assert block.startswith(f'<file id="{seen.id}" name="facture.pdf" type="application/pdf"')
    kept = next(e for e in await journal.read() if e.kind == KEPT)
    assert kept.trust == PHOTO_TRUST and kept.payload["kind"] == "document"

    (tool,) = photo_tools(photos)
    result = await tool.handler(
        tool.parse(f'{{"id": "{seen.id}", "question": "La référence ?"}}'),
        ToolContext("owner", "c"),
    )
    assert result.content == "Référence 4471-AB."
    assert provider.requests[1].messages[1]["content"][1]["type"] == "file"

    await journal.redact([kept.id], "forget", "owner:main")
    await photos.sync()
    assert list(photos.folder.iterdir()) == []


async def test_an_unread_document_is_still_kept(journal: SqliteJournal, tmp_path: Path) -> None:
    provider = ScriptedProvider()  # no answer scripted: the model call fails

    async def broken(_request: object) -> object:
        raise ProviderError("refused", retryable=False)

    provider.complete = broken  # type: ignore[method-assign]
    photos = files_for(journal, provider, tmp_path / "photos", b"PK docx")
    word = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    (seen,) = await photos.look(
        [Attachment("https://media.inkbox.test/contrat.docx", word, 7)], "", "imessage:owner"
    )
    assert "pas pu être lu" in seen.description and seen.id.startswith("at_")
    assert _bytes(dict(seen.attributes)["path"]) == b"PK docx"


async def test_a_text_file_is_its_own_words(journal: SqliteJournal, tmp_path: Path) -> None:
    text = "nom,montant\nLoyer,850\n" + "x" * 20_000
    provider = ScriptedProvider(reply("850 €."))
    photos = files_for(journal, provider, tmp_path / "photos", text.encode())
    (seen,) = await photos.look(
        [Attachment("https://media.inkbox.test/budget.csv", "text/csv", None)], "", "imessage:o"
    )
    assert provider.requests == []  # read as it is, no model in between
    assert seen.description.startswith("nom,montant\nLoyer,850") and "coupé" in seen.description
    (tool,) = photo_tools(photos)
    rest = await tool.handler(
        tool.parse(f'{{"id": "{seen.id}", "part": 1}}'), ToolContext("owner", "c")
    )
    assert provider.requests == [] and rest.content.startswith("x")  # Orion reads on itself


async def test_orion_reads_a_documents_own_text(journal: SqliteJournal, tmp_path: Path) -> None:
    """D114: a Word or PDF with text is read by Orion, not by another model; a scan, without
    text, still goes to the model that sees."""
    import zipfile

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as docx:
        docx.writestr(
            "word/document.xml",
            "<w:document><w:body><w:p><w:r><w:t>Bail de location</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Loyer mensuel : 850 &amp; charges 40 euros, payable le 5.</w:t>"
            "</w:r></w:p></w:body></w:document>",
        )
    provider = ScriptedProvider()
    photos = files_for(journal, provider, tmp_path / "photos", out.getvalue())
    word = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    (seen,) = await photos.look(
        [Attachment("https://media.inkbox.test/bail.docx", word, None)], "", "imessage:o"
    )
    assert provider.requests == []
    assert seen.description == (
        "Bail de location\nLoyer mensuel : 850 & charges 40 euros, payable le 5."
    )


def test_a_file_name_comes_from_its_link_or_its_type() -> None:
    assert kind_of("application/pdf") == "document"
    assert kind_of("text/calendar") == "text" and kind_of("application/ld+json") == "text"
    assert kind_of("audio/mpeg") == "audio" and kind_of("image/heic") == "image"


def a_form() -> bytes:
    """A real photo, bigger than what the model gets."""
    out = io.BytesIO()
    Image.new("RGB", (3024, 4032), (240, 240, 230)).save(out, "JPEG")
    return out.getvalue()


async def test_the_everyday_model_looks_at_a_photo_itself(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    """D87: no model in between. The journal keeps the text; the image is on the wire, the same
    bytes on every request (prompt cache)."""
    provider = ScriptedProvider(reply("C'est un constat."), reply("De rien."))
    orion = make_orion(journal, provider)
    await orion.start()

    async def fetch(_attachment: Attachment) -> bytes:
        return a_form()

    photos = Photos(journal, provider, PhotoSettings(), tmp_path / "photos", fetch)
    orion.inline = photos.inline
    [photo] = await photos.look([Attachment("https://x.test/c.jpg", "image/jpeg")], "", "t")
    assert provider.requests == []  # nobody looked at it for Orion
    await orion.handle_owner_message("owner", "tiens", "imessage", photos=[photo])
    await orion.handle_owner_message("owner", "merci", "imessage")

    first, second = provider.requests
    text, image = first.messages[-1]["content"]
    assert 'vue="directe"' in text["text"] and image["type"] == "image_url"
    with Image.open(io.BytesIO(base64.b64decode(image["image_url"]["url"].split(",")[1]))) as seen:
        assert max(seen.size) == VIEW_SIDE
    assert second.messages[: len(first.messages)] == first.messages
    [said] = [
        e.payload["message"]
        for e in await journal.read()
        if e.kind == "conversation.message" and e.payload["message"]["role"] == "user"
    ][:1]
    assert isinstance(said["content"], str)  # the journal never holds the image


def test_a_forgotten_photo_leaves_its_text_alone(tmp_path: Path) -> None:
    photos = Photos(None, ScriptedProvider(), PhotoSettings(), tmp_path, None)  # type: ignore[arg-type]
    message = {"role": "user", "content": '<photo id="ph_01ABC" vue="directe">\n…\n</photo>'}
    assert photos.inline(message) == message


def test_a_document_that_unzips_into_gigabytes_is_not_read_by_code() -> None:
    import io
    import zipfile

    from orion.photos.module import MAX_UNZIPPED, text_of

    docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    small = io.BytesIO()
    with zipfile.ZipFile(small, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"<w:t>a</w:t>" * (MAX_UNZIPPED // 10))
    assert len(small.getvalue()) < 1_000_000  # small to download, huge once unzipped
    assert text_of(small.getvalue(), docx) == ""


async def test_a_file_being_kept_is_not_taken_for_a_forgotten_one(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    photos = photos_for(journal, ScriptedProvider(), tmp_path / "photos", [])
    photos._write("ph_new.jpg", b"\xff\xd8")  # pyright: ignore[reportPrivateUsage]
    await photos.sync()  # its event is not appended yet
    assert [p.name for p in photos.folder.iterdir()] == ["ph_new.jpg"]
