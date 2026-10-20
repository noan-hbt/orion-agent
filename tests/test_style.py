"""Drafts in the owner's name sound like him: his style, learned from his own messages (D68)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import SqliteJournal
from orion.memory.service import MemoryService, Origin
from orion.memory.store import MemoryStore
from orion.style.module import OwnerWriting, style_card, style_tools
from orion.tools.base import ToolContext

HIS = [
    "tu peux me rappeler demain pour le loyer",
    "ok merci",
    "mdr non c'est jeudi",
    "envoie moi le lien stp",
    "jsp trop, on verra",
    "c'est bon j'ai fini",
    "ouais grave",
]


@pytest.fixture
async def memory(journal: SqliteJournal) -> AsyncIterator[MemoryService]:
    store = MemoryStore(":memory:")
    await store.open()
    yield MemoryService(journal, store)
    await store.close()


async def said(journal: SqliteJournal, text: str, actor: str = "owner:main") -> None:
    await journal.append(
        [
            NewEvent(
                kind="message.received",
                payload={"thread_id": "owner", "channel": "imessage", "text": text},
                provenance=Provenance(source="imessage:owner", actor=actor),
                trust=Trust.OWNER,
            )
        ]
    )


def test_the_card_counts_what_is_countable() -> None:
    card = style_card(HIS)
    assert "commence par une minuscule : presque toujours" in card
    assert "finit par un point ou une ponctuation : rarement" in card
    assert "mdr" in card and "stp" in card and "jsp" in card
    assert "« ok merci »" in card  # his own words, as typed
    assert "pas encore assez" in style_card(HIS[:2]).lower()


async def test_a_draft_for_someone_knows_how_he_talks_to_them(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    for text in HIS:
        await said(journal, text)
    await said(journal, "Message d'un tiers transmis", actor="contact:paul")  # not his
    await memory.remember(
        "Avec sa mère, il écrit des phrases complètes et met des emojis cœur.",
        Origin(actor="owner:main", source="imessage:owner", trust=Trust.OWNER),
        about=["Maman"],
    )
    (tool,) = style_tools(journal, OwnerWriting(), memory)
    result = await tool.handler(tool.parse(json.dumps({"to": "maman"})), ToolContext("owner", "c"))
    assert "D'après ses 7 derniers messages" in result.content
    assert "tiers" not in result.content
    assert "emojis cœur" in result.content
    other = await tool.handler(tool.parse(json.dumps({"to": "Paul"})), ToolContext("owner", "c"))
    assert "Rien en mémoire sur sa façon d'écrire à Paul." in other.content
