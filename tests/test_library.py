"""The second brain: documents kept whole, and the conversation, searchable."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from orion.context.threads import MESSAGE_KIND
from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import SqliteJournal
from orion.core.projections import ProjectionRunner
from orion.library.module import Library, library_tools, notes_in
from orion.library.store import LibraryStore, passages
from orion.tools.base import Tool, ToolContext

NOTE = """Projet appart.

Budget max 700 € charges comprises. Dreux ou Chartres, près de la gare.

Idées en vrac : vérifier l'isolation, demander le DPE."""


async def by_meaning(texts: Sequence[str]) -> dict[str, Any]:
    """A toy embedding: « logement » means the same as « appart »."""

    def vector(text: str) -> Any:
        lowered = text.lower()
        v = np.array([("appart" in lowered or "logement" in lowered) * 1.0, 0.1], np.float32)
        return v / np.linalg.norm(v)

    return {t: vector(t) for t in texts}


@pytest.fixture
async def library(journal: SqliteJournal) -> AsyncIterator[Library]:
    store = LibraryStore(":memory:")
    await store.open()
    yield Library(journal, store, by_meaning)
    await store.close()


async def use(tools: dict[str, Tool], name: str, **args: object) -> Any:
    tool = tools[name]
    return await tool.handler(tool.parse(json.dumps(args)), ToolContext("owner", "c"))


def test_passages_follow_paragraphs() -> None:
    assert passages(NOTE, size=80) == [
        "Projet appart.",
        "Budget max 700 € charges comprises. Dreux ou Chartres, près de la gare.",
        "Idées en vrac : vérifier l'isolation, demander le DPE.",
    ]


async def test_a_note_is_found_by_words_and_by_meaning(library: Library) -> None:
    tools = {t.name: t for t in library_tools(library, "Europe/Paris", 3)}
    await use(tools, "library_add", title="Projet appart", text=NOTE)
    words = await use(tools, "library_search", query="budget gare")
    assert "700 €" in words.content and "Projet appart" in words.content
    # A document is content, not orders.
    assert words.trust is Trust.OWNER_INSTANCE
    meaning = await use(tools, "library_search", query="quel logement je cherche ?")
    assert "Projet appart" in meaning.content
    assert "Projet appart" in (await use(tools, "library")).content


async def test_what_was_said_and_when(journal: SqliteJournal, library: Library) -> None:
    await journal.append(
        [
            NewEvent(
                kind="message.received",
                payload={"thread_id": "owner", "text": "Paul m'a parlé d'un plan à Chartres"},
                provenance=Provenance(source="imessage:owner", actor="owner:main"),
                trust=Trust.OWNER,
            ),
            NewEvent(
                kind=MESSAGE_KIND,
                payload={
                    "thread_id": "owner",
                    "message": {"role": "assistant", "content": "Noté pour Chartres."},
                },
                provenance=Provenance(source="orion:runtime", actor="orion:core"),
                trust=Trust.WEB,
            ),
        ]
    )
    tools = {t.name: t for t in library_tools(library, "Europe/Paris", 3)}
    today = datetime.now(UTC).date().isoformat()
    found = await use(tools, "conversations_search", about="Chartres", since=today)
    assert "lui : Paul m'a parlé" in found.content and "toi : Noté" in found.content
    assert found.trust is Trust.WEB  # what Orion said then was based on a page
    assert (
        "Rien"
        in (await use(tools, "conversations_search", about="Chartres", until="2020-01-01")).content
    )


async def test_the_index_is_rebuilt_from_the_journal(
    journal: SqliteJournal, library: Library
) -> None:
    await library.add(
        "Note", NOTE, Provenance(source="file:x", actor="owner:cli"), Trust.OWNER, None
    )
    rebuilt = LibraryStore(":memory:")
    await rebuilt.open()
    await ProjectionRunner(journal, [rebuilt]).catch_up()
    assert [d[1] for d in await rebuilt.docs()] == ["Note"]
    assert (await rebuilt.search("isolation", 3))[0].trust is Trust.OWNER_INSTANCE
    await rebuilt.close()


def test_a_notes_folder_skips_hidden_and_other_files(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("x")
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "c.md").write_text("x")
    (tmp_path / "d.png").write_bytes(b"x")
    assert [p.name for p in notes_in(tmp_path)] == ["a.md", "b.txt"]
