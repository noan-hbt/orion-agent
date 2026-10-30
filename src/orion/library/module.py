"""The library as a module: `[modules.library]` in orion.toml (on by default).

Orion's memory holds facts drawn from conversations; the library holds what the owner gives it
to keep whole (notes, documents, a page worth keeping) and the conversation itself, both
searchable by words and by meaning:

- `library_add`: keep a text (pasted, read from the owner's machine, a page);
- `library_search`: passages that answer a question, with their document and date;
- `conversations_search`: what was said with the owner, about something, between two dates;
- `library` (keep): a link sent « for later » (D67), read, kept, and noted in memory with its
  gist and topics, so it comes back by itself the day the conversation touches it (memory
  recall), instead of piling up in a to-read list nobody opens.

A document is content, not orders: its passages come back below full trust, like a file read
from the owner's machine. Notes are added at once from the app (Réglages > Bibliothèque, D154)
or with `orion library import <folder>`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from zoneinfo import ZoneInfo

import numpy as np
import structlog
from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.library.store import ADDED, REMOVED, LibraryStore, Passage
from orion.memory.model import MAX_STATEMENT, Category
from orion.memory.service import MemoryService, Origin
from orion.modules.base import Module, ModuleSettings, Plate
from orion.providers.base import ProviderError
from orion.tools.actions import grouped
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig

log = structlog.get_logger(__name__)

DOCUMENT_TRUST = Trust.OWNER_INSTANCE
"""Like a file read from the owner's machine: theirs, but content, not orders."""
RRF_K = 60
MIN_SIMILARITY = 0.25
MAX_DOC_CHARS = 400_000
SUFFIXES = (".md", ".txt", ".markdown", ".org", ".rst")

Vectors = Callable[[Sequence[str]], Awaitable[dict[str, Any]]]


class LibrarySettings(ModuleSettings):
    enabled: bool = True
    results: int = Field(default=6, ge=1, le=20)


class Library:
    def __init__(self, journal: EventStore, store: LibraryStore, vectors: Vectors | None) -> None:
        self._journal = journal
        self.store = store
        self._vectors = vectors
        self._runner = ProjectionRunner(journal, [store])

    async def sync(self) -> None:
        await self._runner.catch_up()

    async def add(
        self,
        title: str,
        text: str,
        provenance: Provenance,
        trust: Trust,
        correlation_id: str | None,
    ) -> str:
        doc_id = f"d_{new_id()}"
        await self._journal.append(
            [
                NewEvent(
                    kind=ADDED,
                    payload={"doc_id": doc_id, "title": title, "text": text[:MAX_DOC_CHARS]},
                    provenance=provenance,
                    trust=min(trust, DOCUMENT_TRUST),
                    correlation_id=correlation_id,
                )
            ]
        )
        await self.sync()
        return doc_id

    async def remove(self, doc_id: str, actor: str) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=REMOVED,
                    payload={"doc_id": doc_id},
                    provenance=Provenance(source="library", actor=actor),
                    trust=Trust.OWNER,
                )
            ]
        )
        await self.sync()

    async def search(self, text: str, limit: int) -> list[Passage]:
        """By words, and by meaning when embeddings are available (fused by rank)."""
        await self.sync()
        lexical = await self.store.search(text, limit * 2)
        if self._vectors is None or not text.strip():
            return lexical[:limit]
        everything = await self.store.all_passages()
        if not everything:
            return []
        try:
            vectors = await self._vectors([text, *(p.text for p in everything)])
        except ProviderError as exc:
            log.warning("library.semantic_unavailable", error=str(exc))
            return lexical[:limit]
        query = vectors[text]
        scored = sorted(
            ((float(np.dot(query, vectors[p.text])), p) for p in everything),
            key=lambda item: item[0],
            reverse=True,
        )
        semantic = [p for score, p in scored[: limit * 2] if score >= MIN_SIMILARITY]
        ranks: dict[tuple[str, str], float] = {}
        found: dict[tuple[str, str], Passage] = {}
        for ranking in (lexical, semantic):
            for rank, passage in enumerate(ranking):
                key = (passage.doc_id, passage.text)
                ranks[key] = ranks.get(key, 0.0) + 1.0 / (RRF_K + rank)
                found[key] = passage
        best = sorted(ranks, key=lambda k: ranks[k], reverse=True)
        return [found[k] for k in best[:limit]]


def notes_in(folder: Path) -> list[Path]:
    return sorted(
        p
        for p in folder.rglob("*")
        if p.is_file()
        and p.suffix.lower() in SUFFIXES
        and not any(part.startswith(".") for part in p.relative_to(folder).parts)
    )


class AddInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=2, max_length=200)
    text: str = Field(min_length=10, max_length=MAX_DOC_CHARS)


class KeepInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=2, max_length=200)
    url: str = Field(default="", max_length=2000, description="Where it was, if a page")
    gist: str = Field(
        min_length=10,
        max_length=800,
        description="What it says that could serve him later, in two or three sentences",
    )
    topics: list[Annotated[str, Field(max_length=60)]] = Field(
        min_length=1,
        max_length=8,
        description="What it is about, in the words a later conversation would use",
    )
    text: str = Field(default="", max_length=MAX_DOC_CHARS, description="What you read of it")


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=2, max_length=300)


class RecallInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    about: str = Field(default="", max_length=300, description="Words to look for; empty: all")
    since: date | None = Field(default=None, description="From this day (YYYY-MM-DD)")
    until: date | None = Field(default=None, description="Up to and including this day")


class ListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RemoveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc: str = Field(description="The document id (d_…)")


def kept_note(args: KeepInput, doc_id: str, day: datetime) -> str:
    """The memory fact for a kept reading, within a fact's length: the address goes first (the
    document keeps it), then the gist is shortened."""
    topics = ", ".join(args.topics)
    head = f"Lecture qu'il a gardée pour plus tard le {day:%d/%m/%Y} ({doc_id}) : « {args.title} »"
    tail = f" Sujets : {topics}."[: MAX_STATEMENT // 4]
    where = f" {args.url}" if args.url else ""
    note = f"{head}{where}. En bref : {args.gist}{tail}"
    if len(note) <= MAX_STATEMENT:
        return note
    room = MAX_STATEMENT - len(f"{head}. En bref : …{tail}")
    return f"{head}. En bref : {args.gist[:room].rstrip()}…{tail}"


def library_tools(
    library: Library, timezone: str, results: int, memory: MemoryService | None = None
) -> list[Tool]:
    zone = ZoneInfo(timezone)

    async def keep(args: Any, ctx: ToolContext) -> ToolResult:
        provenance = Provenance(
            source=f"thread:{ctx.thread_id}",
            actor="orion:core",
            derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
        )
        body = "\n\n".join(p for p in (args.url, args.gist, args.text) if p)
        doc_id = await library.add(
            args.title, body, provenance, ctx.context_trust, ctx.correlation_id
        )
        if memory is not None:
            await memory.remember(
                kept_note(args, doc_id, datetime.now(zone)),
                Origin(
                    actor="orion:core",
                    source=provenance.source,
                    trust=min(ctx.context_trust, DOCUMENT_TRUST),
                    derived_from=provenance.derived_from,
                    correlation_id=ctx.correlation_id,
                ),
                about=["owner"],
                category=Category.OTHER,
            )
        return ToolResult(
            f"Gardé ({doc_id}). Ne lui en fais pas de résumé maintenant : une ligne suffit. "
            "Tu lui en reparleras le jour où la conversation y touchera."
        )

    async def add(args: Any, ctx: ToolContext) -> ToolResult:
        doc_id = await library.add(
            args.title,
            args.text,
            Provenance(
                source=f"thread:{ctx.thread_id}",
                actor="orion:core",
                derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
            ),
            ctx.context_trust,
            ctx.correlation_id,
        )
        return ToolResult(f"Rangé dans la bibliothèque ({doc_id}).")

    async def search(args: Any, _ctx: ToolContext) -> ToolResult:
        found = await library.search(args.query, results)
        if not found:
            return ToolResult("Rien dans la bibliothèque là-dessus.")
        lines = [f"[{p.title} · {p.doc_id} · ajouté le {p.added_at[:10]}]\n{p.text}" for p in found]
        return ToolResult("\n\n".join(lines), trust=min(p.trust for p in found))

    async def recall(args: Any, _ctx: ToolContext) -> ToolResult:
        since = datetime.combine(args.since, time.min, zone) if args.since else None
        until = (
            datetime.combine(args.until + timedelta(days=1), time.min, zone) if args.until else None
        )
        await library.sync()
        said = await library.store.recall(args.about, since, until, 30)
        if not said:
            return ToolResult("Rien de tel dans vos conversations.")
        lines = [
            f"{s.at.astimezone(zone):%a %d/%m/%Y %H:%M} · "
            f"{'lui' if s.who == 'owner' else 'toi'} : {s.text[:500]}"
            for s in said
        ]
        return ToolResult("\n".join(lines), trust=min(s.trust for s in said))

    async def listing(_args: Any, _ctx: ToolContext) -> ToolResult:
        await library.sync()
        docs = await library.store.docs()
        if not docs:
            return ToolResult("La bibliothèque est vide.")
        return ToolResult(
            "\n".join(
                f"- ({d}) {t}, ajouté le {a[:10]}, {n} caractères" for d, t, a, n in docs[:50]
            )
        )

    async def remove(args: Any, ctx: ToolContext) -> ToolResult:
        await library.remove(args.doc, "owner:main" if ctx.thread_id == "owner" else "orion:core")
        return ToolResult("Retiré de la bibliothèque.")

    reading: dict[str, Any] = {
        "effect": Effect.NONE,
        "reversibility": Reversibility.REVERSIBLE,
        "concurrency_safe": True,
        "accepts_untrusted_context": True,
    }
    return [
        Tool(
            name="library_keep",
            description="Keep a link or text he sent « for later » (often a bare link, no "
            "words): after reading it, its gist and topics, so it comes back when it serves.",
            input_model=KeepInput,
            handler=keep,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
            accepts_untrusted_context=True,
        ),
        Tool(
            name="library_add",
            description="Keep a text whole in the owner's library (a note they dictate, a file "
            "read from their machine, a page worth keeping), to find it later.",
            input_model=AddInput,
            handler=add,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
            accepts_untrusted_context=True,
        ),
        Tool(
            name="library_search",
            description="Search the owner's library (their notes and documents) for passages "
            "about a question, by words and by meaning.",
            input_model=SearchInput,
            handler=search,
            **reading,
        ),
        Tool(
            name="conversations_search",
            description="What was said with the owner, about something and/or between two "
            "dates (« qu'est-ce que je disais de ça en août ? »). Older than memory's facts.",
            input_model=RecallInput,
            handler=recall,
            **reading,
        ),
        Tool(
            name="library",
            description="The documents in the owner's library.",
            input_model=ListInput,
            handler=listing,
            **reading,
        ),
        Tool(
            name="library_remove",
            description="Take a document out of the library, when the owner asks.",
            input_model=RemoveInput,
            handler=remove,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
    ]


class LibraryModule(Module):
    name = "library"
    instructions = "library.md"
    settings_model = LibrarySettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.library: Library | None = None
        self._store: LibraryStore | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        settings: LibrarySettings = self.settings
        self._store = LibraryStore(self.config.data_dir / "library.sqlite3")
        await self._store.open()
        semantic = plate.memory.semantic
        self.library = Library(
            plate.journal, self._store, semantic.vectors if semantic is not None else None
        )
        await self.library.sync()
        tools = library_tools(
            self.library, self.config.agent.timezone, settings.results, plate.memory
        )
        talk = [t for t in tools if t.name == "conversations_search"]
        shelf = [t for t in tools if t.name != "conversations_search"]
        return [
            *talk,
            grouped(
                "library",
                "The owner's library: notes and documents kept whole, searchable by words and "
                "by meaning.",
                shelf,
                {
                    "library_add": "add",
                    "library_keep": "keep",
                    "library_search": "search",
                    "library": "list",
                    "library_remove": "remove",
                },
            ),
        ]

    async def review(self, now: datetime) -> list[str]:
        library = self.library
        if library is None:
            return []
        await library.sync()
        return [
            f"- ({d}) {t}, ajouté le {a[:10]}, {n} caractères"
            for d, t, a, n in await library.store.docs()
        ]

    async def close(self) -> None:
        if self._store is not None:
            await self._store.close()
