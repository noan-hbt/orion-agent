"""Integration contracts spanning adapters, routing and prompt state.

These tests deliberately exercise the public-ish seams used by the runtime,
without replacing production components with modified test doubles.
"""

from types import SimpleNamespace

import pytest

from channel_adapters import TelegramAdapter
from channels import AgentOutput
from event_handler import DuplicateEventError, EventHandler
from prompt_context import PromptComposer, PromptContextStore


def _telegram_update(update_id=1, *, message_id=30, thread_id=None, text="hello"):
    message = {
        "message_id": message_id,
        "chat": {"id": 10, "type": "private"},
        "from": {"id": 20, "username": "tester"},
        "text": text,
    }
    if thread_id is not None:
        message["message_thread_id"] = thread_id
    return {"update_id": update_id, "message": message}


def test_duplicate_incoming_message_is_enqueued_once(tmp_path):
    adapter = TelegramAdapter("token", allowed_chat_ids=[10], offset_path=str(tmp_path / "offset"))
    updates = [_telegram_update(1), _telegram_update(2)]
    def api(method, payload):
        if method == "getUpdates":
            adapter._stop_requested.set()
            return {"ok": True, "result": updates}
        return {"ok": True}

    adapter._api = api
    adapter._on_message = lambda message: None
    adapter._stop_requested.clear()
    adapter._run()
    queued = []
    while not adapter._queue.empty():
        queued.append(adapter._queue.get_nowait())
        adapter._queue.task_done()
    assert len(queued) == 1
    assert queued[0].message_id == "10:30"


def test_resume_completed_subagent_result_does_not_call_llm():
    from event_handler import Event
    from runtime import AgentRuntime, RunPhase

    calls = []
    runtime = SimpleNamespace(llm_client=SimpleNamespace(chat=lambda *a, **k: calls.append(1)))
    context = SimpleNamespace(event=Event("subagent.completed", {"result": "done"}), answer=None, phase=None)
    AgentRuntime._run_agent_loop(runtime, context)
    assert context.answer == "done"
    assert context.phase is RunPhase.ANSWER
    assert calls == []


def test_telegram_topic_is_propagated_to_outbound_message(tmp_path):
    adapter = TelegramAdapter("token", allowed_chat_ids=[10], offset_path=str(tmp_path / "offset"))
    adapter._seen_chat_ids.add(10)
    calls = []
    adapter._api = lambda method, payload: calls.append((method, payload)) or {"ok": True}
    adapter.send(AgentOutput("reply", channel="telegram", recipient="10", metadata={"message_thread_id": 42}))
    assert calls[-1][0] == "sendMessage"
    assert calls[-1][1]["message_thread_id"] == 42


def test_forgotten_memory_is_not_reinjected():
    store = PromptContextStore(":memory:")
    store.apply_extraction({"memories": ["Le projet secret Orion"]})
    assert "Le projet secret Orion" in PromptComposer(store, context_mode="legacy").compose()
    store.apply_extraction({"forget": ["secret Orion"]})
    assert "Le projet secret Orion" not in PromptComposer(store, context_mode="legacy").compose()


def test_event_idempotency_collision_is_rejected():
    handler = EventHandler()
    handler.publish("message", {"text": "first"}, message_id="same-message")
    with pytest.raises(DuplicateEventError):
        handler.publish("message", {"text": "different"}, message_id="same-message")
