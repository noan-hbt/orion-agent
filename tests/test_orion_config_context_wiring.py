from event_handler import Event
from context_registry import ConversationThread
from orion_config import OrionConfig


def _config(tmp_path, data=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "ORION_CORE.md").write_text("core", encoding="utf-8")
    config = OrionConfig.from_mapping(
        {
            "reflection": {"enabled": False},
            "context": {"reflection_enabled": False},
            "memory": {"enabled": False},
            **(data or {}),
        }
    )
    config.config_path = tmp_path / "orion.toml"
    return config


def test_config_propagates_context_mode_to_runtime_assembler_and_composer(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    config = _config(tmp_path, {"context": {"context_mode": "legacy", "reflection_enabled": False}})

    app = config.build()
    try:
        assert app.runtime.context_mode == "legacy"
        assert app.runtime.context_assembler.policy.context_mode == "legacy"
        assert app.runtime.prompt_composer.context_mode == "legacy"
    finally:
        app.stop()


def test_config_propagates_task_context_budgets_to_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    config = _config(
        tmp_path,
        {
            "context": {
                "reflection_enabled": False,
                "task_max_chars": 4321,
                "task_max_tokens": 876,
            }
        },
    )

    app = config.build()
    try:
        policy = app.runtime.context_assembler.policy
        assert policy.task_max_chars == 4321
        assert policy.task_max_tokens == 876
        assert app.runtime._context_limit("task_max_tokens", 3000) == 876
    finally:
        app.stop()


def test_context_os_runtime_resolver_isolates_thread_state_and_registry(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    config = _config(
        tmp_path,
        {
            "context_os": {
                "enabled": True,
                "registry_path": "data/context.sqlite3",
                "memory_path": "data/memory.sqlite3",
                "state_path": "data/thread-state.json",
            }
        },
    )

    app = config.build()
    try:
        store = app.runtime.thread_state_store
        registry = app.runtime.context_registry
        registry.upsert_thread(ConversationThread("thread-a", scope="tenant-a"))
        registry.upsert_thread(ConversationThread("thread-b", scope="tenant-b"))

        app.runtime._run_context = type(
            "Context",
            (),
            {
                "event": Event(
                    "message",
                    {"text": "a"},
                    metadata={
                        "conversation_id": "conversation-a",
                        "thread_id": "thread-a",
                        "context_scope": "tenant-a",
                    },
                )
            },
        )()
        store.update(marker="a")
        assert [item["id"] for item in registry.snapshot()["threads"]] == ["thread-a"]

        app.runtime._run_context = type(
            "Context",
            (),
            {
                "event": Event(
                    "message",
                    {"text": "b"},
                    metadata={
                        "conversation_id": "conversation-b",
                        "thread_id": "thread-b",
                        "context_scope": "tenant-b",
                    },
                )
            },
        )()
        store.update(marker="b")
        assert store.get().values == {"marker": "b"}
        assert [item["id"] for item in registry.snapshot()["threads"]] == ["thread-b"]

        assert store.get(
            scope="tenant-a",
            conversation_id="conversation-a",
            thread_id="thread-a",
        ).values == {"marker": "a"}
    finally:
        app.runtime._run_context = None
        app.stop()


def test_memory_defaults_flush_small_tail_and_configure_backlog_drain(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    defaults = OrionConfig.from_mapping({}).memory
    assert defaults.min_entries == 20
    assert defaults.max_batches_per_run == 8
    assert defaults.tail_max_age == 3600.0

    config = _config(
        tmp_path,
        {
            "memory": {
                "enabled": True,
                "batch_size": 2,
                "min_entries": 2,
                "max_batches_per_run": 3,
                "tail_max_age": 0,
            }
        },
    )
    app = config.build()
    try:
        maintenance = app.runtime.memory_maintenance
        assert maintenance.max_batches_per_run == 3
        assert maintenance.tail_max_age == 0
        maintenance.extractor.extract_batch = lambda entries: ({}, len(entries))
        for index in range(5):
            maintenance.journal.append(
                event_id=f"event-{index}",
                task_id=None,
                messages=[{"role": "user", "content": f"message-{index}"}],
                conversation_id="conversation",
            )

        assert maintenance.run_once() == 5
        assert maintenance.extractor.store.journal_cursor == 5
    finally:
        app.stop()
