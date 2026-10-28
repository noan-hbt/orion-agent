"""Files Orion makes and sends: pictures, PDFs, screenshots, to the owner only."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from orion.channels.imessage import IMessageChannel
from orion.channels.inbox import OwnerInbox
from orion.computer.direct import BrowserDesk, browser_tools
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.files.module import MADE, FileStore, file_tool, page_html, render_pdf
from orion.guardian.client import GuardianUnavailableError
from orion.hands.hand import Hand, HandConfig
from orion.tools.base import Tool, ToolContext, ToolResult
from orion.web.search import WebSearch
from orion.web.service import DOWNLOADED, WebDesk
from tests.fake_guardian import guardian_config, running_guardian
from tests.fake_inkbox import CONVERSATION, FakeInkbox
from tests.fakes import Script, ScriptedProvider
from tests.test_browser_direct import owner_says
from tests.test_computer import Site, session
from tests.test_hands import server, until
from tests.test_imessage import SETTINGS, make_orion
from tests.test_secretary import MUM, MUM_CHAT
from tests.test_secretary import SETTINGS as SECRETARY
from tests.test_web import allow_all, site

_ = server  # the fixture, for the hand below
PNG = base64.b64encode(b"\x89PNG fake").decode()
CTX = ToolContext("owner", "c", context_trust=Trust.OWNER)


def picture(request: Any) -> Any:
    from orion.providers.base import ModelResponse, Usage

    assert request.image_output
    return ModelResponse(
        message={"role": "assistant", "content": ""},
        text="",
        tool_calls=[],
        finish_reason="stop",
        usage=Usage(),
        model="img",
        images=[f"data:image/png;base64,{PNG}"],
    )


async def run(tool: Tool, **args: object) -> ToolResult:
    return await tool.handler(tool.parse(json.dumps(args)), CTX)


def test_a_document_is_plain_readable_html() -> None:
    page = page_html("Devis", "# Total\nUn **gros** total\n- ligne 1\n- ligne <2>\nFin")
    assert "<h1>Devis</h1>" in page and "<h2>Total</h2>" in page
    assert "<strong>gros</strong>" in page and "<li>ligne &lt;2&gt;</li></ul><p>Fin</p>" in page


async def test_a_real_pdf_is_printed() -> None:
    data = await render_pdf(page_html("Bonjour", "Un paragraphe."))
    assert data.startswith(b"%PDF")


async def test_a_picture_and_a_pdf_are_made_kept_and_sent(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    store = FileStore(journal, tmp_path / "files")
    sent: list[tuple[Path, str]] = []

    async def deliver(path: Path, caption: str, _cid: str | None) -> None:
        sent.append((path, caption))

    async def render(page: str) -> bytes:
        assert "Liste de courses" in page
        return b"%PDF-1.7 fake"

    script: Script = picture
    tool = file_tool(store, ScriptedProvider(script), "img/model", lambda: deliver, render)
    made = await run(tool, action="image", prompt="un chat roux sur un toit")
    image_id = made.content.split("(")[1].split(")")[0]
    doc = await run(tool, action="pdf", title="Liste de courses", text="- pain\n- lait")
    pdf_id = doc.content.split("(")[1].split(",")[0]

    await run(tool, action="send", file=image_id)
    await run(tool, action="send", file=pdf_id)
    assert [(p.read_bytes()[:4], c) for p, c in sent] == [(b"\x89PNG", ""), (b"%PDF", "")]
    listing = await run(tool, action="list")
    assert "image.png" in listing.content and "Liste de courses.pdf" in listing.content

    events = [e for e in await journal.read() if e.kind == MADE]
    assert "chat roux" not in str([e.payload for e in events])  # what, never the content
    await journal.redact([events[0].id], "forget", "owner:main")
    await store.sync()
    assert store.get(image_id) is None and len(list(store.folder.iterdir())) == 1
    assert (await run(tool, action="send", file=image_id)).is_error


async def test_a_screenshot_of_the_page_shown(journal: SqliteJournal, tmp_path: Path) -> None:
    store = FileStore(journal, tmp_path / "files")

    async def keep(name: str, data: bytes, kind: str, ctx: ToolContext) -> str:
        return (await store.save(name, data, kind, "screenshot", ctx, Trust.WEB)).id

    site = Site()
    desk = BrowserDesk(journal, lambda: session(site))
    tools = {t.name: t for t in browser_tools(desk, keep, looks=True)}
    try:
        nothing = await run(tools["browser_screenshot"])
        assert nothing.is_error
        await owner_says(journal, "Montre-moi https://annonces.test/", "c")
        await run(tools["browser_open"], url="https://annonces.test/")
        shot = await run(tools["browser_screenshot"])
        file_id = shot.content.split("(")[1].split(")")[0]
        made = store.get(file_id)
        assert made is not None and made.path.read_bytes().startswith(b"\x89PNG")
        assert f"`attachment_look` ({file_id})" in shot.content  # it can see what it sends
        full = await run(tools["browser_screenshot"], full=True)
        assert "Capture prête" in full.content
    finally:
        await desk.aclose()
    trusts = [e.trust for e in await journal.read() if e.kind == MADE]
    assert trusts and all(t == Trust.WEB for t in trusts)  # a page is web content


async def test_imessage_uploads_then_sends_with_the_file(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    fake = FakeInkbox()
    orion = make_orion(journal, ScriptedProvider())
    channel = IMessageChannel(fake.client(), journal, OwnerInbox(orion), SETTINGS)
    await channel.start()
    fake.inbound("salut")
    await channel.poll_once()
    file = tmp_path / "chat.png"
    file.write_bytes(b"\x89PNG fake")
    await channel.deliver_file(file, "Ton chat", None)
    assert fake.uploads and b"\x89PNG fake" in fake.uploads[0]
    assert fake.sent[-1]["media"] == ["https://media.inkbox.test/1"]
    assert fake.sent[-1]["text"] == "Ton chat"
    queued = [e for e in await journal.read() if e.kind == "outbox.queued"]
    assert queued[-1].payload["media"] == str(file)  # the path, not the bytes


async def test_the_guardian_lets_files_go_to_the_owner_only(tmp_path: Path) -> None:
    fake = FakeInkbox()
    fake.inbound("salut")
    fake.inbound("Coucou", sender=MUM, conversation=MUM_CHAT)
    config = guardian_config(tmp_path, modules={"secretary": SECRETARY.model_dump()})
    from datetime import datetime

    at = datetime.fromisoformat(fake.messages["in-2"]["created_at"])
    async with running_guardian(config, fake, clock=lambda: at) as g:
        inkbox = g.client.inkbox()
        url = await inkbox.upload("chat.png", b"\x89PNG", "image/png")
        await inkbox.send(CONVERSATION, "Ton chat", "k1", None, media_url=url)
        assert fake.sent[-1]["media"] == [url]
        with pytest.raises(Exception, match="files to the owner only"):
            await inkbox.send(MUM_CHAT, "Tiens", "k2", None, media_url=url)
        with pytest.raises((GuardianUnavailableError, Exception), match="too large"):
            await inkbox.upload("big.bin", b"0" * (11 * 1024 * 1024), "application/octet-stream")


def tool_for(store: FileStore, sent: list[tuple[Path, str]], **sources: Any) -> Tool:
    async def deliver(path: Path, caption: str, _cid: str | None) -> None:
        sent.append((path, caption))

    return file_tool(store, ScriptedProvider(), "img", lambda: deliver, **sources)


def made_id(result: ToolResult) -> str:
    assert not result.is_error, result.content
    return result.content.split("(")[1].split(",")[0]


async def test_any_file_on_the_web_is_kept_and_sent_as_it_is(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    xml = b'<?xml version="1.0"?><config><port>8080</port></config>'
    client = site(
        {
            "https://x.example/dl/config.xml": httpx.Response(
                200, content=xml, headers={"content-type": "application/xml"}
            ),
            "https://x.example/big.zip": httpx.Response(200, content=b"0" * (11 * 1024 * 1024)),
        }
    )
    desk = WebDesk(journal, WebSearch(ScriptedProvider(), "m"), client, allow_all)
    await desk.start()
    store = FileStore(journal, tmp_path / "files")
    sent: list[tuple[Path, str]] = []
    tool = tool_for(store, sent, web=lambda: desk)
    try:
        file_id = made_id(await run(tool, action="download", url="https://x.example/dl/config.xml"))
        await run(tool, action="send", file=file_id)
        too_big = await run(tool, action="download", url="https://x.example/big.zip")
        assert too_big.is_error and "larger" in too_big.content
    finally:
        await desk.aclose()
    [(path, caption)] = sent
    assert path.name == "config.xml" and path.read_bytes() == xml and caption == ""
    events = await journal.read()
    [got] = [e for e in events if e.kind == DOWNLOADED]
    assert got.trust is Trust.WEB and "8080" not in str(got.payload)
    assert [e.trust for e in events if e.kind == MADE] == [Trust.WEB]
    assert tool.accepts_untrusted_for(tool.parse('{"action": "download", "url": "https://a.b"}'))


async def test_a_file_on_the_server_but_never_a_secret(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    site_dir = tmp_path / "www"
    site_dir.mkdir()
    (site_dir / "index.php").write_text("<?php echo 'bonjour'; ?>")
    (site_dir / ".env").write_text("KEY=secret")
    (site_dir / "prod.env").write_text("KEY=secret")
    (site_dir / ".env.production").write_text("KEY=secret")
    (site_dir / "server.pem").write_text("-----BEGIN PRIVATE KEY-----")
    guardian = tmp_path / "guardian"
    guardian.mkdir()
    (guardian / "guardian.sqlite3").write_bytes(b"ledger")
    (site_dir / "innocent.txt").symlink_to(guardian / "guardian.sqlite3")
    store = FileStore(journal, tmp_path / "files")
    sent: list[tuple[Path, str]] = []
    tool = tool_for(store, sent, closed=(guardian,))

    file_id = made_id(await run(tool, action="server", path=str(site_dir / "index.php")))
    await run(tool, action="send", file=file_id)
    assert sent[0][0].name == "index.php" and b"bonjour" in sent[0][0].read_bytes()
    refused = {
        str(site_dir / ".env"): "secrets",
        str(site_dir / "prod.env"): "secrets",
        str(site_dir / ".env.production"): "secrets",
        str(site_dir / "server.pem"): "secrets",
        str(guardian / "guardian.sqlite3"): "never sends from",
        str(site_dir / "innocent.txt"): "never sends from",  # a link is followed, then checked
        "/proc/self/environ": "never sends from",
        "index.php": "absolute",
        str(site_dir / "nope.txt"): "not a file",
        str(site_dir): "not a file",
    }
    for path, why in refused.items():
        result = await run(tool, action="server", path=path)
        assert result.is_error and why in result.content, (path, result.content)
    # Steered by a page, it asks the owner first.
    assert not tool.accepts_untrusted_for(tool.parse('{"action": "server", "path": "/a"}'))


async def test_a_file_from_a_folder_the_owner_shared(
    server: tuple[Any, str], tmp_path: Path, journal: SqliteJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    desk, url = server
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    shared = tmp_path / "site"
    shared.mkdir()
    (shared / "index.php").write_bytes(b"<?php phpinfo(); ?>\x00binary ok")
    (tmp_path / "secret.txt").write_text("non")
    code = await desk.new_code("mac")
    from orion.hands.hand import pair

    config = await pair(url, code, [str(shared)], ["get_file"])
    running = asyncio.create_task(Hand(config).session())
    await until(lambda: "mac" in desk.connected)
    store = FileStore(journal, tmp_path / "files")
    sent: list[tuple[Path, str]] = []
    tool = tool_for(store, sent, hands=lambda: desk)
    try:
        path = str(shared / "index.php")
        file_id = made_id(await run(tool, action="device", device="mac", path=path))
        await run(tool, action="send", file=file_id)
        outside = await run(
            tool, action="device", device="mac", path=str(shared / ".." / "secret.txt")
        )
        assert outside.is_error and "outside the allowed folders" in outside.content
        offline = await run(tool, action="device", device="pc", path=path)
        assert offline.is_error and "offline" in offline.content
    finally:
        running.cancel()
    assert sent[0][0].name == "index.php"
    assert sent[0][0].read_bytes() == b"<?php phpinfo(); ?>\x00binary ok"


def test_a_hand_refuses_a_file_too_large(tmp_path: Path) -> None:
    from orion.hands.hand import Refused

    (tmp_path / "big.bin").write_bytes(b"0" * (10 * 1024 * 1024 + 1))
    hand = Hand(HandConfig("http://x", "mac", "s", [str(tmp_path)]))
    with pytest.raises(Refused, match="too large"):
        hand.get_file(str(tmp_path / "big.bin"))


def test_a_file_keeps_its_own_name() -> None:
    from orion.files.module import _safe_name  # pyright: ignore[reportPrivateUsage]

    assert _safe_name("index.php", "application/octet-stream") == "index.php"
    assert _safe_name("../../etc/config.XML", "application/xml") == "config.xml"
    assert _safe_name("Rapport v1.2", "application/pdf") == "Rapport v12.pdf"
    assert _safe_name("photo.jpeg", "image/jpeg") == "photo.jpeg"
    assert _safe_name("image", "image/png") == "image.png"
    assert _safe_name("", "application/pdf") == "fichier.pdf"


async def test_orions_own_data_is_never_sent_from_the_server(tmp_path: Path) -> None:
    from orion.app import build_app
    from orion.config import OrionConfig
    from tests.fakes import ScriptedProvider

    config = OrionConfig.model_validate({"data_dir": tmp_path / "data"})
    async with build_app(config, ScriptedProvider()) as app:
        tool = app.orion.registry.get("file")
        assert tool is not None
        journal = str((tmp_path / "data" / "journal.sqlite3").resolve())
        result = await run(tool, action="server", path=journal)
    assert result.is_error and "never sends from" in result.content


async def test_a_file_being_saved_is_not_taken_for_a_forgotten_one(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    store = FileStore(journal, tmp_path / "files")
    path = tmp_path / "files" / "fi_1" / "devis.pdf"
    path.parent.mkdir(parents=True)
    store._writing.add(path)  # pyright: ignore[reportPrivateUsage]  # as save does first
    path.write_bytes(b"%PDF")
    await store.sync()  # its event is not appended yet
    assert path.exists()


async def test_a_picture_orion_downloads_can_be_looked_at(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    """Photos of a flat on a site: Orion sends them, and can see them first, as content from
    the page they came from."""
    from orion.photos.module import Own, Photos, PhotoSettings, photo_tools
    from tests.fakes import reply

    jpeg = b"\xff\xd8\xff" + b"0" * 100
    client = site(
        {
            "https://annonces.example/p/1.jpg": httpx.Response(
                200, content=jpeg, headers={"content-type": "image/jpeg"}
            ),
            "https://annonces.example/plan.xml": httpx.Response(
                200, content=b"<plan/>", headers={"content-type": "application/xml"}
            ),
        }
    )
    desk = WebDesk(journal, WebSearch(ScriptedProvider(), "m"), client, allow_all)
    await desk.start()
    store = FileStore(journal, tmp_path / "files")
    tool = tool_for(store, [], web=lambda: desk, looks=True)
    try:
        said = await run(tool, action="download", url="https://annonces.example/p/1.jpg")
        other = await run(tool, action="download", url="https://annonces.example/plan.xml")
    finally:
        await desk.aclose()
    file_id = made_id(said)
    assert f"`attachment_look` ({file_id})" in said.content
    assert "attachment_look" not in other.content  # not a picture

    provider = ScriptedProvider(reply("Un séjour lumineux, parquet, deux fenêtres."))
    photos = Photos(journal, provider, PhotoSettings(direct=False), tmp_path / "photos")
    await store.sync()

    def own(given: str) -> Own | None:
        made = store.get(given)
        return Own(made.path, made.content_type, made.trust) if made else None

    photos.own = own
    (look,) = photo_tools(photos)
    seen = await look.handler(
        look.parse(json.dumps({"id": file_id, "question": "Le séjour est lumineux ?"})), CTX
    )
    assert seen.content.startswith("Un séjour lumineux") and seen.trust is Trust.WEB
    assert "image_url" in str(provider.requests[0].messages)
