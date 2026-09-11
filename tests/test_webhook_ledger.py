from __future__ import annotations

import threading
import time

import pytest

import channel_adapters
from channel_adapters import HttpWebhookAdapter
from channels import AgentOutput
from communication_ledger import CommunicationLedger
from orion_config import OrionConfig


def _run_worker(adapter: HttpWebhookAdapter) -> threading.Thread:
    adapter._stop_requested.clear()
    worker = threading.Thread(target=adapter._worker_loop, daemon=True)
    worker.start()
    return worker


def _stop_worker(adapter: HttpWebhookAdapter, worker: threading.Thread) -> None:
    adapter._stop_requested.set()
    worker.join(timeout=2.0)


def _wait_for(predicate, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def test_webhook_success_claims_then_marks_ledger_delivered(tmp_path):
    ledger = CommunicationLedger(tmp_path / "communication.sqlite3")
    adapter = HttpWebhookAdapter(ledger=ledger)
    received = []
    adapter._on_message = lambda message: received.append(message.payload["text"])
    worker = _run_worker(adapter)
    try:
        assert adapter.receive_payload({"message_id": "web-1", "text": "hello"})
        adapter._queue.join()

        row = ledger.get("web-1")
        assert received == ["hello"]
        assert row is not None
        assert row["status"] == "delivered"
        assert row["attempts"] == 1
        assert row["lease_owner"] is None
    finally:
        _stop_worker(adapter, worker)
        ledger.close()


def test_webhook_callback_failure_moves_ledger_to_failed_and_can_retry(tmp_path):
    ledger = CommunicationLedger(tmp_path / "communication.sqlite3")
    adapter = HttpWebhookAdapter(ledger=ledger)
    calls = []

    def failing(message):
        calls.append(message.payload["text"])
        raise RuntimeError("boom")

    adapter._on_message = failing
    worker = _run_worker(adapter)
    try:
        payload = {"message_id": "web-2", "text": "retry me"}
        assert adapter.receive_payload(payload)
        adapter._queue.join()

        failed = ledger.get("web-2")
        assert calls == ["retry me"]
        assert failed is not None
        assert failed["status"] == "failed"
        assert failed["attempts"] == 1
        assert failed["last_error"] == "RuntimeError"

        adapter._on_message = lambda message: calls.append("retry:" + message.payload["text"])
        _wait_for(lambda: ledger.get("web-2")["status"] == "delivered")

        delivered = ledger.get("web-2")
        assert calls == ["retry me", "retry:retry me"]
        assert delivered is not None
        assert delivered["status"] == "delivered"
        assert delivered["attempts"] == 2
    finally:
        _stop_worker(adapter, worker)
        ledger.close()


def test_webhook_duplicate_does_not_reexecute_delivered_or_inflight(tmp_path):
    ledger = CommunicationLedger(tmp_path / "communication.sqlite3")
    adapter = HttpWebhookAdapter(ledger=ledger)
    calls = []
    adapter._on_message = lambda message: calls.append(message.payload["text"])
    worker = _run_worker(adapter)
    try:
        delivered_payload = {"message_id": "web-delivered", "text": "once"}
        assert adapter.receive_payload(delivered_payload)
        adapter._queue.join()
        assert adapter.receive_payload(delivered_payload)
        adapter._queue.join()
        assert calls == ["once"]
        assert ledger.get("web-delivered")["status"] == "delivered"

        # Stop the local worker before constructing a row owned elsewhere;
        # otherwise the new durable scanner is intentionally allowed to claim
        # ready rows on its own.
        _stop_worker(adapter, worker)

        inflight_payload = {"message_id": "web-inflight", "text": "owned elsewhere"}
        row_id, is_new = ledger.record(
            channel=adapter.name,
            payload=inflight_payload,
            message_id="web-inflight",
        )
        assert is_new
        assert ledger.claim_by_id(row_id, worker_id="other-worker") is not None
        assert adapter.receive_payload(inflight_payload)
        adapter._queue.join()

        assert calls == ["once"]
        assert ledger.get("web-inflight")["status"] == "claimed"
        assert ledger.get("web-inflight")["lease_owner"] == "other-worker"
    finally:
        _stop_worker(adapter, worker)
        ledger.close()


def test_webhook_restart_hydrates_durable_accepted_input(tmp_path):
    path = tmp_path / "communication.sqlite3"
    first_ledger = CommunicationLedger(path)
    first = HttpWebhookAdapter(ledger=first_ledger, port=0)
    first._on_message = lambda _message: None
    assert first.receive_payload({"message_id": "restart-1", "text": "survive"})
    assert first_ledger.get("restart-1")["status"] == "queued"
    # Simulate process loss: the RAM queue disappears without a clean stop.
    first_ledger.close()

    second_ledger = CommunicationLedger(path)
    second = HttpWebhookAdapter(ledger=second_ledger, port=0)
    received = []
    second.start(lambda message: received.append(message.payload["text"]))
    try:
        _wait_for(lambda: second_ledger.get("restart-1")["status"] == "delivered")
        assert received == ["survive"]
        assert second_ledger.get("restart-1")["attempts"] == 1
    finally:
        second.stop()
        second_ledger.close()


def test_webhook_clean_stop_drains_all_accepted_rows(tmp_path):
    ledger = CommunicationLedger(tmp_path / "communication.sqlite3")
    adapter = HttpWebhookAdapter(ledger=ledger, port=0, queue_size=1)
    entered = threading.Event()
    release = threading.Event()
    received = []

    def callback(message):
        received.append(message.message_id)
        if len(received) == 1:
            entered.set()
            assert release.wait(timeout=2.0)

    adapter.start(callback)
    assert adapter.receive_payload({"message_id": "stop-1", "text": "one"})
    assert entered.wait(timeout=2.0)
    assert adapter.receive_payload({"message_id": "stop-2", "text": "two"})
    # The RAM queue is now full while stop-1 is executing.  stop-3 is still
    # accepted durably (the HTTP surface would return 202) and must be drained
    # from SQLite during the same clean shutdown.
    assert adapter.receive_payload({"message_id": "stop-3", "text": "three"})

    stopped = threading.Event()
    stopper = threading.Thread(target=lambda: (adapter.stop(), stopped.set()), daemon=True)
    stopper.start()
    time.sleep(0.05)
    assert not stopped.is_set()
    release.set()
    stopper.join(timeout=3.0)
    try:
        assert stopped.is_set()
        assert ledger.get("stop-1")["status"] == "delivered"
        assert ledger.get("stop-2")["status"] == "delivered"
        assert ledger.get("stop-3")["status"] == "delivered"
        assert received == ["stop-1", "stop-2", "stop-3"]
    finally:
        release.set()
        adapter.stop()
        ledger.close()


def test_hmac_nonce_replay_is_rejected_after_restart(tmp_path):
    path = tmp_path / "communication.sqlite3"
    timestamp = time.time()
    headers = {
        "X-Orion-Timestamp": str(timestamp),
        "X-Orion-Nonce": "restart-safe-nonce",
    }
    first_ledger = CommunicationLedger(path)
    first = HttpWebhookAdapter(hmac_secret="secret", ledger=first_ledger)
    assert first._check_replay(headers) is True
    assert first._check_replay(headers) is False
    first_ledger.close()

    second_ledger = CommunicationLedger(path)
    try:
        restarted = HttpWebhookAdapter(hmac_secret="secret", ledger=second_ledger)
        assert restarted._check_replay(headers) is False
    finally:
        second_ledger.close()


def test_http_full_url_allowlist_enforces_path_and_query(monkeypatch):
    sent = []

    class Response:
        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        channel_adapters.httpx,
        "post",
        lambda url, **_kwargs: sent.append(url) or Response(),
    )
    adapter = HttpWebhookAdapter(
        outbound_allowlist=["https://example.com/orion/reply?slot=1"]
    )
    adapter.send(
        AgentOutput(
            "ok",
            recipient="https://example.com/orion/reply?slot=1",
        )
    )
    with pytest.raises(RuntimeError):
        adapter.send(AgentOutput("blocked", recipient="https://example.com/admin/delete?slot=1"))
    with pytest.raises(RuntimeError):
        adapter.send(AgentOutput("blocked", recipient="https://example.com/orion/reply?slot=2"))
    assert sent == ["https://example.com/orion/reply?slot=1"]


def test_named_web_channel_wires_source_allowlist():
    config = OrionConfig.from_mapping(
        {
            "channels": {
                "enabled": ["web"],
                "web": {
                    "host": "127.0.0.1",
                    "allowlist": ["127.0.0.2"],
                },
            }
        }
    )

    class Router:
        def __init__(self):
            self.registered = []

        def register(self, adapter):
            self.registered.append(adapter)

    router = Router()
    config._configure_channels(router)
    assert len(router.registered) == 1
    assert router.registered[0].source_allowlist == ("127.0.0.2",)
