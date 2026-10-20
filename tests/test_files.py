"""Files Orion makes and sends: pictures, PDFs, screenshots, to the owner only."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from orion.channels.imessage import IMessageChannel
from orion.channels.inbox import OwnerInbox
from orion.computer.direct import BrowserDesk, browser_tools
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.files.module import MADE, FileStore, file_tool, page_html, render_pdf
from orion.guardian.client import GuardianUnavailableError
from orion.tools.base import Tool, ToolContext, ToolResult
from tests.fake_guardian import guardian_config, running_guardian
from tests.fake_inkbox import CONVERSATION, FakeInkbox
from tests.fakes import Script, ScriptedProvider
from tests.test_browser_direct import owner_says
from tests.test_computer import Site, session
from tests.test_imessage import SETTINGS, make_orion
from tests.test_secretary import MUM, MUM_CHAT
from tests.test_secretary import SETTINGS as SECRETARY

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

    await run(tool, action="send", file=image_id, caption="Ton chat")
    await run(tool, action="send", file=pdf_id)
    assert [(p.read_bytes()[:4], c) for p, c in sent] == [(b"\x89PNG", "Ton chat"), (b"%PDF", "")]
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
    tools = {t.name: t for t in browser_tools(desk, keep)}
    try:
        nothing = await run(tools["browser_screenshot"])
        assert nothing.is_error
        await owner_says(journal, "Montre-moi https://annonces.test/", "c")
        await run(tools["browser_open"], url="https://annonces.test/")
        shot = await run(tools["browser_screenshot"])
        file_id = shot.content.split("(")[1].split(")")[0]
        made = store.get(file_id)
        assert made is not None and made.path.read_bytes().startswith(b"\x89PNG")
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
