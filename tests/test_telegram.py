from __future__ import annotations

import time
from pathlib import Path

import pytest

from channels import AgentOutput
from channel_adapters import TelegramAdapter, escape_telegram_markdown_v2
from communication_ledger import CommunicationLedger


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


def update(
    update_id=1,
    chat_id=10,
    user_id=20,
    message_id=30,
    text="hello",
    *,
    chat_type="private",
):
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "chat": {"id": chat_id, "type": chat_type},
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


def _wait_for(predicate, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def test_allowlist_is_fail_closed_and_ids_are_normalized(tmp_path: Path):
    adapter = TelegramAdapter(
        "token",
        bootstrap_owner=False,
        offset_path=str(tmp_path / "offset"),
    )
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


def test_explicit_outbound_allowlist_is_not_widened_by_seen_chats(tmp_path: Path):
    adapter = TelegramAdapter(
        "token",
        allowed_chat_ids=[99],
        outbound_allowed_chat_ids=[10],
        offset_path=str(tmp_path / "offset"),
    )
    adapter._seen_chat_ids.add(99)
    calls = []
    adapter._api = lambda method, payload: calls.append((method, payload)) or {"ok": True}

    with pytest.raises(RuntimeError):
        adapter.send(AgentOutput("blocked", recipient="99"))
    adapter.send(AgentOutput("allowed", recipient="10"))
    assert [item[1]["chat_id"] for item in calls] == [10]


def test_legacy_unconfigured_bootstrap_owner_never_claims_first_private_dm(tmp_path: Path):
    owner_path = tmp_path / "telegram.owner"
    adapter = TelegramAdapter(
        "token",
        owner_path=str(owner_path),
        offset_path=str(tmp_path / "offset"),
    )
    received, _ = run_once(adapter, [update(chat_id=10, user_id=20)])

    assert received == []
    assert not owner_path.exists()


def test_previously_persisted_telegram_owner_remains_authorized(tmp_path: Path):
    owner_path = tmp_path / "telegram.owner"
    owner_path.write_text('{"chat_id": 10, "user_id": 20}', encoding="utf-8")

    restarted = TelegramAdapter(
        "token",
        owner_path=str(owner_path),
        offset_path=str(tmp_path / "offset-restarted"),
    )
    accepted, _ = run_once(restarted, [update(chat_id=10, user_id=20, message_id=31)])
    rejected, _ = run_once(restarted, [update(chat_id=99, user_id=88, message_id=32)])
    assert len(accepted) == 1
    assert rejected == []


def test_group_message_cannot_bootstrap_owner(tmp_path: Path):
    owner_path = tmp_path / "telegram.owner"
    adapter = TelegramAdapter(
        "token",
        owner_path=str(owner_path),
        offset_path=str(tmp_path / "offset"),
    )

    received, _ = run_once(
        adapter,
        [update(chat_id=10, user_id=20, chat_type="group")],
    )

    assert received == []
    assert not owner_path.exists()


def test_pairing_secret_requires_explicit_correct_private_pair_command(tmp_path: Path):
    owner_path = tmp_path / "telegram.owner"
    adapter = TelegramAdapter(
        "token",
        bootstrap_pairing_secret="correct-secret",
        owner_path=str(owner_path),
        offset_path=str(tmp_path / "offset"),
    )

    ordinary, _ = run_once(adapter, [update(update_id=1, text="hello")])
    wrong, _ = run_once(
        adapter,
        [update(update_id=2, message_id=31, text="/pair wrong-secret")],
    )
    assert ordinary == []
    assert wrong == []
    assert not owner_path.exists()

    paired, _ = run_once(
        adapter,
        [update(update_id=3, message_id=32, text="/pair correct-secret")],
    )
    assert paired == []
    assert owner_path.read_text(encoding="utf-8") == '{"chat_id": 10, "user_id": 20}'

    accepted, _ = run_once(
        adapter,
        [update(update_id=4, message_id=33, text="after pairing")],
    )
    assert len(accepted) == 1
    assert accepted[0].text == "after pairing"


def test_group_cannot_pair_even_with_correct_secret(tmp_path: Path):
    owner_path = tmp_path / "telegram.owner"
    adapter = TelegramAdapter(
        "token",
        bootstrap_pairing_secret="correct-secret",
        owner_path=str(owner_path),
        offset_path=str(tmp_path / "offset"),
    )

    received, _ = run_once(
        adapter,
        [
            update(
                chat_id=-100,
                user_id=20,
                text="/pair correct-secret",
                chat_type="group",
            )
        ],
    )

    assert received == []
    assert not owner_path.exists()


def test_pairing_command_and_secret_never_reach_inbound_or_ledger(tmp_path: Path):
    secret = "never-log-this"
    ledger = CommunicationLedger(tmp_path / "communication.sqlite3")
    try:
        adapter = TelegramAdapter(
            "token",
            bootstrap_pairing_secret=secret,
            owner_path=str(tmp_path / "telegram.owner"),
            offset_path=str(tmp_path / "offset"),
            ledger=ledger,
        )

        paired, _ = run_once(
            adapter,
            [update(text=f"/pair {secret}")],
        )

        assert paired == []
        assert ledger.pending(limit=20) == []
        assert secret not in (tmp_path / "telegram.owner").read_text(encoding="utf-8")

        accepted, _ = run_once(
            adapter,
            [update(update_id=2, message_id=31, text="normal message")],
        )
        assert len(accepted) == 1
        assert secret not in repr(accepted[0])
        assert "/pair" not in repr(accepted[0])
    finally:
        ledger.close()


def test_paired_owner_persists_across_restart_without_repairing(tmp_path: Path):
    owner_path = tmp_path / "telegram.owner"
    first = TelegramAdapter(
        "token",
        bootstrap_pairing_secret="correct-secret",
        owner_path=str(owner_path),
        offset_path=str(tmp_path / "offset"),
    )
    paired, _ = run_once(first, [update(text="/pair correct-secret")])
    assert paired == []

    restarted = TelegramAdapter(
        "token",
        bootstrap_pairing_secret="correct-secret",
        owner_path=str(owner_path),
        offset_path=str(tmp_path / "offset-restarted"),
    )
    accepted, _ = run_once(
        restarted,
        [update(update_id=2, message_id=31, text="normal owner message")],
    )
    repair, _ = run_once(
        restarted,
        [update(update_id=3, message_id=32, text="/pair correct-secret")],
    )

    assert len(accepted) == 1
    assert accepted[0].text == "normal owner message"
    assert repair == []


def test_durable_telegram_restart_hydrates_even_if_provider_offset_advanced(tmp_path: Path):
    path = tmp_path / "communication.sqlite3"
    offset_path = tmp_path / "telegram.offset"
    first_ledger = CommunicationLedger(path)
    first = TelegramAdapter(
        "token",
        allowed_chat_ids=[10],
        offset_path=str(offset_path),
        ledger=first_ledger,
    )
    accepted, _ = run_once(
        first,
        [update(update_id=10, message_id=30, text="survive restart")],
    )
    assert len(accepted) == 1
    assert first.offset == 0
    assert first_ledger.get("10:30")["status"] == "queued"
    first.client.close()
    first_ledger.close()

    # Emulate a database produced by the old unsafe ordering where the provider
    # cursor had already advanced before the durable row was ACKed.
    offset_path.write_text("11", encoding="utf-8")
    second_ledger = CommunicationLedger(path)
    restarted = TelegramAdapter(
        "token",
        allowed_chat_ids=[10],
        offset_path=str(offset_path),
        ledger=second_ledger,
    )
    restarted._run = lambda: None
    received = []
    restarted.start(lambda message: received.append(message.text))
    try:
        _wait_for(lambda: second_ledger.get("10:30")["status"] == "delivered")
        assert received == ["survive restart"]
        assert restarted.offset == 11
    finally:
        restarted.stop()
        second_ledger.close()


def test_durable_telegram_callback_failure_retries_before_offset_ack(tmp_path: Path):
    ledger = CommunicationLedger(tmp_path / "communication.sqlite3")
    offset_path = tmp_path / "telegram.offset"
    payload = {
        "text": "retry me",
        "chat_id": 10,
        "user_id": 20,
        "update_id": 1,
        "message_id": "10:30",
        "conversation_id": "10",
        "message_thread_id": None,
    }
    row_id, _ = ledger.record_inbound(
        channel="telegram",
        payload=payload,
        message_id="10:30",
        event_id="telegram:update:1",
        correlation_id="telegram:update:1",
        reply_to="10",
    )
    adapter = TelegramAdapter(
        "token",
        allowed_chat_ids=[10],
        offset_path=str(offset_path),
        ledger=ledger,
    )
    adapter._run = lambda: None
    calls = []

    def callback(message):
        calls.append(message.text)
        if len(calls) == 1:
            raise RuntimeError("temporary")

    adapter.start(callback)
    try:
        _wait_for(lambda: ledger.get(row_id)["status"] == "failed")
        assert adapter.offset == 0
        _wait_for(lambda: ledger.get(row_id)["status"] == "delivered")
        _wait_for(lambda: adapter.offset == 2)
        assert calls == ["retry me", "retry me"]
        assert ledger.get(row_id)["attempts"] == 2
        assert adapter.offset == 2
        assert offset_path.read_text(encoding="utf-8") == "2"
    finally:
        adapter.stop()
        ledger.close()
