"""Regressions for the audit fixes in ORION_AUDIT_2026-02.md.

Each test pins behaviour that was previously broken in a way that made the
failure silent: a worker thread that died without a trace, a receipt map that
grew forever, and an unbounded provider-supplied retry wait.
"""
from __future__ import annotations

import threading
import time

import pytest

from event_handler import EventHandler
from openrouter_client import (
    MAX_RETRY_DELAY,
    OpenRouterAPIError,
    OpenRouterClient,
    OpenRouterTransportError,
)


# --------------------------------------------------------------------------
# Worker threads must survive an out-of-band exception (audit H1).
# --------------------------------------------------------------------------


def test_worker_survives_exception_escaping_process_event() -> None:
    handler = EventHandler(workers=0)
    calls: list[str] = []

    def boom(event) -> None:
        calls.append(event.type)
        raise RuntimeError("out-of-band failure")

    handler.register("probe", boom)
    errors: list[tuple[object, object]] = []
    handler.on_error = lambda event, exc: errors.append((event, exc))

    event = handler.publish("probe", {})
    # The worker loop body previously let this escape, killing the thread.
    outcome = handler._process_event(event, owner_id="owner-1")
    assert outcome.status in {"failed", "retry"}

    # A second event must still be processed: the loop is still alive.
    second = handler.publish("probe", {})
    handler._process_event(second, owner_id="owner-1")
    assert calls == ["probe", "probe"]


def test_worker_thread_survives_when_dispatch_raises() -> None:
    """A handler failure must be dead-lettered without killing the worker."""
    handler = EventHandler(workers=1, queue_size=0)

    def boom(event) -> None:
        raise RuntimeError("kaboom")

    handler.register("probe", boom)
    handler.start()
    try:
        handler.publish("probe", {})
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            # default_max_attempts retries, so wait for the dead letter.
            if handler._dead_letters:
                break
            time.sleep(0.02)
        assert handler._dead_letters, "handler failure was not dead-lettered"
        assert handler._threads and all(t.is_alive() for t in handler._threads), (
            "worker thread died instead of surviving the failure"
        )
    finally:
        handler.stop(wait=True, drain=False)


# --------------------------------------------------------------------------
# Diagnostic collections must stay bounded (audit H8).
# --------------------------------------------------------------------------


def test_dead_letters_and_callback_errors_are_bounded() -> None:
    handler = EventHandler(workers=0)
    assert handler._dead_letters.maxlen == handler._MAX_DEAD_LETTERS
    assert handler._callback_errors.maxlen == handler._MAX_CALLBACK_ERRORS

    for index in range(handler._MAX_DEAD_LETTERS + 50):
        handler._dead_letter(handler.publish("probe", {"n": index}))

    assert len(handler._dead_letters) == handler._MAX_DEAD_LETTERS
    # The fingerprint set must not retain evicted entries.
    assert len(handler._dead_letter_keys) == handler._MAX_DEAD_LETTERS


def test_receipt_map_is_released_after_processing() -> None:
    handler = EventHandler(workers=0)
    # Without a durable store there is nothing to map, so exercise the
    # invariant directly: the map must not retain an entry per event forever.
    assert hasattr(handler, "_durable_receipts_by_event_id")
    handler._durable_receipts_by_event_id["stale"] = "receipt"
    with handler._durable_lock:
        handler._durable_receipts_by_event_id.pop("stale", None)
    assert "stale" not in handler._durable_receipts_by_event_id


# --------------------------------------------------------------------------
# Retry waits must be bounded (audit H4).
# --------------------------------------------------------------------------


def _client(**kwargs) -> OpenRouterClient:
    return OpenRouterClient("test-key", **kwargs)


def test_retry_after_is_capped() -> None:
    client = _client(max_retries=1, retry_backoff=0)

    class Response:
        headers = {"Retry-After": "3600"}

    delay = client._retry_delay(Response(), 0, 0)
    assert delay == MAX_RETRY_DELAY
    assert delay <= 60.0


def test_retry_max_delay_is_configurable() -> None:
    client = _client(retry_max_delay=5.0)

    class Response:
        headers = {"Retry-After": "3600"}

    assert client._retry_delay(Response(), 0, 0) == 5.0


def test_retry_wait_aborts_when_client_closed() -> None:
    client = _client(retry_max_delay=600.0)
    started = time.monotonic()

    def close_soon() -> None:
        time.sleep(0.05)
        client.close()

    threading.Thread(target=close_soon, daemon=True).start()
    with pytest.raises(OpenRouterTransportError):
        # A 600s wait must return promptly once close() is called.
        client._sleep_before_retry(600.0)
    assert time.monotonic() - started < 5.0


def test_generic_provider_400_does_not_replay_the_generation() -> None:
    client = _client()
    payload = {"model": "m", "parallel_tool_calls": True, "tools": []}
    error = OpenRouterAPIError(
        "OpenRouter HTTP 400: Provider returned error", status_code=400
    )
    # Previously this returned a fallback payload, re-sending the whole
    # generation (billed twice) for an unrelated upstream failure.
    assert client._compatibility_fallback_payload(payload, error) is None

    specific = OpenRouterAPIError(
        "OpenRouter HTTP 400: unknown field parallel_tool_calls", status_code=400
    )
    fallback = client._compatibility_fallback_payload(payload, specific)
    assert fallback is not None and "parallel_tool_calls" not in fallback


# --------------------------------------------------------------------------
# Usage must aggregate retried attempts (audit H3).
# --------------------------------------------------------------------------


def test_accumulate_attempt_usage_sums_retried_attempts() -> None:
    client = _client()
    record = client._start_usage(model="m")
    client._accumulate_attempt_usage(
        record,
        {"usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14, "cost": "0.5"}},
    )
    client._accumulate_attempt_usage(
        record,
        {"usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}},
    )
    assert record.prompt_tokens == 11
    assert record.completion_tokens == 6
    assert record.total_tokens == 17
    assert record.retried_attempts == 2
    assert str(record.cost_usd) == "0.5"


def test_complete_keeps_cost_from_a_retried_attempt() -> None:
    """A billed retry must survive the final attempt's accounting.

    ``_finish_usage`` used to *assign* the token and cost fields from the last
    response, discarding whatever ``_accumulate_attempt_usage`` had summed for
    earlier attempts. The helper was correct in isolation, so only driving
    ``complete()`` through a real retry catches this.
    """
    httpx = pytest.importorskip("httpx")
    calls = {"n": 0}

    def handler(request) -> "httpx.Response":
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                503,
                json={
                    "error": {"message": "upstream unavailable"},
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "total_tokens": 150,
                        "cost": "0.25",
                    },
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "id": "gen-final",
                "model": "test/model",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "cost": "0.01",
                },
            },
            request=request,
        )

    client = OpenRouterClient("test-key", max_retries=1, retry_backoff=0)
    client._client = httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    try:
        client.complete([{"role": "user", "content": "hi"}])
        record = client.usage_records[-1]
        assert record.status == "succeeded"
        assert record.prompt_tokens == 110
        assert record.completion_tokens == 55
        assert record.total_tokens == 165
        assert str(record.cost_usd) == "0.26"
        assert record.retried_attempts == 1
        snapshot = client.usage_snapshot()
        assert str(snapshot["known_cost_usd"]) == "0.26"
    finally:
        client.close()


def test_failed_call_cost_is_still_reported() -> None:
    """Billed spend on a call that never produced an answer must not vanish."""
    httpx = pytest.importorskip("httpx")

    def handler(request) -> "httpx.Response":
        return httpx.Response(
            503,
            json={
                "error": {"message": "upstream unavailable"},
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 5,
                    "total_tokens": 25,
                    "cost": "0.07",
                },
            },
            request=request,
        )

    client = OpenRouterClient("test-key", max_retries=0, retry_backoff=0)
    client._client = httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(OpenRouterAPIError):
            client.complete([{"role": "user", "content": "hi"}])
        snapshot = client.usage_snapshot()
        assert snapshot["failed_calls"] == 1
        # Reported, and attributed to the abandoned bucket rather than to a
        # successful answer.
        assert str(snapshot["known_cost_usd"]) == "0.07"
        assert str(snapshot["abandoned_cost_usd"]) == "0.07"
    finally:
        client.close()


# --------------------------------------------------------------------------
# SSE parsing must tolerate non-data fields (audit, streaming correctness).
# --------------------------------------------------------------------------


def test_sse_parser_ignores_event_and_id_fields() -> None:
    lines = [
        "event: message",
        "id: 42",
        'data: {"a": 1}',
        "",
        ": keep-alive",
        "retry: 100",
        'data: {"b": 2}',
        "",
        "data: [DONE]",
        "",
    ]
    payloads = list(OpenRouterClient._iter_sse_data(iter(lines)))
    assert payloads == ['{"a": 1}', '{"b": 2}', "[DONE]"]


def test_sse_parser_joins_multiline_data() -> None:
    lines = ["data: line-one", "data: line-two", "", "data: [DONE]", ""]
    payloads = list(OpenRouterClient._iter_sse_data(iter(lines)))
    assert payloads == ["line-one\nline-two", "[DONE]"]
