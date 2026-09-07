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
