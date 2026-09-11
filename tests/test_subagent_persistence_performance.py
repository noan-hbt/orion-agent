from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import subagents as subagents_module
from subagents import SubAgentManager


class _LLM:
    model = "stub/model"

    def tool_definitions(self):
        return []


class _Events:
    def __init__(self) -> None:
        self.published: list[str] = []

    def publish(self, event_type, *args, **kwargs):
        self.published.append(str(event_type))
        return object()


class _SignalingEvents(_Events):
    def __init__(self) -> None:
        super().__init__()
        self.failed = threading.Event()

    def publish(self, event_type, *args, **kwargs):
        result = super().publish(event_type, *args, **kwargs)
        if str(event_type) == "subagent.failed":
            self.failed.set()
        return result


class _BlockingLLM:
    model = "stub/model"

    def __init__(self) -> None:
        self.complete_started = threading.Event()
        self.release_complete = threading.Event()
        self.execute_calls: list[str] = []

    def tool_definitions(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "test tool",
                    "parameters": {"type": "object"},
                },
            }
        ]

    def complete(self, messages, **kwargs):
        self.complete_started.set()
        assert self.release_complete.wait(timeout=2.0)
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-after-stop",
                                "type": "function",
                                "function": {
                                    "name": "web_search",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    }
                }
            ]
        }

    def execute_tool_call(self, call, **kwargs):
        self.execute_calls.append(str(call["id"]))
        return {
            "role": "tool",
            "tool_call_id": call["id"],
            "name": "web_search",
            "content": "unexpected",
        }


def _manager(tmp_path: Path, events=None) -> SubAgentManager:
    return SubAgentManager(
        _LLM(),
        events or _Events(),
        state_path=tmp_path / "subagents.json",
        default_tools=[],
        emit_progress_events=False,
    )


def test_older_snapshot_cannot_overwrite_newer_generation(
    tmp_path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    agent = manager.create_agent("worker", "initial")

    with manager._lock:
        current = manager._agents[agent.id]
        current.description = "older"
        older = manager._prepare_save_locked()
        current.description = "newer"
        newer = manager._prepare_save_locked()

    original_dumps = subagents_module.json.dumps
    older_serializing = threading.Event()
    release_older = threading.Event()

    def controlled_dumps(value, *args, **kwargs):
        if (
            isinstance(value, dict)
            and value.get("persistence_generation") == older[0]
        ):
            older_serializing.set()
            assert release_older.wait(timeout=2.0)
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(subagents_module.json, "dumps", controlled_dumps)
    results: dict[str, bool] = {}

    old_thread = threading.Thread(
        target=lambda: results.__setitem__("older", manager._persist_snapshot(older))
    )
    new_thread = threading.Thread(
        target=lambda: results.__setitem__("newer", manager._persist_snapshot(newer))
    )
    old_thread.start()
    assert older_serializing.wait(timeout=2.0)
    new_thread.start()
    new_thread.join(timeout=2.0)
    assert not new_thread.is_alive()
    release_older.set()
    old_thread.join(timeout=2.0)
    assert not old_thread.is_alive()

    durable = json.loads(manager.state_path.read_text(encoding="utf-8"))
    durable_agent = next(item for item in durable["agents"] if item["id"] == agent.id)
    assert results == {"newer": True, "older": False}
    assert durable["persistence_generation"] == newer[0]
    assert durable_agent["description"] == "newer"


def test_normal_save_serialization_and_write_do_not_hold_state_lock(
    tmp_path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    agent = manager.create_agent("worker", "initial")
    original_dumps = subagents_module.json.dumps
    original_write_text = Path.write_text
    dump_lock_states: list[bool] = []
    write_lock_states: list[bool] = []

    def observed_dumps(value, *args, **kwargs):
        dump_lock_states.append(manager._lock._is_owned())
        return original_dumps(value, *args, **kwargs)

    def observed_write_text(path, data, *args, **kwargs):
        if path.parent == manager.state_path.parent and path.name.endswith(".tmp"):
            write_lock_states.append(manager._lock._is_owned())
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(subagents_module.json, "dumps", observed_dumps)
    monkeypatch.setattr(Path, "write_text", observed_write_text)

    manager.update_agent(agent.id, description="updated")

    # One full-state serialization and one atomic-temp write per save, both
    # outside the mutable-state lock.  This is deterministic and avoids a
    # machine-speed-dependent wall-clock threshold.
    assert dump_lock_states == [False]
    assert write_lock_states == [False]


def test_outbox_receipt_write_does_not_hold_state_lock(tmp_path, monkeypatch) -> None:
    events = _Events()
    manager = _manager(tmp_path, events)
    agent = manager.create_agent("worker", "does work")
    job = manager.submit("finish", agent_id=agent.id)
    with manager._lock:
        manager._queue_outbox_locked(job, "subagent.completed", "done", 20)
        queued_snapshot = manager._prepare_save_locked()
    manager._persist_snapshot(queued_snapshot)

    original_write_text = Path.write_text
    write_lock_states: list[bool] = []

    def observed_write_text(path, data, *args, **kwargs):
        if path.parent == manager.state_path.parent and path.name.endswith(".tmp"):
            write_lock_states.append(manager._lock._is_owned())
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", observed_write_text)
    manager._drain_outbox()

    assert events.published == ["subagent.completed"]
    assert write_lock_states == [False]


def test_outbox_waits_for_its_snapshot_to_be_durable_before_publish(
    tmp_path, monkeypatch
) -> None:
    events = _Events()
    manager = _manager(tmp_path, events)
    agent = manager.create_agent("worker", "does work")
    job = manager.submit("cancel me", agent_id=agent.id)
    original_write_text = Path.write_text
    write_started = threading.Event()
    release_write = threading.Event()

    def blocking_write_text(path, data, *args, **kwargs):
        if path.parent == manager.state_path.parent and path.name.endswith(".tmp"):
            write_started.set()
            assert release_write.wait(timeout=2.0)
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", blocking_write_text)
    cancel_thread = threading.Thread(target=lambda: manager.cancel_job(job.id))
    cancel_thread.start()
    assert write_started.wait(timeout=2.0)

    # Another concurrent drainer can see the in-memory item, but it must not
    # publish until the generation containing that item has reached disk.
    drain_started = threading.Event()

    def drain() -> None:
        drain_started.set()
        manager._drain_outbox()

    drain_thread = threading.Thread(target=drain)
    drain_thread.start()
    assert drain_started.wait(timeout=2.0)
    assert events.published == []

    release_write.set()
    cancel_thread.join(timeout=2.0)
    drain_thread.join(timeout=2.0)
    assert not cancel_thread.is_alive()
    assert not drain_thread.is_alive()
    assert events.published == ["subagent.cancelled"]


def test_legacy_json_without_persistence_generation_remains_compatible(
    tmp_path,
) -> None:
    state_path = tmp_path / "subagents.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "agents": [],
                "jobs": [],
                "sessions": [],
                "outbox": [],
            }
        ),
        encoding="utf-8",
    )

    manager = _manager(tmp_path)
    assert manager._persistence_generation == 0
    manager.create_agent("worker", "created after legacy load")

    durable = json.loads(state_path.read_text(encoding="utf-8"))
    assert durable["version"] == 1
    assert durable["persistence_generation"] == 1


def test_subagent_state_path_is_single_writer_for_object_lifetime(tmp_path) -> None:
    first = _manager(tmp_path)
    first.create_agent("owner", "holds state writer lock")
    before = first.state_path.read_bytes()

    with pytest.raises(RuntimeError, match="déjà ouvert"):
        _manager(tmp_path)
    assert first.state_path.read_bytes() == before

    # stop/start of the same manager must retain ownership; only close hands
    # the state path to another process/instance.
    first.start()
    first.stop()
    with pytest.raises(RuntimeError, match="déjà ouvert"):
        _manager(tmp_path)
    first.start()
    first.stop()
    first.close()

    second = _manager(tmp_path)
    try:
        assert second.list_agents()[0].name == "owner"
    finally:
        second.close()


def test_subagent_state_lock_is_released_when_process_dies(tmp_path) -> None:
    state_path = tmp_path / "subagents.json"
    code = """
import sys, time
from subagents import SubAgentManager

class LLM:
    model = "stub/model"
    def tool_definitions(self):
        return []

class Events:
    def publish(self, *args, **kwargs):
        return object()

manager = SubAgentManager(
    LLM(), Events(), state_path=sys.argv[1], default_tools=[], emit_progress_events=False
)
print("LOCKED", flush=True)
time.sleep(60)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(state_path)],
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "LOCKED"
        with pytest.raises(RuntimeError, match="déjà ouvert"):
            _manager(tmp_path)
    finally:
        child.kill()
        child.wait(timeout=5)

    reopened = _manager(tmp_path)
    reopened.close()


def test_stop_during_blocked_llm_prevents_followup_tool_and_settles_after_return(
    tmp_path,
) -> None:
    llm = _BlockingLLM()
    events = _SignalingEvents()
    manager = SubAgentManager(
        llm,
        events,
        state_path=tmp_path / "subagents.json",
        workers=1,
        default_tools=["web_search"],
        emit_progress_events=False,
    )
    agent = manager.create_agent(
        "worker", "does work", allowed_tools=["web_search"]
    )
    job = manager.submit("run a tool", agent_id=agent.id)
    manager.start()
    try:
        assert llm.complete_started.wait(timeout=2.0)

        manager.stop(wait=False)
        in_flight = manager.get_job(job.id)
        assert in_flight is not None
        assert in_flight.status.value == "running"
        assert events.published == []

        llm.release_complete.set()
        assert events.failed.wait(timeout=2.0)
        settled = manager.get_job(job.id)
        assert settled is not None
        assert settled.status.value == "failed"
        assert "arrêt" in (settled.error or "").lower()
        assert llm.execute_calls == []

        durable = json.loads(manager.state_path.read_text(encoding="utf-8"))
        durable_job = next(item for item in durable["jobs"] if item["id"] == job.id)
        assert durable_job["status"] == "failed"
    finally:
        llm.release_complete.set()
        manager.stop(wait=True)


def test_draining_stop_never_returns_with_admitted_worker_still_alive(
    tmp_path, monkeypatch
) -> None:
    llm = _BlockingLLM()
    events = _SignalingEvents()
    manager = SubAgentManager(
        llm,
        events,
        state_path=tmp_path / "subagents.json",
        workers=1,
        default_tools=["web_search"],
        emit_progress_events=False,
    )
    agent = manager.create_agent(
        "worker", "does work", allowed_tools=["web_search"]
    )
    manager.submit("run a tool", agent_id=agent.id)
    manager.start()
    assert llm.complete_started.wait(timeout=2.0)
    worker = manager._threads[0]
    original_join = worker.join
    join_timeouts: list[float | None] = []

    def observed_join(timeout=None):
        join_timeouts.append(timeout)
        if timeout is not None:
            # Make the historical two-second timeout return immediately so the
            # regression is deterministic without a wall-clock sleep.
            return original_join(timeout=0.0)
        return original_join()

    monkeypatch.setattr(worker, "join", observed_join)
    stopped = threading.Event()
    stopper = threading.Thread(
        target=lambda: (manager.stop(wait=True), stopped.set())
    )
    stopper.start()
    assert not stopped.wait(timeout=0.05)
    assert worker.is_alive()

    llm.release_complete.set()
    stopper.join(timeout=2.0)
    assert not stopper.is_alive()
    assert not worker.is_alive()
    assert join_timeouts == [None]
    assert manager._threads == []
    assert manager.running is False
