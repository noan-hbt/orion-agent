"""Files: `[modules.files]` in orion.toml (on by default).

The owner can ask for a file, not only for words: « fais-moi une image de… », « envoie-moi une
capture de la page », « mets ça dans un PDF ». Orion makes it, keeps it under the data directory,
and sends it in the owner's conversation, with a short caption.

- `image`: a picture made by the image model (`agent.image_model`);
- `pdf`: a document from text Orion writes (titles, paragraphs, lists), printed to PDF by the
  same headless Chromium the browser uses;
- a screenshot of the page shown in Orion's browser is `browser` (screenshot);
- `send`: one of these files to the owner. Files go to the owner only: the guardian refuses a
  file for anyone else.

    file.made

Only the file's name, type, size and digest are journaled, never its content. Forgetting the
conversation it was made in redacts its event, and the file goes at the next sweep.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import REDACTED_KIND, EventStore
from orion.core.projections import ProjectionRunner
from orion.modules.base import Daemon, Module, ModuleSettings, Plate
from orion.providers.base import ModelProvider, ModelRequest, ProviderError
from orion.tools.actions import NO_ARGS, Action, action_tool
from orion.tools.base import Effect, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig

log = structlog.get_logger(__name__)

MADE = "file.made"
MAX_BYTES = 10 * 1024 * 1024
"""What Inkbox takes in one message."""
SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}

Deliver = Callable[[Path, str, str | None], Awaitable[None]]
"""(file, caption, correlation id): sends a file to the owner."""


class FileSettings(ModuleSettings):
    enabled: bool = True


@dataclass(frozen=True)
class Made:
    id: str
    name: str
    path: Path
    content_type: str
    size: int
    event_id: str


class _Kept:
    name = "files"

    def __init__(self, folder: Path) -> None:
        self._folder = folder
        self.files: dict[str, Made] = {}
        self._seq = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.files.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        payload: dict[str, Any] = event.payload
        if event.kind == MADE and not event.redacted:
            made = Made(
                str(payload["file_id"]),
                str(payload["name"]),
                self._folder / str(payload["stored"]),
                str(payload["content_type"]),
                int(payload["size"]),
                event.id,
            )
            self.files[made.id] = made
        elif event.kind == REDACTED_KIND:
            gone = set(payload.get("event_ids", []))
            self.files = {k: v for k, v in self.files.items() if v.event_id not in gone}


class FileStore:
    def __init__(self, journal: EventStore, folder: Path) -> None:
        self._journal = journal
        self.folder = folder
        self._kept = _Kept(folder)
        self._runner = ProjectionRunner(journal, [self._kept])

    async def sync(self) -> None:
        """Catch up, and remove the files no event names any more (forgotten)."""
        await self._runner.catch_up()
        if not self.folder.exists():
            return
        names = {m.path.name for m in self._kept.files.values()}
        for path in self.folder.iterdir():
            if path.is_file() and path.name not in names:
                path.unlink()
                log.info("files.forgotten", file=path.name)

    def get(self, file_id: str) -> Made | None:
        return self._kept.files.get(file_id)

    def all(self) -> list[Made]:
        return list(self._kept.files.values())

    async def save(
        self,
        name: str,
        data: bytes,
        content_type: str,
        how: str,
        ctx: ToolContext,
        trust: Trust | None = None,
    ) -> Made:
        if len(data) > MAX_BYTES:
            raise ValueError(f"file too large to send ({len(data)} bytes, 10 MiB at most)")
        file_id = f"fi_{new_id()}"
        stored = file_id + SUFFIXES.get(content_type, "")
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.folder / stored
        await asyncio.to_thread(path.write_bytes, data)
        path.chmod(0o600)
        await self._journal.append(
            [
                NewEvent(
                    kind=MADE,
                    payload={
                        "file_id": file_id,
                        "name": _safe_name(name, content_type),
                        "stored": stored,
                        "content_type": content_type,
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "how": how,
                    },
                    provenance=Provenance(
                        source=f"thread:{ctx.thread_id}",
                        actor="orion:core",
                        derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
                    ),
                    trust=min(ctx.context_trust, trust or ctx.context_trust),
                    correlation_id=ctx.correlation_id,
                )
            ]
        )
        await self._runner.catch_up()
        made = self._kept.files[file_id]
        log.info("files.made", file=file_id, how=how, size=len(data))
        return made


def _safe_name(name: str, content_type: str) -> str:
    stem = re.sub(r"[^\w\- ]+", "", name).strip()[:60] or "fichier"
    return stem + SUFFIXES.get(content_type, "")


def page_html(title: str, text: str) -> str:
    """A plain, readable page from simple markup: `# titles`, paragraphs, `- lists`, **bold**."""

    def inline(line: str) -> str:
        escaped = html.escape(line)
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)

    parts: list[str] = [f"<h1>{html.escape(title)}</h1>"]
    listing = False
    for raw in text.splitlines():
        line = raw.strip()
        is_item = line.startswith(("- ", "* "))
        if listing and not is_item:
            parts.append("</ul>")
            listing = False
        if not line:
            continue
        if line.startswith("#"):
            level = min(len(line) - len(line.lstrip("#")) + 1, 4)
            parts.append(f"<h{level}>{inline(line.lstrip('#').strip())}</h{level}>")
        elif is_item:
            if not listing:
                parts.append("<ul>")
                listing = True
            parts.append(f"<li>{inline(line[2:])}</li>")
        else:
            parts.append(f"<p>{inline(line)}</p>")
    if listing:
        parts.append("</ul>")
    style = (
        "body{font-family:-apple-system,'Helvetica Neue',Arial,sans-serif;font-size:11pt;"
        "line-height:1.5;color:#1c1c1e;margin:0}h1{font-size:20pt;margin:0 0 12pt}"
        "h2{font-size:14pt;margin:16pt 0 6pt}h3,h4{font-size:12pt;margin:12pt 0 4pt}"
        "p{margin:0 0 8pt}ul{margin:0 0 8pt 18pt;padding:0}li{margin:0 0 3pt}"
    )
    return (
        f'<!doctype html><html lang="fr"><head><meta charset="utf-8"><style>{style}</style>'
        f"</head><body>{''.join(parts)}</body></html>"
    )


async def render_pdf(page: str) -> bytes:
    """Print a page with the headless Chromium Orion's browser uses. No network: the page is
    given whole, and every request it would make is refused."""
    from playwright.async_api import Route, async_playwright

    async def refuse(route: Route) -> None:
        await route.abort()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            context = await browser.new_context(java_script_enabled=False)
            await context.route("**/*", refuse)
            tab = await context.new_page()
            await tab.set_content(page, wait_until="domcontentloaded")
            return await tab.pdf(
                format="A4",
                margin={"top": "18mm", "bottom": "18mm", "left": "18mm", "right": "18mm"},
            )
        finally:
            await browser.close()


class ImageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(
        min_length=3, max_length=2000, description="What the picture shows, in detail"
    )
    name: str = Field(default="image", max_length=60, description="A short file name")


class PdfInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    text: str = Field(
        min_length=1,
        max_length=60_000,
        description="The document: paragraphs, '# ' titles, '- ' lists, **bold**",
    )


class SendInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str = Field(description="The file id (fi_…)")
    caption: str = Field(default="", max_length=300, description="A few words to go with it")


Render = Callable[[str], Awaitable[bytes]]


def file_tool(
    store: FileStore,
    provider: ModelProvider,
    image_model: str,
    deliver: Callable[[], Deliver | None],
    render: Render = render_pdf,
) -> Tool:
    async def image(args: Any, ctx: ToolContext) -> ToolResult:
        try:
            response = await provider.complete(
                ModelRequest(
                    model=image_model,
                    messages=[{"role": "user", "content": args.prompt}],
                    session_id="orion:images",
                    image_output=True,
                )
            )
        except ProviderError as exc:
            return ToolResult(f"The picture could not be made: {exc}", is_error=True)
        if not response.images:
            said = response.text.strip()[:300]
            return ToolResult(
                f"The image model made no picture{': ' + said if said else ''}.", is_error=True
            )
        head, _, encoded = response.images[0].partition(",")
        content_type = head.removeprefix("data:").split(";")[0] or "image/png"
        made = await store.save(args.name, base64.b64decode(encoded), content_type, "image", ctx)
        return ToolResult(f"Image prête ({made.id}). Envoie-la avec `file` (send).")

    async def pdf(args: Any, ctx: ToolContext) -> ToolResult:
        data = await render(page_html(args.title, args.text))
        made = await store.save(args.title, data, "application/pdf", "pdf", ctx)
        return ToolResult(
            f"PDF prêt ({made.id}, {len(data) // 1024} Ko). Envoie-le avec `file` (send)."
        )

    async def send(args: Any, ctx: ToolContext) -> ToolResult:
        await store.sync()
        made = store.get(args.file)
        if made is None:
            return ToolResult(f"No file {args.file} (or it was forgotten).", is_error=True)
        to_owner = deliver()
        if to_owner is None:
            return ToolResult(
                f"No channel can carry a file here; it is at {made.path}.", is_error=True
            )
        await to_owner(made.path, args.caption, ctx.correlation_id)
        return ToolResult(f"Envoyé : {made.name}.")

    async def listing(_args: Any, _ctx: ToolContext) -> ToolResult:
        await store.sync()
        files = store.all()[-20:]
        return ToolResult(
            "\n".join(f"- ({m.id}) {m.name}, {m.size // 1024} Ko" for m in files)
            or "Aucun fichier."
        )

    return action_tool(
        "file",
        "Files for the owner, when he asks for one rather than words: make a picture or a PDF, "
        "then send it in his conversation (a screenshot of a page is `browser` (screenshot)).",
        [
            Action("image", "Make a picture from a description.", ImageInput, image),
            Action("pdf", "Make a PDF document from text you write.", PdfInput, pdf),
            Action(
                "send",
                "Send a file to the owner, with a short caption.",
                SendInput,
                send,
                accepts_untrusted_context=True,
            ),
            Action("list", "The files made lately.", NO_ARGS, listing, Effect.NONE, True),
        ],
    )


class FilesModule(Module):
    name = "files"
    instructions = "files.md"
    settings_model = FileSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.store: FileStore | None = None
        self.deliver: Deliver | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        self.store = FileStore(plate.journal, self.config.data_dir / "files")
        await self.store.sync()
        return [
            file_tool(
                self.store, plate.provider, self.config.agent.image_model, lambda: self.deliver
            )
        ]

    async def serve(self, daemon: Daemon) -> None:
        from orion.channels.module import IMessageModule

        imessage = daemon.plate.module(IMessageModule)
        if imessage is not None and imessage.imessage is not None:
            self.deliver = imessage.imessage.deliver_file
