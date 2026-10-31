"""Semantic memory search: facts found by meaning, not only by shared words.

"Qu'est-ce que Paul ne mange pas ?" should find "Paul est allergique aux noix de cajou" although
the two share no word. Each current fact is embedded once (OpenRouter's embeddings), and a
search fuses two rankings, the full-text one and the vector one, by reciprocal rank.

Embeddings live in a cache file (`embeddings.sqlite3`), keyed by model and text. It is a cache,
not a projection: deleting it costs one re-embedding, and forgetting a fact removes it from the
store, so it is never found again whatever the cache holds. When embeddings are unavailable, the
search falls back to full text and says so in the logs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import structlog

from orion.core.sqlite import SqliteWorker
from orion.memory.store import Fact, MemoryStore
from orion.providers.base import Embedder, ProviderError

log = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    model   TEXT NOT NULL,
    digest  TEXT NOT NULL,
    vector  BLOB NOT NULL,
    PRIMARY KEY (model, digest)
);
"""
RRF_K = 60
MIN_SIMILARITY = 0.25
BATCH = 64
# Embedding models take 8192 tokens at most; a query can be a whole night transcript (400k
# characters), which failed every search of the night. The head of a long text carries its
# meaning well enough; the cache stays keyed by the full text.
MAX_CHARS = 8000

Vector = np.ndarray[tuple[int], np.dtype[np.float32]]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class EmbeddingCache:
    def __init__(self, path: Path | str) -> None:
        self._sql = SqliteWorker(path, "embeddings", _SCHEMA)

    async def open(self) -> None:
        await self._sql.open()

    async def close(self) -> None:
        await self._sql.close()

    async def get(self, model: str, texts: Sequence[str]) -> dict[str, Vector]:
        return await self._sql.run(self._get_sync, model, list(texts))

    async def put(self, model: str, vectors: dict[str, Vector]) -> None:
        await self._sql.run(self._put_sync, model, vectors)

    def _get_sync(self, model: str, texts: list[str]) -> dict[str, Vector]:
        found: dict[str, Vector] = {}
        for text in texts:
            row = self._sql.db.execute(
                "SELECT vector FROM embeddings WHERE model = ? AND digest = ?",
                (model, _digest(text)),
            ).fetchone()
            if row is not None:
                found[text] = np.frombuffer(row[0], dtype=np.float32)
        return found

    def _put_sync(self, model: str, vectors: dict[str, Vector]) -> None:
        with self._sql.transaction() as db:
            db.executemany(
                "INSERT OR REPLACE INTO embeddings (model, digest, vector) VALUES (?, ?, ?)",
                [(model, _digest(t), v.astype(np.float32).tobytes()) for t, v in vectors.items()],
            )


def _unit(vector: Sequence[float]) -> Vector:
    array = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(array))
    return array / norm if norm else array


class SemanticIndex:
    def __init__(
        self, store: MemoryStore, embedder: Embedder, cache: EmbeddingCache, model: str
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._cache = cache
        self._model = model

    async def vectors(self, texts: Sequence[str]) -> dict[str, Vector]:
        """Unit vectors for `texts`, embedding only what the cache does not hold yet."""
        found = await self._cache.get(self._model, texts)
        missing = [t for t in dict.fromkeys(texts) if t not in found]
        for start in range(0, len(missing), BATCH):
            chunk = missing[start : start + BATCH]
            embedded = await self._embedder.embed(self._model, [t[:MAX_CHARS] for t in chunk])
            fresh = {t: _unit(v) for t, v in zip(chunk, embedded, strict=True)}
            await self._cache.put(self._model, fresh)
            found.update(fresh)
        return found

    async def search(self, text: str, limit: int = 8, include_past: bool = False) -> list[Fact]:
        lexical = await self._store.search(text, limit * 2, include_past)
        facts = await self._store.facts(include_past)
        if not facts or not text.strip():
            return lexical[:limit]
        try:
            vectors = await self.vectors([text, *(f.statement for f in facts)])
        except ProviderError as exc:
            log.warning("memory.semantic_unavailable", error=str(exc))
            return lexical[:limit]
        query = vectors[text]
        scored = sorted(
            ((float(np.dot(query, vectors[f.statement])), f) for f in facts),
            key=lambda item: item[0],
            reverse=True,
        )
        semantic = [f for score, f in scored[: limit * 2] if score >= MIN_SIMILARITY]
        ranks: dict[str, float] = {}
        by_id: dict[str, Fact] = {}
        for ranking in (lexical, semantic):
            for rank, fact in enumerate(ranking):
                ranks[fact.id] = ranks.get(fact.id, 0.0) + 1.0 / (RRF_K + rank)
                by_id[fact.id] = fact
        best = sorted(ranks, key=lambda fact_id: ranks[fact_id], reverse=True)
        return [by_id[fact_id] for fact_id in best[:limit]]
