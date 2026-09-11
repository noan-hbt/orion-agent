from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import channel_adapters
from channel_adapters import HttpWebhookAdapter
from channels import AgentOutput, ChannelRouter, InboundMessage
from communication_ledger import CommunicationLedger, IdempotencyConflict
from event_handler import DuplicateEventError, EventHandler
from orion_config import OrionApplication, OrionConfig


def _wait_for(predicate, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


class _Adapter:
    name = "test"

    def __init__(self, ledger: CommunicationLedger | None = None) -> None:
        self.ledger = ledger
        self.sent: list[AgentOutput] = []
        self.send_calls = 0
        self.started = False
        self.failures: list[BaseException] = []
        self.saw_processing_row = False

    def start(self, _on_message) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def send(self, output: AgentOutput) -> None:
        self.send_calls += 1
        if self.ledger is not None and output.idempotency_key:
            row = self.ledger.get_by_event_id(
                output.idempotency_key,
                channel=self.name,
                kind="outbound",
            )
            self.saw_processing_row = bool(row and row["status"] == "processing")
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(output)


def _router(
    ledger: CommunicationLedger,
    adapter: _Adapter,
    *,
    lease_seconds: float = 0.2,
) -> ChannelRouter:
    router = ChannelRouter(
        EventHandler(workers=0),
        default_channel=adapter.name,
        ledger=ledger,
        outbound_retry_backoff=0.01,
        outbound_poll_interval=0.01,
        outbound_lease_seconds=lease_seconds,
    )
    router.register(adapter)
    return router


def test_inbound_native_message_id_is_scoped_by_channel() -> None:
    handler = EventHandler(workers=0)
    router = ChannelRouter(handler)
    received_at = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)

    telegram = InboundMessage(
        channel="telegram",
        source="telegram",
        payload={"text": "telegram"},
        message_id="42",
        correlation_id="corr-telegram",
        received_at=received_at,
    )
    email = InboundMessage(
        channel="email",
        source="email",
        payload={"text": "email"},
        message_id="42",
        correlation_id="corr-email",
        received_at=received_at,
    )

    telegram_event = router.receive(telegram)
    email_event = router.receive(email)

    assert telegram.message_id == "42"
    assert email.message_id == "42"
    assert telegram_event.metadata["message_id"] == "42"
    assert email_event.metadata["message_id"] == "42"
    assert telegram_event.message_id != email_event.message_id
    assert telegram_event.correlation_id == "corr-telegram"
    assert email_event.correlation_id == "corr-email"
    assert handler.queue.qsize() == 2


def test_same_channel_native_message_id_same_content_dedupes() -> None:
    handler = EventHandler(workers=0)
    router = ChannelRouter(handler)
    received_at = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)

    first = router.receive(
        InboundMessage(
            channel="telegram",
            source="telegram",
            payload={"text": "same"},
            message_id="42",
            correlation_id="corr-42",
            received_at=received_at,
        )
    )
    duplicate = router.receive(
        InboundMessage(
            channel="telegram",
            source="telegram",
            payload={"text": "same"},
            message_id="42",
            correlation_id="corr-42",
            received_at=received_at,
        )
    )

    assert duplicate.id == first.id
    assert duplicate.message_id == first.message_id
    assert duplicate.metadata["message_id"] == "42"
    assert handler.queue.qsize() == 1


def test_same_scoped_native_message_id_changed_content_conflicts() -> None:
    handler = EventHandler(workers=0)
    router = ChannelRouter(handler)
    received_at = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)

    router.receive(
        InboundMessage(
            channel="telegram",
            source="telegram",
            payload={"text": "first"},
            message_id="42",
            correlation_id="corr-42",
            received_at=received_at,
        )
    )

    with pytest.raises(DuplicateEventError):
        router.receive(
            InboundMessage(
                channel="telegram",
                source="telegram",
                payload={"text": "changed"},
                message_id="42",
                correlation_id="corr-42",
                received_at=received_at,
            )
        )


def test_router_records_before_transport_send_and_acks_locally(tmp_path: Path) -> None:
    ledger = CommunicationLedger(tmp_path / "communication.sqlite3")
    adapter = _Adapter(ledger)
    router = _router(ledger, adapter)
    router.start()
    try:
        row_id = router.route(
            AgentOutput("hello", event_id="event-1", recipient="recipient-1")
        )

        _wait_for(lambda: ledger.get(row_id)["status"] == "delivered")
        row = ledger.get(row_id)
        assert adapter.send_calls == 1
        assert adapter.saw_processing_row is True
        assert row is not None
        assert row["kind"] == "outbound"
        assert row["attempts"] == 1
    finally:
        router.stop()
        ledger.close()


def test_delivered_replay_is_suppressed_even_if_display_timestamp_changes(tmp_path: Path) -> None:
    ledger = CommunicationLedger(tmp_path / "communication.sqlite3")
    adapter = _Adapter(ledger)
    router = _router(ledger, adapter)
    router.start()
    try:
        first_id = router.route(
            AgentOutput(
                "same answer",
                event_id="event-replay",
                recipient="recipient-1",
                metadata={"timestamp": "2026-09-10T10:00:00+02:00"},
            )
        )
        _wait_for(lambda: ledger.get(first_id)["status"] == "delivered")

        second_id = router.route(
            AgentOutput(
                "same answer",
                event_id="event-replay",
                recipient="recipient-1",
                metadata={"timestamp": "2026-09-10T10:00:05+02:00"},
            )
        )
        time.sleep(0.05)

        assert second_id == first_id
        assert adapter.send_calls == 1
        assert ledger.get(first_id)["status"] == "delivered"
    finally:
        router.stop()
        ledger.close()


def test_same_output_identity_with_changed_payload_fails_closed(tmp_path: Path) -> None:
    ledger = CommunicationLedger(tmp_path / "communication.sqlite3")
    adapter = _Adapter(ledger)
    router = _router(ledger, adapter)
    try:
        router.route(AgentOutput("first", output_id="stable-output", recipient="recipient-1"))

        with pytest.raises(IdempotencyConflict):
            router.route(
                AgentOutput("changed", output_id="stable-output", recipient="recipient-1")
            )
    finally:
        router.stop()
        ledger.close()


def test_expired_crash_claim_is_recovered_at_least_once(tmp_path: Path) -> None:
    ledger = CommunicationLedger(tmp_path / "communication.sqlite3")
    adapter = _Adapter(ledger)
    router = _router(ledger, adapter, lease_seconds=0.05)
    try:
        row_id = router.route(
            AgentOutput("recover me", output_id="recover-output", recipient="recipient-1")
        )
        claimed = ledger.claim_by_id(row_id, worker_id="crashed-worker", lease_seconds=0.03)
        assert claimed is not None
        assert ledger.mark_processing(row_id, worker_id="crashed-worker")
        time.sleep(0.05)

        router.start()
        _wait_for(lambda: ledger.get(row_id)["status"] == "delivered")

        row = ledger.get(row_id)
        assert adapter.send_calls == 1
        assert row is not None
        assert row["attempts"] == 2
    finally:
        router.stop()
        ledger.close()


def test_transient_send_retries_without_second_runtime_route_and_does_not_store_secret(tmp_path: Path) -> None:
    ledger = CommunicationLedger(tmp_path / "communication.sqlite3")
    adapter = _Adapter(ledger)
    adapter.failures.append(ConnectionError("transport failed token=SUPER-SECRET"))
    router = _router(ledger, adapter)
    router.start()
    try:
        row_id = router.route(
            AgentOutput("retry once", output_id="retry-output", recipient="recipient-1")
        )
        _wait_for(lambda: ledger.get(row_id)["status"] == "delivered")

        row = ledger.get(row_id)
        assert adapter.send_calls == 2
        assert len(adapter.sent) == 1
        assert row is not None
        assert row["attempts"] == 2
        assert "SUPER-SECRET" not in str(row["last_error"])
        assert row["last_error"] == "ConnectionError"
    finally:
        router.stop()
        ledger.close()


class _DrainRuntime:
    def __init__(self, router: ChannelRouter) -> None:
        self.router = router
        self.action_ledger = None
        self.approval_store = None
        self.context_registry = None
        self.retrieval_store = None
        self.conversation_journal = None
        self._durable_store = None

    def start(self) -> None:
        return

    def stop(self) -> None:
        # OrionApplication quiesces channels before runtime drain.  This late
        # output must still be durably accepted even though transport workers
        # have already stopped.
        self.router.route(
            AgentOutput("late drain output", output_id="late-output", recipient="recipient-1")
        )


class _Closable:
    def close(self) -> None:
        return


def test_shutdown_keeps_ledger_open_through_runtime_drain_and_restart_recovers(tmp_path: Path) -> None:
    path = tmp_path / "communication.sqlite3"
    ledger = CommunicationLedger(path)
    first_adapter = _Adapter(ledger)
    first_router = _router(ledger, first_adapter)
    events = EventHandler(workers=0)
    app = OrionApplication(
        events=events,
        llm=_Closable(),
        runtime=_DrainRuntime(first_router),
        channels=first_router,
        communication_ledger=ledger,
    )
    app.start()
    app.stop()

    reopened = CommunicationLedger(path)
    second_adapter = _Adapter(reopened)
    second_router = _router(reopened, second_adapter)
    try:
        pending = reopened.pending(kind="outbound")
        assert len(pending) == 1
        row_id = str(pending[0]["id"])
        assert pending[0]["status"] == "queued"

        second_router.start()
        _wait_for(lambda: reopened.get(row_id)["status"] == "delivered")
        assert second_adapter.send_calls == 1
        assert second_adapter.sent[0].content == "late drain output"
    finally:
        second_router.stop()
        reopened.close()


def test_application_build_owns_communication_ledger_without_gateway(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    (tmp_path / "ORION_CORE.md").write_text("core", encoding="utf-8")
    (tmp_path / "tools").mkdir()
    config = OrionConfig.from_mapping(
        {
            "reflection": {"enabled": False},
            "context": {"reflection_enabled": False},
            "subagents": {"enabled": False},
            "scheduler": {"enabled": False},
            "memory": {"enabled": False},
            "channels": {"ledger_path": "data/shared-communication.sqlite3"},
            "tools": {"directory": "tools"},
        }
    )
    config.config_path = tmp_path / "orion.toml"

    app = config.build()
    try:
        assert app.gateway_ledger is None
        assert app.communication_ledger is not None
        assert app.channels.ledger is app.communication_ledger
        assert Path(app.communication_ledger.path) == tmp_path / "data" / "shared-communication.sqlite3"
    finally:
        app.stop()


def test_gateway_adapter_shares_application_communication_ledger(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("ORION_GATEWAY_TOKEN", "gateway-token")
    (tmp_path / "ORION_CORE.md").write_text("core", encoding="utf-8")
    (tmp_path / "tools").mkdir()
    config = OrionConfig.from_mapping(
        {
            "reflection": {"enabled": False},
            "context": {"reflection_enabled": False},
            "subagents": {"enabled": False},
            "scheduler": {"enabled": False},
            "memory": {"enabled": False},
            "gateway": {"enabled": True},
            "tools": {"directory": "tools"},
        }
    )
    config.config_path = tmp_path / "orion.toml"

    app = config.build()
    try:
        gateway = app.channels.adapters["gateway"]
        assert app.gateway_ledger is app.communication_ledger
        assert gateway.ledger is app.communication_ledger
        assert app.channels.ledger is app.communication_ledger
    finally:
        app.stop()


def test_http_outbound_propagates_stable_idempotency_identity(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr(channel_adapters.httpx, "post", fake_post)
    adapter = HttpWebhookAdapter(outbound_url="https://example.com/orion")
    adapter.send(
        AgentOutput(
            "hello",
            recipient="https://example.com/orion",
            idempotency_key="orion-outbound:stable",
        )
    )

    assert captured["headers"] == {"Idempotency-Key": "orion-outbound:stable"}
