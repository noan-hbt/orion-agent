"""End-to-end contracts for the durable context registry."""

import sqlite3

import pytest

from context_registry import (
    ContextRegistry,
    ConversationThread,
    IntentState,
    Principal,
    RevisionConflict,
)


def test_principal_and_thread_are_durable_and_linked(tmp_path):
    path = tmp_path / "context.sqlite"
    registry = ContextRegistry(path)
    principal = registry.upsert_principal(
        Principal("p-1", scope="tenant-a", data={"channel": "telegram", "user": 42})
    )
    thread = registry.upsert_thread(
        ConversationThread("t-1", principal_id=principal.id, scope="tenant-a", data={"chat": 7})
    )
    assert principal.revision == 0
    assert registry.get_principal("p-1", "tenant-a") == principal
    assert registry.get_thread("t-1", "tenant-a") == thread
    registry.close()


def test_intent_versions_support_optimistic_concurrency(tmp_path):
    registry = ContextRegistry(tmp_path / "context.sqlite")
    first = registry.upsert_intent(IntentState("t-1", "research", "tenant-a", {"step": 1}))
    second = registry.upsert_intent(
        IntentState("t-1", "research", "tenant-a", {"step": 2}),
        expected_revision=first.revision,
    )
    assert second.revision == first.revision + 1
    assert registry.get_intent("t-1", "tenant-a").data["step"] == 2
    with pytest.raises(RevisionConflict):
        registry.upsert_intent(IntentState("t-1", "done", "tenant-a"), expected_revision=first.revision)


def test_same_ids_are_isolated_by_scope(tmp_path):
    registry = ContextRegistry(tmp_path / "context.sqlite")
    registry.upsert_principal(Principal("same", "one", {"owner": "a"}))
    registry.upsert_principal(Principal("same", "two", {"owner": "b"}))
    registry.upsert_thread(ConversationThread("thread", "same", "one"))
    registry.upsert_thread(ConversationThread("thread", "same", "two"))
    registry.upsert_intent(IntentState("thread", "a", "one"))
    registry.upsert_intent(IntentState("thread", "b", "two"))
    assert registry.get_principal("same", "one").data["owner"] == "a"
    assert registry.get_principal("same", "two").data["owner"] == "b"
    assert registry.get_thread("thread", "one").scope == "one"
    assert registry.get_intent("thread", "two").intent == "b"
    registry.bind_channel("telegram", "42", scope="one", principal_id="same", thread_id="thread")
    registry.bind_channel("telegram", "42", scope="two", principal_id="same", thread_id="thread")
    assert registry.resolve_binding("telegram", "42", "one")["scope"] == "one"
    assert registry.resolve_binding("telegram", "42", "two")["scope"] == "two"


def test_migration_is_additive_to_existing_database(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE legacy_events (id INTEGER PRIMARY KEY, payload TEXT)")
        db.execute("INSERT INTO legacy_events VALUES (1, 'keep me')")
        db.execute("CREATE TABLE principals (id TEXT NOT NULL, scope TEXT NOT NULL, data TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(id, scope))")
    registry = ContextRegistry(path)
    registry.upsert_principal(Principal("legacy", "global", {"new": True}))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT payload FROM legacy_events WHERE id=1").fetchone()[0] == "keep me"
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"conversation_threads", "intent_states", "channel_bindings"} <= tables


def test_state_survives_registry_restart(tmp_path):
    path = tmp_path / "restart.sqlite"
    first = ContextRegistry(path)
    first.upsert_principal(Principal("p", "scope", {"name": "Noah"}))
    first.upsert_intent(IntentState("t", "ship", "scope", {"version": 3}))
    first.close()
    restarted = ContextRegistry(path)
    assert restarted.get_principal("p", "scope").data == {"name": "Noah"}
    assert restarted.get_intent("t", "scope").data["version"] == 3


def test_snapshot_filters_to_requested_conversation_instead_of_first_rows(tmp_path):
    registry = ContextRegistry(tmp_path / "context.sqlite")
    for index in range(125):
        principal_id = f"p-{index:03d}"
        thread_id = f"thread-{index:03d}"
        registry.upsert_principal(
            Principal(principal_id, "tenant", {"index": index})
        )
        registry.upsert_thread(
            ConversationThread(
                thread_id,
                principal_id=principal_id,
                scope="tenant",
                data={"index": index},
            )
        )
        registry.upsert_intent(
            IntentState(thread_id, f"intent-{index}", "tenant", {"index": index})
        )

    snapshot = registry.snapshot(scope="tenant", thread_id="thread-124")

    assert [item["id"] for item in snapshot["threads"]] == ["thread-124"]
    assert [item["thread_id"] for item in snapshot["intents"]] == ["thread-124"]
    assert [item["id"] for item in snapshot["principals"]] == ["p-124"]


def test_snapshot_resolves_conversation_binding_and_dynamic_scope(tmp_path):
    current = {
        "scope": "tenant-a",
        "conversation_id": "conversation-a",
        "thread_id": "thread-a",
    }
    registry = ContextRegistry(
        tmp_path / "context.sqlite",
        scope_resolver=lambda: dict(current),
    )
    for suffix in ("a", "b"):
        scope = f"tenant-{suffix}"
        principal_id = f"principal-{suffix}"
        thread_id = f"thread-{suffix}"
        conversation_id = f"conversation-{suffix}"
        registry.upsert_principal(Principal(principal_id, scope, {"owner": suffix}))
        registry.upsert_thread(
            ConversationThread(thread_id, principal_id=principal_id, scope=scope)
        )
        registry.upsert_intent(IntentState(thread_id, f"intent-{suffix}", scope))
        registry.bind_channel(
            "telegram",
            conversation_id,
            scope=scope,
            principal_id=principal_id,
            thread_id=thread_id,
        )

    first = registry.snapshot()
    assert first["scope"] == "tenant-a"
    assert [item["id"] for item in first["threads"]] == ["thread-a"]
    assert [item["external_id"] for item in first["bindings"]] == ["conversation-a"]

    current.update(
        scope="tenant-b",
        conversation_id="conversation-b",
        thread_id="thread-b",
    )
    second = registry.snapshot()
    assert second["scope"] == "tenant-b"
    assert [item["id"] for item in second["threads"]] == ["thread-b"]
    assert [item["external_id"] for item in second["bindings"]] == ["conversation-b"]


def test_unscoped_snapshot_is_not_silently_truncated_to_100_rows(tmp_path):
    registry = ContextRegistry(tmp_path / "context.sqlite")
    for index in range(105):
        registry.upsert_thread(ConversationThread(f"thread-{index:03d}"))

    assert len(registry.snapshot()["threads"]) == 105
    assert len(registry.snapshot(limit=100)["threads"]) == 100
