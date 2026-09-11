from __future__ import annotations

from pathlib import Path

from event_handler import EventHandler
from orion_config import OrionConfig
from runtime import AgentRuntime


def test_sample_config_enables_both_durable_inboxes_on_same_file():
    config = OrionConfig.from_file(Path(__file__).resolve().parents[1] / "orion.toml")

    assert config.events.durable_path == "data/events.sqlite3"
    assert config.runtime.durable_path == "data/events.sqlite3"


def test_event_handler_to_runtime_durable_crash_replay_without_duplicate(tmp_path):
    path = tmp_path / "data" / "events.sqlite3"
    first_outputs = []
    first_handler = EventHandler(workers=0, durable_path=str(path))
    first_runtime = AgentRuntime(
        durable_path=str(path),
        action_ledger_path=":memory:",
        history_enabled=False,
        on_output=first_outputs.append,
    ).attach(first_handler)

    published = first_handler.publish(
        "subagent.completed",
        {
            "job_id": "job-production-durable",
            "status": "completed",
            "result": "survived runtime restart",
        },
        idempotency_key="pipeline-event-1",
    )

    # EventHandler durably owns and completes its handoff. Runtime durably
    # accepts before RAM processing; the crash window is between those two
    # independent namespaces in the same SQLite file.
    dispatched = first_handler.dispatch_one(timeout=0)
    assert dispatched is not None and dispatched.id == published.id
    handler_receipt = first_handler._durable_store.list_events()[0]
    runtime_receipt = first_runtime._durable_store.list_events()[0]
    assert handler_receipt.status == "acked"
    assert runtime_receipt.status == "queued"
    assert handler_receipt.event_id == runtime_receipt.event_id == published.id
    assert handler_receipt.receipt_id != runtime_receipt.receipt_id
    assert first_outputs == []

    # Simulated process crash before AgentRuntime.process_one(): discard both
    # RAM queues/connections without letting the runtime consume its receipt.
    first_handler.close()
    first_runtime._durable_store.close()

    replay_outputs = []
    restarted_handler = EventHandler(workers=0, durable_path=str(path))
    restarted_runtime = AgentRuntime(
        durable_path=str(path),
        action_ledger_path=":memory:",
        history_enabled=False,
        on_output=replay_outputs.append,
    ).attach(restarted_handler)
    try:
        restarted_handler.start()

        # EventHandler's namespace was already acked, so it must not dispatch
        # the same upstream event again. Runtime independently rehydrates its
        # queued receipt and processes it exactly once.
        assert restarted_handler.dispatch_one(timeout=0) is None
        replayed = restarted_runtime.process_one(timeout=0)
        assert replayed is not None and replayed.id == published.id
        assert replay_outputs[-1].content == "survived runtime restart"
        assert restarted_runtime.process_one(timeout=0) is None
        assert len(replay_outputs) == 1

        handler_rows = restarted_handler._durable_store.list_events()
        runtime_rows = restarted_runtime._durable_store.list_events()
        assert len(handler_rows) == 1
        assert len(runtime_rows) == 1
        assert handler_rows[0].namespace == "event_handler"
        assert runtime_rows[0].namespace == "runtime"
        assert handler_rows[0].status == "acked"
        assert runtime_rows[0].status == "acked"
        assert handler_rows[0].event_id == runtime_rows[0].event_id == published.id
    finally:
        restarted_handler.close()
        restarted_runtime._durable_store.close()
