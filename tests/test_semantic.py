from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.memory.model import Category
from orion.memory.semantic import EmbeddingCache, SemanticIndex
from orion.memory.service import MemoryService, Origin
from orion.memory.store import MemoryStore
from orion.providers.base import Embedder, ProviderError

OWNER = Origin(actor="orion:core", source="terminal:owner", trust=Trust.OWNER)
CONCEPTS = [
    ("allergique", "noix", "cajou", "manger", "mange", "nourriture"),
    ("travail", "boulot", "bureau", "collègue"),
    ("sport", "escalade", "grimpe"),
]


class Concepts:
    """Embeds a text as the concepts its words belong to: enough to tell meaning from words."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.down = False

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        if self.down:
            raise ProviderError("embeddings down", retryable=False)
        self.calls.append(texts)
        vectors: list[list[float]] = []
        for text in texts:
            words = text.lower().replace("'", " ").split()
            vector = [float(sum(w.strip("?.,") in c for w in words)) for c in CONCEPTS]
            vectors.append([*vector, 0.1])  # never the zero vector
        return vectors


@pytest.fixture
async def setup(
    journal: SqliteJournal, tmp_path: Path
) -> AsyncIterator[tuple[MemoryService, Concepts]]:
    store = MemoryStore(":memory:")
    await store.open()
    cache = EmbeddingCache(tmp_path / "embeddings.sqlite3")
    await cache.open()
    embedder = Concepts()
    assert isinstance(embedder, Embedder)
    memory = MemoryService(journal, store, SemanticIndex(store, embedder, cache, "emb"))
    await memory.remember("Paul est allergique aux noix de cajou.", OWNER, about=["Paul"])
    await memory.remember("Noan grimpe en salle le jeudi.", OWNER, category=Category.PROFILE)
    await memory.remember("Le bureau de Noan est à Lyon.", OWNER)
    yield memory, embedder
    await cache.close()
    await store.close()


async def test_meaning_finds_what_words_miss(setup: tuple[MemoryService, Concepts]) -> None:
    memory, embedder = setup
    question = "que doit-il éviter de manger ?"
    assert await memory.store.search(question) == []  # no word in common
    [first, *_] = await memory.search(question, limit=2)
    assert first.statement == "Paul est allergique aux noix de cajou."

    calls = len(embedder.calls)
    await memory.search("et le sport ?", limit=2)
    assert embedder.calls[calls:] == [["et le sport ?"]]  # facts come from the cache


async def test_words_still_count_and_failures_fall_back(
    setup: tuple[MemoryService, Concepts],
) -> None:
    memory, embedder = setup
    [first, *_] = await memory.search("Lyon", limit=3)
    assert first.statement == "Le bureau de Noan est à Lyon."
    embedder.down = True
    assert [f.statement for f in await memory.search("Lyon")] == ["Le bureau de Noan est à Lyon."]
