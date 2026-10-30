"""Papers: `[modules.papers]` in orion.toml (on by default).

Identity card, payslips, rent receipts, insurance certificate: what the owner sends as a photo
or a PDF can be filed as a paper, with a label, what it is about, its holder and dates. Filing
copies the kept attachment into the file store, so the paper is a file like any other: sent to
the owner with `file` (send), or to someone else with `mail_send` (`attachments`), which he
approves.

- No list of kinds (D131): a paper is found by searching, by words and by meaning, what Orion
  wrote of it when filing it (« attestation d'assurance habitation MAAF »).
- Before a paper with an end of validity expires, Orion warns him three months, then one month
  ahead.
- « Prépare mon dossier de location » : Orion picks the papers, in the order a landlord
  expects; one PDF, with a cover page listing what is in and what is missing.

Only what identifies a paper is journaled (label, what it is about, holder, dates), never its
numbers. Papers filed before D131 carried a kind, read as what they are about.
The fields come from a document an administration or an employer wrote, read by a model: they
carry `Trust.THIRD_PARTY`. Forgetting the file (redacting its `file.made`) drops the paper.

    paper.filed  paper.removed
"""

from __future__ import annotations

import asyncio
import io
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import numpy as np
import structlog
from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.initiative.engine import Alert
from orion.modules.base import Daemon, Module, ModuleSettings, Planned, Plate
from orion.providers.base import ProviderError
from orion.tools.actions import Action, action_tool
from orion.tools.base import Effect, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.files.module import FileStore
    from orion.photos.module import Photos

log = structlog.get_logger(__name__)

FILED = "paper.filed"
REMOVED = "paper.removed"

WARN_DAYS = (90, 30)
RRF_K = 60
MIN_SIMILARITY = 0.25

Render = Callable[[str], Awaitable[bytes]]
Vectors = Callable[[Sequence[str]], Awaitable[dict[str, Any]]]


class PaperSettings(ModuleSettings):
    enabled: bool = True


@dataclass(frozen=True)
class Paper:
    id: str
    file_id: str
    label: str
    about: str
    holder: str
    issued: date | None
    valid_until: date | None
    filed_at: datetime

    def describe(self) -> str:
        parts = [f"({self.id}) {self.label}"]
        if self.about:
            parts.append(f"({self.about})")
        if self.holder:
            parts.append(f"de {self.holder}")
        if self.issued:
            parts.append(f"du {self.issued:%d/%m/%Y}")
        if self.valid_until:
            parts.append(f"valable jusqu'au {self.valid_until:%d/%m/%Y}")
        return " ".join(parts) + f" [fichier {self.file_id}]"

    @property
    def when(self) -> date:
        return self.issued or self.filed_at.date()

    @property
    def searched(self) -> str:
        """What a search looks through: what Orion wrote of it when filing it."""
        dates = " ".join(
            f"{d:%d/%m/%Y} {d:%m/%Y} {d:%Y}" for d in (self.issued, self.valid_until) if d
        )
        return " ".join(p for p in (self.label, self.about, self.holder, dates) if p)


class PaperState:
    name = "papers"

    def __init__(self) -> None:
        self.papers: dict[str, Paper] = {}
        self._seq = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.papers.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind == FILED:
            paper = Paper(
                id=str(payload["paper_id"]),
                file_id=str(payload["file_id"]),
                label=str(payload["label"]),
                about=_about(payload),
                holder=str(payload.get("holder", "")),
                issued=_date(payload.get("issued")),
                valid_until=_date(payload.get("valid_until")),
                filed_at=datetime.fromisoformat(str(payload["filed_at"])),
            )
            self.papers[paper.id] = paper
        elif event.kind == REMOVED:
            self.papers.pop(str(payload["paper_id"]), None)


def _date(value: object) -> date | None:
    return date.fromisoformat(str(value)) if value else None


def _about(payload: dict[str, Any]) -> str:
    """What a paper is about; before D131, its kind from a fixed list (« autre »: nothing)."""
    if "about" in payload:
        return str(payload["about"])
    kind = str(payload.get("kind", ""))
    return "" if kind == "autre" else kind


def _words(text: str) -> list[str]:
    """Lowercase words without accents: « Fiche de paie » finds « fiche de PAIE »."""
    plain = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(c if c.isalnum() else " " for c in plain if not unicodedata.combining(c))
    return [w for w in plain.split() if len(w) > 1]


class Vault:
    def __init__(
        self,
        journal: EventStore,
        store: FileStore,
        photos: Photos | None,
        timezone: str,
        render: Render | None = None,
        vectors: Vectors | None = None,
    ) -> None:
        self._journal = journal
        self.store = store
        self._photos = photos
        self.zone = ZoneInfo(timezone)
        self._render = render
        self._vectors = vectors
        self.state = PaperState()
        self._runner = ProjectionRunner(journal, [self.state])

    async def sync(self) -> None:
        await self._runner.catch_up()
        await self.store.sync()

    def papers(self) -> list[Paper]:
        """Filed papers whose file is still kept, newest first."""
        kept = [p for p in self.state.papers.values() if self.store.get(p.file_id) is not None]
        return sorted(kept, key=lambda p: p.when, reverse=True)

    async def file(
        self,
        attachment: str,
        label: str,
        about: str,
        holder: str,
        issued: date | None,
        valid_until: date | None,
        ctx: ToolContext,
    ) -> Paper:
        if self._photos is None:
            raise LookupError("attachments are not kept (the photos module is off)")
        found = await self._photos.file(attachment)
        if found is None:
            raise LookupError(f"no attachment {attachment}")
        data, content_type, stored = found
        suffix = stored.rsplit(".", 1)[-1] if "." in stored else ""
        name = f"{label}.{suffix}" if suffix else label
        made = await self.store.save(name, data, content_type, "paper", ctx, Trust.THIRD_PARTY)
        paper_id = f"pa_{new_id()}"
        await self._journal.append(
            [
                NewEvent(
                    kind=FILED,
                    payload={
                        "paper_id": paper_id,
                        "file_id": made.id,
                        "attachment_id": attachment,
                        "label": label,
                        "about": about,
                        "holder": holder,
                        "issued": issued.isoformat() if issued else None,
                        "valid_until": valid_until.isoformat() if valid_until else None,
                        "filed_at": datetime.now(self.zone).isoformat(),
                    },
                    provenance=_origin(ctx),
                    trust=Trust.THIRD_PARTY,
                    correlation_id=ctx.correlation_id,
                )
            ]
        )
        await self.sync()
        return self.state.papers[paper_id]

    async def remove(self, paper: Paper, ctx: ToolContext) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=REMOVED,
                    payload={"paper_id": paper.id},
                    provenance=_origin(ctx),
                    trust=Trust.OWNER,
                    correlation_id=ctx.correlation_id,
                )
            ]
        )
        await self.sync()

    # -- search ---------------------------------------------------------------------------

    async def search(self, query: str, limit: int = 40) -> list[Paper]:
        """By words, and by meaning when embeddings are available (fused by rank, as the
        library does); an empty query gives them all, newest first."""
        papers = self.papers()
        wanted = _words(query)
        if not wanted:
            return papers[:limit]
        scored = [(sum(w in _words(p.searched) for w in wanted), p) for p in papers]
        lexical = [p for n, p in sorted(scored, key=lambda s: -s[0]) if n]
        semantic: list[Paper] = []
        if self._vectors is not None and papers:
            try:
                vectors = await self._vectors([query, *(p.searched for p in papers)])
            except ProviderError as exc:
                log.warning("papers.semantic_unavailable", error=str(exc))
            else:
                near = sorted(
                    ((float(np.dot(vectors[query], vectors[p.searched])), p) for p in papers),
                    key=lambda s: -s[0],
                )
                semantic = [p for score, p in near if score >= MIN_SIMILARITY]
        ranks: dict[str, float] = {}
        for ranking in (lexical, semantic):
            for rank, paper in enumerate(ranking):
                ranks[paper.id] = ranks.get(paper.id, 0.0) + 1.0 / (RRF_K + rank)
        by_id = {p.id: p for p in papers}
        return [by_id[i] for i in sorted(ranks, key=lambda i: -ranks[i])][:limit]

    async def dossier(self, title: str, papers: list[Paper], missing: list[str]) -> bytes:
        from orion.files.module import page_html, render_pdf

        lines = [f"- {p.label}" for p in papers]
        if missing:
            lines += ["", "## Manque encore", *[f"- {m}" for m in missing]]
        cover = await (self._render or render_pdf)(page_html(title, "\n".join(lines)))
        parts = [cover]
        for paper in papers:
            made = self.store.get(paper.file_id)
            if made is not None:
                parts.append(
                    await asyncio.to_thread(_as_pdf, made.path.read_bytes(), made.content_type)
                )
        return await asyncio.to_thread(_merge, parts)

    # -- initiative ---------------------------------------------------------------------------

    async def alerts(self, now: datetime) -> list[Alert]:
        await self.sync()
        today = now.astimezone(self.zone).date()
        found: list[Alert] = []
        for paper in self.papers():
            if paper.valid_until is None:
                continue
            left = (paper.valid_until - today).days
            if left < 0:
                continue
            days = next((d for d in sorted(WARN_DAYS) if left <= d), None)
            if days is None:
                continue
            found.append(
                Alert(
                    key=f"paper:{paper.id}:{days}",
                    notice=f"Son papier expire bientôt : {paper.describe()}. Il reste {left} "
                    "jours ; propose de l'aider à le renouveler (la démarche, le rendez-vous).",
                    why=f"{paper.label} expire le {paper.valid_until:%d/%m/%Y}",
                    fallback=f"{paper.label} : expire le {paper.valid_until:%d/%m/%Y}.",
                    trust=Trust.THIRD_PARTY,
                    expires_at=datetime.combine(paper.valid_until, datetime.min.time(), self.zone),
                    importance=0.6,  # weeks ahead: it waits for a point (D144)
                )
            )
        return found


def _as_pdf(data: bytes, content_type: str) -> bytes:
    if content_type == "application/pdf":
        return data
    from PIL import Image

    from orion.photos.module import to_jpeg

    image_data = to_jpeg(data) if content_type in ("image/heic", "image/heif") else data
    with Image.open(io.BytesIO(image_data)) as image:
        out = io.BytesIO()
        image.convert("RGB").save(out, "PDF", resolution=150)
    return out.getvalue()


def _merge(parts: list[bytes]) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for part in parts:
        for page in PdfReader(io.BytesIO(part)).pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _origin(ctx: ToolContext) -> Provenance:
    return Provenance(
        source=f"thread:{ctx.thread_id}",
        actor="orion:core",
        derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
    )


class FileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attachment: str = Field(description="The attachment id (at_… or ph_…) he sent")
    label: str = Field(
        min_length=2, max_length=80, description="« Fiche de paie septembre 2026 », « Passeport »"
    )
    about: str = Field(
        default="",
        max_length=300,
        description="What it is, in the words a later search would use; never its numbers",
    )
    holder: str = Field(default="", max_length=80, description="Whose paper, if not his")
    issued: date | None = Field(default=None, description="Issue date or period start, ISO")
    valid_until: date | None = Field(default=None, description="End of validity, ISO")


class FindInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        default="",
        max_length=200,
        description="Its words (« assurance habitation »); empty: all",
    )


class RemoveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper: str = Field(description="The paper id (pa_…)")


class DossierInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    papers: list[str] = Field(
        min_length=1, max_length=20, description="Paper ids, in the order they go in"
    )
    title: str = Field(default="Dossier", min_length=2, max_length=80)
    missing: list[str] = Field(
        default_factory=list[str],
        max_length=12,
        description="What the dossier should hold and he has not given you, for the cover page",
    )


def paper_tools(vault: Vault) -> list[Tool]:
    async def file(args: Any, ctx: ToolContext) -> ToolResult:
        try:
            paper = await vault.file(
                args.attachment,
                args.label,
                args.about,
                args.holder,
                args.issued,
                args.valid_until,
                ctx,
            )
        except (LookupError, ValueError) as exc:
            return ToolResult(f"Not filed: {exc}", is_error=True)
        warn = " Je le préviendrai avant qu'il expire." if paper.valid_until else ""
        return ToolResult(f"Rangé dans ses papiers : {paper.describe()}.{warn}")

    async def find(args: Any, _ctx: ToolContext) -> ToolResult:
        await vault.sync()
        found = await vault.search(args.query)
        if not found:
            return ToolResult(
                "Aucun papier correspondant." if vault.papers() else "Aucun papier rangé."
            )
        return ToolResult(
            "Ses papiers (envoie le fichier avec `file` (send), ou en pièce jointe de "
            "`mail_send`) :\n" + "\n".join(f"- {p.describe()}" for p in found[:40]),
            trust=Trust.THIRD_PARTY,
        )

    async def remove(args: Any, ctx: ToolContext) -> ToolResult:
        await vault.sync()
        paper = vault.state.papers.get(args.paper)
        if paper is None:
            return ToolResult(f"No paper {args.paper}.", is_error=True)
        await vault.remove(paper, ctx)
        return ToolResult(f"Retiré de ses papiers : {paper.label} (le fichier reste).")

    async def dossier(args: Any, ctx: ToolContext) -> ToolResult:
        await vault.sync()
        unknown = [p for p in args.papers if p not in vault.state.papers]
        if unknown:
            return ToolResult(
                f"Pas de papier {', '.join(unknown)} : cherche-les avec `papers` (find).",
                is_error=True,
            )
        papers = [vault.state.papers[p] for p in args.papers]
        title, missing = args.title, list(args.missing)
        try:
            data = await vault.dossier(title, papers, missing)
        except (OSError, ValueError) as exc:
            return ToolResult(f"Le dossier n'a pas pu être fait : {exc}", is_error=True)
        made = await vault.store.save(
            f"{title}.pdf", data, "application/pdf", "dossier", ctx, Trust.THIRD_PARTY
        )
        lacking = f" Il manque : {', '.join(missing)}." if missing else " Rien ne manque."
        return ToolResult(
            f"Dossier prêt ({made.id}, {len(papers)} papiers).{lacking} Envoie-le-lui avec "
            "`file` (send), ou à l'agence avec `mail_send`.",
            data={"file_id": made.id},
        )

    return [
        action_tool(
            "papers",
            "The owner's papers (ID, payslips, receipts, certificates), kept as files.",
            [
                Action(
                    "file",
                    "File an attachment he sent as a paper, once you know what it is.",
                    FileInput,
                    file,
                    Effect.INTERNAL,
                    accepts_untrusted_context=True,
                ),
                Action(
                    "find",
                    "Search his papers by words and meaning (empty: all, newest first), with "
                    "their file ids.",
                    FindInput,
                    find,
                    Effect.NONE,
                    accepts_untrusted_context=True,
                ),
                Action("remove", "Take a paper out of the vault.", RemoveInput, remove),
                Action(
                    "dossier",
                    "One PDF of the papers you pick, in order (a rental file…), with a cover "
                    "page listing them and what is missing.",
                    DossierInput,
                    dossier,
                    Effect.INTERNAL,
                    accepts_untrusted_context=True,
                ),
            ],
        )
    ]


class PapersModule(Module):
    name = "papers"
    requires = ("files", "photos")
    instructions = "papers.md"
    settings_model = PaperSettings

    def __init__(self, config: OrionConfig, render: Render | None = None) -> None:
        super().__init__(config)
        self._render = render
        self.vault: Vault | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        from orion.files.module import FilesModule
        from orion.photos.module import PhotosModule

        files = plate.module(FilesModule)
        photos = plate.module(PhotosModule)
        if files is None or files.store is None:  # pragma: no cover - required module
            return []
        self.vault = Vault(
            plate.journal,
            files.store,
            photos.photos if photos is not None else None,
            self.config.agent.timezone,
            self._render,
            plate.memory.semantic.vectors if plate.memory.semantic is not None else None,
        )
        await self.vault.sync()
        return paper_tools(self.vault)

    async def serve(self, daemon: Daemon) -> None:
        if self.vault is not None:
            daemon.alerts.append(self.vault)

    async def review(self, now: datetime) -> list[str]:
        vault = self.vault
        if vault is None:
            return []
        await vault.sync()
        return [f"- {p.describe()}" for p in vault.state.papers.values()]

    async def planned(self, now: datetime, until: datetime) -> list[Planned]:
        vault = self.vault
        if vault is None:
            return []
        await vault.sync()
        found: list[Planned] = []
        for paper in vault.papers():
            if paper.valid_until is None:
                continue
            for days in WARN_DAYS:
                warn = paper.valid_until - timedelta(days=days)
                at = datetime.combine(warn, time(9, 0), vault.zone)
                if now <= at < until:
                    found.append(
                        Planned(
                            at,
                            f"te prévenir que {paper.label} expire le {paper.valid_until:%d/%m/%Y}",
                        )
                    )
        return found
