"""Papers: `[modules.papers]` in orion.toml (on by default).

Identity card, payslips, rent receipts, insurance certificate: what the owner sends as a photo
or a PDF can be filed as a paper, with its kind, holder and dates. Filing copies the kept
attachment into the file store, so the paper is a file like any other: sent to the owner with
`file` (send), or to someone else with `mail_send` (`attachments`), which he approves.

- Before a paper expires (passport, ID, residence permit, licence, insurance), Orion warns him
  three months, then one month ahead.
- « Prépare mon dossier de location » : one PDF, a cover page listing what is in and what is
  missing, then the papers in the order a landlord expects.

Only what identifies a paper is journaled (kind, holder, dates, a label), never its numbers.
The fields come from a document an administration or an employer wrote, read by a model: they
carry `Trust.THIRD_PARTY`. Forgetting the file (redacting its `file.made`) drops the paper.

    paper.filed  paper.removed
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

import structlog
from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.initiative.engine import Alert
from orion.modules.base import Daemon, Module, ModuleSettings, Plate
from orion.tools.actions import Action, action_tool
from orion.tools.base import Effect, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.files.module import FileStore
    from orion.photos.module import Photos

log = structlog.get_logger(__name__)

FILED = "paper.filed"
REMOVED = "paper.removed"

Kind = Literal[
    "carte d'identité",
    "passeport",
    "titre de séjour",
    "permis de conduire",
    "fiche de paie",
    "avis d'imposition",
    "quittance de loyer",
    "justificatif de domicile",
    "attestation d'assurance",
    "contrat de travail",
    "attestation employeur",
    "rib",
    "diplôme",
    "autre",
]
EXPIRING: frozenset[str] = frozenset(
    {
        "carte d'identité",
        "passeport",
        "titre de séjour",
        "permis de conduire",
        "attestation d'assurance",
    }
)
WARN_DAYS = (90, 30)
IDENTITY = ("carte d'identité", "passeport", "titre de séjour")
RENTAL: tuple[tuple[str, tuple[str, ...], int], ...] = (
    ("Pièce d'identité", IDENTITY, 1),
    ("Trois dernières fiches de paie", ("fiche de paie",), 3),
    (
        "Contrat de travail ou attestation employeur",
        ("contrat de travail", "attestation employeur"),
        1,
    ),
    ("Dernier avis d'imposition", ("avis d'imposition",), 1),
    ("Justificatif de domicile", ("quittance de loyer", "justificatif de domicile"), 3),
    ("RIB", ("rib",), 1),
)
"""(section, kinds that fill it, how many, newest first)"""

Render = Callable[[str], Awaitable[bytes]]


class PaperSettings(ModuleSettings):
    enabled: bool = True


@dataclass(frozen=True)
class Paper:
    id: str
    file_id: str
    kind: str
    label: str
    holder: str
    issued: date | None
    valid_until: date | None
    filed_at: datetime

    def describe(self) -> str:
        parts = [f"({self.id}) {self.kind} : {self.label}"]
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
                kind=str(payload["kind"]),
                label=str(payload["label"]),
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


class Vault:
    def __init__(
        self,
        journal: EventStore,
        store: FileStore,
        photos: Photos | None,
        timezone: str,
        render: Render | None = None,
    ) -> None:
        self._journal = journal
        self.store = store
        self._photos = photos
        self.zone = ZoneInfo(timezone)
        self._render = render
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
        kind: str,
        label: str,
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
                        "kind": kind,
                        "label": label,
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

    # -- the rental file ---------------------------------------------------------------------

    def rental(self) -> tuple[list[tuple[str, list[Paper]]], list[str]]:
        """Each section with its papers, and the sections nothing fills."""
        papers = self.papers()
        sections: list[tuple[str, list[Paper]]] = []
        missing: list[str] = []
        for title, kinds, count in RENTAL:
            found = [p for p in papers if p.kind in kinds][:count]
            sections.append((title, found))
            if not found:
                missing.append(title)
            elif len(found) < count:
                missing.append(f"{title} ({len(found)} sur {count})")
        return sections, missing

    async def dossier(self, title: str, papers: list[Paper], missing: list[str]) -> bytes:
        from orion.files.module import page_html, render_pdf

        lines = [f"- {p.kind} : {p.label}" for p in papers]
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
            if paper.kind not in EXPIRING or paper.valid_until is None:
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
                    why=f"{paper.kind} expire le {paper.valid_until:%d/%m/%Y}",
                    fallback=f"Ton {paper.kind} expire le {paper.valid_until:%d/%m/%Y}.",
                    trust=Trust.THIRD_PARTY,
                    expires_at=datetime.combine(paper.valid_until, datetime.min.time(), self.zone),
                    importance=0.6 if days == WARN_DAYS[0] else 0.75,
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
    kind: Kind
    label: str = Field(
        min_length=2, max_length=80, description="« Fiche de paie septembre 2026 », « Passeport »"
    )
    holder: str = Field(default="", max_length=80, description="Whose paper, if not his")
    issued: date | None = Field(default=None, description="Issue date or period start, ISO")
    valid_until: date | None = Field(default=None, description="Expiry date, ISO")


class FindInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind | None = None
    text: str = Field(default="", max_length=80, description="Words of the label or holder")


class RemoveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper: str = Field(description="The paper id (pa_…)")


class DossierInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: Literal["location", "papiers choisis"] = "location"
    papers: list[str] = Field(
        default_factory=list[str], max_length=20, description="Paper ids, for « papiers choisis »"
    )
    title: str = Field(default="", max_length=80)


def paper_tools(vault: Vault) -> list[Tool]:
    async def file(args: Any, ctx: ToolContext) -> ToolResult:
        try:
            paper = await vault.file(
                args.attachment,
                args.kind,
                args.label,
                args.holder,
                args.issued,
                args.valid_until,
                ctx,
            )
        except (LookupError, ValueError) as exc:
            return ToolResult(f"Not filed: {exc}", is_error=True)
        warn = (
            " Je le préviendrai avant qu'il expire."
            if paper.kind in EXPIRING and paper.valid_until
            else ""
        )
        return ToolResult(f"Rangé dans ses papiers : {paper.describe()}.{warn}")

    async def find(args: Any, _ctx: ToolContext) -> ToolResult:
        await vault.sync()
        needle = args.text.lower()
        found = [
            p
            for p in vault.papers()
            if (args.kind is None or p.kind == args.kind)
            and (not needle or needle in f"{p.label} {p.holder}".lower())
        ]
        if not found:
            return ToolResult("Aucun papier correspondant.")
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
        if args.purpose == "location":
            sections, missing = vault.rental()
            papers = [p for _, found in sections for p in found]
            title = args.title or "Dossier de location"
        else:
            papers = [vault.state.papers[p] for p in args.papers if p in vault.state.papers]
            missing = []
            title = args.title or "Dossier"
        if not papers:
            return ToolResult(
                "Aucun papier pour ce dossier : demande-lui de t'envoyer ses papiers.",
                is_error=True,
            )
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
                    "His papers, by kind or words (newest first), with their file ids.",
                    FindInput,
                    find,
                    Effect.NONE,
                    accepts_untrusted_context=True,
                ),
                Action("remove", "Take a paper out of the vault.", RemoveInput, remove),
                Action(
                    "dossier",
                    "One PDF of several papers: a rental file (location), or the papers "
                    "you choose; the cover page says what is missing.",
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
        )
        await self.vault.sync()
        return paper_tools(self.vault)

    async def serve(self, daemon: Daemon) -> None:
        if self.vault is not None:
            daemon.alerts.append(self.vault)
