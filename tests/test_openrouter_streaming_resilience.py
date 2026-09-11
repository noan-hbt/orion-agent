from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from budgets import BudgetExceeded, BudgetLimits, BudgetTracker
from openrouter_client import OpenRouterClient, OpenRouterTransportError


def _sse(*events: dict) -> bytes:
    chunks = [f"data: {json.dumps(event)}\n\n" for event in events]
    chunks.append("data: [DONE]\n\n")
    return "".join(chunks).encode()


class _Circuit:
    def __init__(self) -> None:
        self.allowed = 0
        self.successes = 0
        self.failures = 0

    def allow(self) -> bool:
        self.allowed += 1
        return True

    def success(self) -> None:
        self.successes += 1

    def failure(self) -> None:
        self.failures += 1


class _SyncLimiter:
    def __init__(self) -> None:
        self.waits = 0

    def wait(self) -> None:
        self.waits += 1


class _AsyncLimiter:
    def __init__(self) -> None:
        self.reservations = 0

    def reserve(self) -> float:
        self.reservations += 1
        return 0.0


def test_sync_stream_retries_before_output_and_accounts_once() -> None:
    requests = []
    final = {
        "id": "gen-1",
        "model": "provider/model",
        "choices": [],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5, "cost": 0.01},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, json={"error": {"message": "retry"}}, request=request)
        return httpx.Response(200, content=_sse(final), headers={"x-request-id": "req-2"}, request=request)

    budget = BudgetTracker()
    client = OpenRouterClient("key", budget=budget, max_retries=1, retry_backoff=0)
    client._client = httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(handler))
    circuit = _Circuit()
    limiter = _SyncLimiter()
    client._circuit = circuit
    client._rate_limiter = limiter
    try:
        events = list(client.stream_chat([{"role": "user", "content": "hello"}]))
    finally:
        client.close()

    assert events == [final]
    assert len(requests) == 2
    assert limiter.waits == 1
    assert circuit.allowed == 1
    assert circuit.successes == 1
    assert circuit.failures == 0
    assert budget.calls == 1
    record = client.usage_records[0]
    assert record.streamed is True
    assert record.status == "succeeded"
    assert record.attempt == 2
    assert record.provider_request_id == "req-2"
    assert record.total_tokens == 5
    assert str(record.cost_usd) == "0.01"
    assert client.usage_snapshot()["completed_calls"] == 1


def test_sync_stream_checks_budget_before_transport() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=_sse({"choices": []}), request=request)

    budget = BudgetTracker(BudgetLimits(max_calls=0))
    client = OpenRouterClient("key", budget=budget)
    client._client = httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(BudgetExceeded):
            list(client.stream_chat([{"role": "user", "content": "hello"}]))
    finally:
        client.close()

    assert requests == 0
    record = client.usage_records[0]
    assert record.status == "failed"
    assert record.error_type == "BudgetExceeded"


class _ExplodingStream(httpx.SyncByteStream):
    def __iter__(self):
        yield _sse({"choices": [{"delta": {"content": "partial"}}]}).split(b"data: [DONE]", 1)[0]
        raise httpx.ReadError("stream broke")


def test_sync_stream_does_not_retry_after_emitting_data() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=_ExplodingStream(), request=request)

    budget = BudgetTracker()
    client = OpenRouterClient("key", budget=budget, max_retries=2, retry_backoff=0)
    client._client = httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(handler))
    circuit = _Circuit()
    client._circuit = circuit
    client._rate_limiter = _SyncLimiter()
    stream = client.stream_chat([{"role": "user", "content": "hello"}])
    first = next(stream)
    assert first["choices"][0]["delta"]["content"] == "partial"
    with pytest.raises(OpenRouterTransportError):
        next(stream)
    client.close()

    assert calls == 1
    assert budget.calls == 0
    assert circuit.failures == 1
    record = client.usage_records[0]
    assert record.status == "failed"
    assert record.attempt == 1


def test_async_stream_applies_resilience_and_accounts_once() -> None:
    async def run() -> None:
        requests = []
        final = {
            "id": "gen-async",
            "choices": [],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7, "cost": "0.02"},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                raise httpx.ConnectError("temporary", request=request)
            return httpx.Response(200, content=_sse(final), request=request)

        budget = BudgetTracker()
        client = OpenRouterClient("key", budget=budget, max_retries=1, retry_backoff=0)
        client._async_client = httpx.AsyncClient(
            base_url="https://example.test", transport=httpx.MockTransport(handler)
        )
        circuit = _Circuit()
        limiter = _AsyncLimiter()
        client._circuit = circuit
        client._rate_limiter = limiter
        events = []
        try:
            async for event in client.stream_chat_async([{"role": "user", "content": "hello"}]):
                events.append(event)
        finally:
            await client.aclose()

        assert events == [final]
        assert len(requests) == 2
        assert limiter.reservations == 1
        assert circuit.allowed == 1
        assert circuit.failures == 1
        assert circuit.successes == 1
        assert budget.calls == 1
        record = client.usage_records[0]
        assert record.status == "succeeded"
        assert record.attempt == 2
        assert record.total_tokens == 7
        assert str(record.cost_usd) == "0.02"
        assert client.usage_snapshot()["completed_calls"] == 1

    asyncio.run(run())
