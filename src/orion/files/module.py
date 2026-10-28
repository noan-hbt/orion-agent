"""Files: `[modules.files]` in orion.toml (on by default).

The owner can ask for a file, not only for words: « fais-moi une image de… », « envoie-moi une
capture de la page », « mets ça dans un PDF », « télécharge le XML de leur config », « envoie-moi
le index.php de mon dossier site ». Orion makes or fetches it, keeps it under the data directory,
and sends it in the owner's conversation, alone: no caption. Any type, 10 MiB at most.

- `image`: a picture made by the image model (`agent.image_model`);
- `pdf`: a document from text Orion writes (titles, paragraphs, lists), printed to PDF by the
  same headless Chromium the browser uses;
- a screenshot of the page shown in Orion's browser is `browser` (screenshot);
- `download`: a file on the web, byte for byte, at any web address
  (the web module's rule: a made-up address could carry data out);
- `server`: a file on Orion's own server. Not the guardian's folder, not `/proc`, `/sys`,
  `/dev`, not an `.env` file: those hold secrets, and a page could ask for them. In a context
  holding untrusted content the owner approves it first;
- `device`: a file from a folder the owner shared on one of his machines (`orion hand`,
  `get_file`);
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
import mimetypes
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
from orion.core.private import write_private
from orion.core.projections import ProjectionRunner
from orion.modules.base import Daemon, Module, ModuleSettings, Plate
from orion.providers.base import ModelProvider, ModelRequest, ProviderError
from orion.tools.actions import NO_ARGS, Action, action_tool
from orion.tools.base import Effect, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.hands.desk import HandsDesk
    from orion.web.service import WebDesk

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

SYSTEM_DIRS = (Path("/proc"), Path("/sys"), Path("/dev"))
"""Never sent: `/proc/self/environ` alone holds the model key."""

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
    trust: Trust = Trust.SYSTEM
    """The trust of what it came from: a picture downloaded from a page is worth that page."""


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
                event.trust,
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
        self._writing: set[Path] = set()
        """Files written whose event is not appended yet."""

    async def sync(self) -> None:
        """Catch up, and remove the files no event names any more (forgotten)."""
        await self._runner.catch_up()
        if not self.folder.exists():
            return
        kept = {m.path for m in self._kept.files.values()}
        self._writing -= kept
        for path in sorted(self.folder.rglob("*"), reverse=True):
            # Just written, its event not appended yet: being kept, not forgotten.
            if path.is_file() and path not in kept and path not in self._writing:
                path.unlink()
                log.info("files.forgotten", file=path.relative_to(self.folder).as_posix())
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()

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
        # One folder per file, so it travels under its own name (« config.xml », not an id).
        named = _safe_name(name, content_type)
        stored = f"{file_id}/{named}"
        path = self.folder / stored
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._writing.add(path)  # until MADE names it
        await asyncio.to_thread(write_private, path, data)
        await self._journal.append(
            [
                NewEvent(
                    kind=MADE,
                    payload={
                        "file_id": file_id,
                        "name": named,
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
    """A plain file name. A file got as it is keeps its own extension (`index.php`); a file Orion
    made gets its type's (« Rapport v1.2 » is `Rapport v12.pdf`)."""
    given = Path(name.replace("\\", "/")).name
    stem, dot, suffix = given.rpartition(".")
    made = SUFFIXES.get(content_type)
    own = dot and stem and re.fullmatch(r"[A-Za-z0-9]{1,10}", suffix)
    if own and (made is None or f".{suffix.lower()}" in (made, ".jpeg")):
        return f"{_plain(stem)}.{suffix.lower()}"
    return _plain(given) + (made or mimetypes.guess_extension(content_type) or "")


def _plain(stem: str) -> str:
    return re.sub(r"[^\w\- ]+", "", stem).strip()[:60] or "fichier"


def _content_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


class Unsendable(ValueError):
    """A server file Orion may not or cannot send."""


SECRET_NAMES = (".netrc", ".pgpass", "id_rsa", "id_ecdsa", "id_ed25519", "credentials.json")
SECRET_SUFFIXES = (".env", ".pem", ".key", ".p12", ".pfx")


def _secret_file(name: str) -> bool:
    """`.env`, `.env.production`, `prod.env`, a private key…: never sent, wherever they are."""
    lowered = name.lower()
    return (
        lowered in SECRET_NAMES or lowered.startswith(".env") or lowered.endswith(SECRET_SUFFIXES)
    )


def read_server_file(given: str, closed: tuple[Path, ...]) -> tuple[Path, bytes]:
    """A file on Orion's server, resolved (links followed) before it is checked."""
    path = Path(given).expanduser()
    if not path.is_absolute():
        raise Unsendable("Give an absolute path.")
    path = path.resolve()
    shut = (*SYSTEM_DIRS, *(c.resolve() for c in closed))
    if any(path == d or d in path.parents for d in shut):
        raise Unsendable(f"{path} is in a folder Orion never sends from (secrets).")
    if _secret_file(path.name):
        raise Unsendable(f"{path} holds secrets.")
    try:
        if not path.is_file():
            raise Unsendable(f"{path} is not a file.")
        size = path.stat().st_size
        if size > MAX_BYTES:
            raise Unsendable(f"{path} is too large to send ({size // 1_048_576} MiB, 10 at most).")
        return path, path.read_bytes()
    except OSError as exc:
        raise Unsendable(f"{path} cannot be read: {exc.strerror or exc}") from exc


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


class DownloadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(pattern=r"^https?://", description="The file's address")
    name: str = Field(default="", max_length=80, description="A file name, if the address has none")


class ServerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1000, description="Absolute path on your server")


class DeviceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device: str
    path: str = Field(description="A path `hand_find_files` returned")


Render = Callable[[str], Awaitable[bytes]]


def file_tool(
    store: FileStore,
    provider: ModelProvider,
    image_model: str,
    deliver: Callable[[], Deliver | None],
    render: Render = render_pdf,
    *,
    web: Callable[[], WebDesk | None] = lambda: None,
    hands: Callable[[], HandsDesk | None] = lambda: None,
    closed: tuple[Path, ...] = (),
    looks: bool = False,
) -> Tool:
    """`looks`: `attachment_look` can look at a picture (the photos module is on)."""

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

    def ready(made: Made) -> ToolResult:
        look = (
            f" Tu ne la vois pas : `attachment_look` ({made.id}) te dit ce qu'elle montre."
            if looks and made.content_type.startswith("image/")
            else ""
        )
        return ToolResult(
            f"Fichier prêt ({made.id}, {made.name}, {max(made.size // 1024, 1)} Ko). "
            f"Envoie-le avec `file` (send).{look}"
        )

    async def downloading(args: Any, ctx: ToolContext) -> ToolResult:
        from urllib.parse import unquote, urlsplit

        from orion.web.service import FetchError, NotAllowedError

        desk = web()
        if desk is None:
            return ToolResult("The web module is off: nothing can be downloaded.", is_error=True)
        try:
            got = await desk.download(args.url, MAX_BYTES, ctx.correlation_id)
        except (FetchError, NotAllowedError) as exc:
            return ToolResult(str(exc), is_error=True)
        name = args.name or unquote(urlsplit(got.url).path.rsplit("/", 1)[-1]) or "fichier"
        kind = got.content_type or _content_type(name)
        made = await store.save(name, got.data, kind, "download", ctx, Trust.WEB)
        return ready(made)

    async def from_server(args: Any, ctx: ToolContext) -> ToolResult:
        try:
            path, data = await asyncio.to_thread(read_server_file, args.path, closed)
        except Unsendable as exc:
            return ToolResult(str(exc), is_error=True)
        made = await store.save(
            path.name, data, _content_type(path.name), "server", ctx, Trust.OWNER_INSTANCE
        )
        return ready(made)

    async def from_device(args: Any, ctx: ToolContext) -> ToolResult:
        from orion.hands.desk import HandError

        desk = hands()
        if desk is None:
            return ToolResult(
                "No hand is set up (he adds a machine in the app: Réglages > Machines).",
                is_error=True,
            )
        try:
            got = await desk.call(args.device, "get_file", {"path": args.path})
            data = base64.b64decode(str(got["data"]), validate=True)
        except HandError as exc:
            return ToolResult(str(exc), is_error=True)
        except (KeyError, ValueError) as exc:
            log.warning("files.bad_hand_reply", device=args.device, error=repr(exc))
            return ToolResult(f"{args.device} sent back something unreadable.", is_error=True)
        name = str(got.get("name") or Path(args.path).name)
        made = await store.save(
            name, data, _content_type(name), "device", ctx, Trust.OWNER_INSTANCE
        )
        return ready(made)

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
        await to_owner(made.path, "", ctx.correlation_id)
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
        "or get any file (from the web, your server, or one of his machines), then send it in "
        "his conversation (a screenshot of a page is `browser` (screenshot)).",
        [
            Action("image", "Make a picture from a description.", ImageInput, image),
            Action("pdf", "Make a PDF document from text you write.", PdfInput, pdf),
            Action(
                "download",
                "Get a file from the web as it is (any type), at its direct address.",
                DownloadInput,
                downloading,
                accepts_untrusted_context=True,
            ),
            Action(
                "server", "Get a file from your own server, by its path.", ServerInput, from_server
            ),
            Action(
                "device",
                "Get a file from a folder he shared on one of his machines (`hands`).",
                DeviceInput,
                from_device,
                accepts_untrusted_context=True,
            ),
            Action(
                "send",
                "Send a file to the owner, as it is: no caption.",
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
        from orion.hands.module import HandsModule
        from orion.web.module import WebModule

        web = plate.module(WebModule)
        hands = plate.module(HandsModule)
        guardian = self.config.guardian
        return [
            file_tool(
                self.store,
                plate.provider,
                self.config.agent.image_model,
                lambda: self.deliver,
                web=lambda: web.desk if web is not None else None,
                hands=lambda: hands.desk if hands is not None else None,
                # Orion's own data too: its journal, its memory, its ChatGPT tokens.
                closed=(self.config.data_dir, guardian.data_dir, guardian.socket.parent),
                looks=plate.has("photos"),
            )
        ]

    async def serve(self, daemon: Daemon) -> None:
        self.deliver = daemon.inbox.deliver_file  # whichever channel carries files (D88)
