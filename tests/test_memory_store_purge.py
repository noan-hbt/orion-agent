from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from memory_store import MemoryStore


class _Clock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_expired_items_never_escape_before_amortized_physical_purge() -> None:
    clock = _Clock()
    store = MemoryStore(clock=clock, purge_interval=60.0)
    try:
        item = store.put("short lived", namespace="user-a", ttl=5)
        assert item is not None

        clock.value = 1_006.0
        assert store.get(item.id, namespace="user-a") is None
        assert store.search("short", namespace="user-a") == []

        # Physical cleanup is deliberately delayed, but logical TTL semantics
        # are already exact because every read filters expires_at.
        raw_count = store._db.execute(
            "SELECT COUNT(*) FROM memories WHERE id=?", (item.id,)
        ).fetchone()[0]
        assert raw_count == 1

        clock.value = 1_061.0
        assert store.get(item.id, namespace="user-a") is None
        raw_count = store._db.execute(
            "SELECT COUNT(*) FROM memories WHERE id=?", (item.id,)
        ).fetchone()[0]
        assert raw_count == 0
    finally:
        store.close()


def test_search_injected_now_filters_ttl_without_time_travel_cleanup() -> None:
    clock = _Clock()
    store = MemoryStore(clock=clock, purge_interval=60.0)
    try:
        item = store.put("future expiry", namespace="user-a", ttl=30)

        assert store.search("future", namespace="user-a", now=1_031.0) == []
        assert store.get(item.id, namespace="user-a") is not None
        assert store._db.execute(
            "SELECT COUNT(*) FROM memories WHERE id=?", (item.id,)
        ).fetchone()[0] == 1
    finally:
        store.close()


def test_repeated_reads_do_not_issue_delete_or_commit_before_cleanup_window() -> None:
    clock = _Clock()
    store = MemoryStore(clock=clock, purge_interval=60.0)
    try:
        item = store.put("stable memory", namespace="user-a", ttl=120)
        statements: list[str] = []
        store._db.set_trace_callback(statements.append)

        for _ in range(100):
            assert store.get(item.id, namespace="user-a") is not None
            assert store.search("stable", namespace="user-a")

        writes = [
            statement.upper()
            for statement in statements
            if statement.lstrip().upper().startswith(("DELETE", "COMMIT"))
        ]
        assert writes == []
    finally:
        store.close()


def test_namespace_ttl_and_concurrent_reads_remain_isolated_and_safe() -> None:
    clock = _Clock()
    store = MemoryStore(clock=clock, purge_interval=60.0)
    try:
        expired = store.put("shared term expired", namespace="a", ttl=1)
        live = store.put("shared term live", namespace="b", ttl=100)
        clock.value = 1_002.0

        def read(namespace: str):
            return store.search("shared", namespace=namespace)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(read, ["a", "b"] * 50))

        assert all(result == [] for result in results[0::2])
        assert all([item.id for item in result] == [live.id] for result in results[1::2])
        assert store.get(expired.id, namespace="a") is None
    finally:
        store.close()
