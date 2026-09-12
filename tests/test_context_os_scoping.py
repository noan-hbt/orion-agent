import multiprocessing
import json
import threading

from context_os import ThreadStateStore


def _stale_process_write(path, ready, proceed):
    store = ThreadStateStore(path)
    ready.set()
    if not proceed.wait(timeout=5):
        raise RuntimeError("parent did not release stale writer")
    store.update(
        conversation_id="conversation-child",
        thread_id="thread-child",
        marker="child",
    )


def test_thread_state_store_isolates_conversations_and_threads(tmp_path):
    path = tmp_path / "thread-state.json"
    store = ThreadStateStore(path)

    first = store.update(
        conversation_id="conversation-a",
        thread_id="thread-a",
        step="alpha",
    )
    second = store.update(
        conversation_id="conversation-b",
        thread_id="thread-b",
        step="beta",
    )

    assert first.version == 1
    assert second.version == 1
    assert store.get(conversation_id="conversation-a", thread_id="thread-a").values == {
        "step": "alpha"
    }
    assert store.get(conversation_id="conversation-b", thread_id="thread-b").values == {
        "step": "beta"
    }
    assert store.get(conversation_id="conversation-a", thread_id="thread-b").values == {}

    restored = ThreadStateStore(path)
    assert restored.get(
        conversation_id="conversation-a", thread_id="thread-a"
    ).values == {"step": "alpha"}
    assert restored.get(
        conversation_id="conversation-b", thread_id="thread-b"
    ).values == {"step": "beta"}


def test_thread_state_store_uses_dynamic_scope_resolver(tmp_path):
    current = {
        "scope": "tenant-a",
        "conversation_id": "conversation-a",
        "thread_id": "thread-a",
    }
    store = ThreadStateStore(
        tmp_path / "thread-state.json",
        scope_resolver=lambda: dict(current),
    )

    store.update(marker="a")
    current.update(
        scope="tenant-b",
        conversation_id="conversation-b",
        thread_id="thread-b",
    )
    store.update(marker="b")

    assert store.get().scope == "tenant-b"
    assert store.get().values == {"marker": "b"}
    current.update(
        scope="tenant-a",
        conversation_id="conversation-a",
        thread_id="thread-a",
    )
    assert store.get().scope == "tenant-a"
    assert store.get().values == {"marker": "a"}


def test_multiple_instances_refresh_merge_and_preserve_concurrent_scoped_writes(tmp_path):
    path = tmp_path / "thread-state.json"
    first = ThreadStateStore(path)
    stale = ThreadStateStore(path)

    first.update(
        conversation_id="conversation-a",
        thread_id="thread-a",
        marker="a1",
    )
    # ``stale`` was created before the write above. Its update must refresh and
    # merge the on-disk v2 state instead of replacing conversation A.
    stale.update(
        conversation_id="conversation-b",
        thread_id="thread-b",
        marker="b1",
    )

    reopened = ThreadStateStore(path)
    assert reopened.get(
        conversation_id="conversation-a", thread_id="thread-a"
    ).values == {"marker": "a1"}
    assert reopened.get(
        conversation_id="conversation-b", thread_id="thread-b"
    ).values == {"marker": "b1"}

    # Reads on an already-open instance must refresh too, not serve the stale
    # constructor snapshot indefinitely.
    first.update(
        conversation_id="conversation-a",
        thread_id="thread-a",
        marker="a2",
    )
    assert stale.get(
        conversation_id="conversation-a", thread_id="thread-a"
    ).values == {"marker": "a2"}

    writer_c = ThreadStateStore(path)
    writer_d = ThreadStateStore(path)
    barrier = threading.Barrier(2)

    def write(store, conversation_id, thread_id, marker):
        barrier.wait()
        store.update(
            conversation_id=conversation_id,
            thread_id=thread_id,
            marker=marker,
        )

    threads = [
        threading.Thread(
            target=write,
            args=(writer_c, "conversation-c", "thread-c", "c1"),
        ),
        threading.Thread(
            target=write,
            args=(writer_d, "conversation-d", "thread-d", "d1"),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    fresh = ThreadStateStore(path)
    expected = {
        ("conversation-a", "thread-a"): "a2",
        ("conversation-b", "thread-b"): "b1",
        ("conversation-c", "thread-c"): "c1",
        ("conversation-d", "thread-d"): "d1",
    }
    for (conversation_id, thread_id), marker in expected.items():
        assert fresh.get(
            conversation_id=conversation_id,
            thread_id=thread_id,
        ).values == {"marker": marker}


def test_stale_instance_in_another_process_merges_and_existing_reader_refreshes(tmp_path):
    path = tmp_path / "thread-state.json"
    parent_store = ThreadStateStore(path)
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    proceed = ctx.Event()
    child = ctx.Process(
        target=_stale_process_write,
        args=(str(path), ready, proceed),
    )
    child.start()
    try:
        assert ready.wait(timeout=5)
        parent_store.update(
            conversation_id="conversation-parent",
            thread_id="thread-parent",
            marker="parent",
        )
        proceed.set()
        child.join(timeout=10)
        assert child.exitcode == 0

        # This store predates the child write and must still observe it.
        assert parent_store.get(
            conversation_id="conversation-child",
            thread_id="thread-child",
        ).values == {"marker": "child"}

        fresh = ThreadStateStore(path)
        assert fresh.get(
            conversation_id="conversation-parent",
            thread_id="thread-parent",
        ).values == {"marker": "parent"}
        assert fresh.get(
            conversation_id="conversation-child",
            thread_id="thread-child",
        ).values == {"marker": "child"}
    finally:
        proceed.set()
        if child.is_alive():
            child.terminate()
        child.join(timeout=5)


def test_legacy_single_state_is_preserved_and_migrated_to_v2_on_write(tmp_path):
    path = tmp_path / "thread-state.json"
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "thread_id": "legacy-thread",
                "legacy": "kept",
            }
        ),
        encoding="utf-8",
    )

    store = ThreadStateStore(path, thread_id="ignored")
    assert store.get().thread_id == "legacy-thread"
    assert store.get().values == {"legacy": "kept"}
    updated = store.update(expected_version=3, added="new")
    assert updated.version == 4

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["schema"] == "orion.thread_state.v2"
    reopened = ThreadStateStore(path)
    assert reopened.get().values == {"legacy": "kept", "added": "new"}
