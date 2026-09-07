from __future__ import annotations

from pathlib import Path

import pytest

from channels import AgentOutput
from channel_adapters import TelegramAdapter, escape_telegram_markdown_v2


class FakeTelegram:
    def __init__(self, updates):
        self.updates = updates
        self.calls = []

    def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method == "getUpdates":
            result, self.updates = self.updates, []
            return {"ok": True, "result": result}
        return {"ok": True, "result": {}}


def update(update_id=1, chat_id=10, user_id=20, message_id=30, text="hello"):
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "chat": {"id": chat_id},
            "from": {"id": user_id, "username": "tester"},
            "text": text,
        },
    }


def run_once(adapter, updates):
    fake = FakeTelegram(updates)
    received = []
    calls = 0
    adapter._stop_requested.clear()

    def api(method, payload):
        nonlocal calls
        calls += 1
        result = fake(method, payload)
        if calls > 1:
            adapter._stop_requested.set()
        return result

    adapter._api = api

    def callback(message):
        received.append(message)
        adapter._stop_requested.set()

    adapter._on_message = callback
    adapter._run()
    # ``_run`` is the polling thread; the worker is intentionally not started
    # in this unit helper, so inspect the accepted queue item directly.
    while True:
        try:
            item = adapter._queue.get_nowait()
        except Exception:
            break
        if item is not None:
            received.append(item)
        adapter._queue.task_done()
    return received, fake.calls


def test_allowlist_is_fail_closed_and_ids_are_normalized(tmp_path: Path):
    adapter = TelegramAdapter("token", offset_path=str(tmp_path / "offset"))
    received, _ = run_once(adapter, [update()])
    assert received == []
    assert adapter.offset == 2

    adapter = TelegramAdapter(
        "token", allowed_chat_ids=[10], offset_path=str(tmp_path / "offset2")
    )
    received, _ = run_once(adapter, [update()])
    assert received[0].message_id == "10:30"
    assert received[0].payload["conversation_id"] == "10"
    assert received[0].payload["update_id"] == 1


def test_queue_full_does_not_advance_offset(tmp_path: Path):
    adapter = TelegramAdapter(
        "token", allowed_chat_ids=[10], queue_size=1, offset_path=str(tmp_path / "offset")
    )
    fake = FakeTelegram([update(1), update(2, message_id=31)])

    def api(method, payload):
        result = fake(method, payload)
        adapter._stop_requested.set()
        return result

    adapter._api = api
    adapter._on_message = lambda _message: None
    adapter._stop_requested.set()  # stop after the first polling cycle
    adapter._stop_requested.clear()
    # Process synchronously with a pre-filled bounded queue.
    adapter._queue.put_nowait(None)
    adapter._run()
    assert adapter.offset == 0


def test_markdown_v2_escaping_and_outbound_allowlist(tmp_path: Path):
    assert escape_telegram_markdown_v2("a_b [c]!") == r"a\_b \[c\]\!"
    adapter = TelegramAdapter("token", allowed_chat_ids=[10], offset_path=str(tmp_path / "offset"))
    calls = []
    adapter._api = lambda method, payload: calls.append((method, payload)) or {"ok": True}
    adapter.send(AgentOutput("a_b", recipient="10"))
    assert calls[0][1]["parse_mode"] == "HTML"
    with pytest.raises(RuntimeError):
        adapter.send(AgentOutput("no", recipient="99"))


def test_first_message_bootstraps_owner_and_persists_across_restart(tmp_path: Path):
    owner_path = tmp_path / "telegram.owner"
    adapter = TelegramAdapter(
        "token",
        owner_path=str(owner_path),
        offset_path=str(tmp_path / "offset"),
    )
    received, _ = run_once(adapter, [update(chat_id=10, user_id=20)])

    assert len(received) == 1
    assert owner_path.read_text(encoding="utf-8") == '{"chat_id": 10, "user_id": 20}'

    restarted = TelegramAdapter(
        "token",
        owner_path=str(owner_path),
        offset_path=str(tmp_path / "offset-restarted"),
    )
    accepted, _ = run_once(restarted, [update(chat_id=10, user_id=20, message_id=31)])
    rejected, _ = run_once(restarted, [update(chat_id=99, user_id=88, message_id=32)])
    assert len(accepted) == 1
    assert rejected == []
