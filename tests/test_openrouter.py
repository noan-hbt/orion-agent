from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from orion.providers.base import ModelRequest, ProviderError, ToolSpec
from orion.providers.openrouter import OpenRouterProvider

OK_BODY: dict[str, Any] = {
    "model": "deepseek/deepseek-v4.1-flash",
    "provider": "Morph",
    "choices": [
        {
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": "je regarde",
                "reasoning": "internal thoughts that must not be journaled",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "current_time", "arguments": "{}"},
                    }
                ],
            },
        }
    ],
    "usage": {
        "prompt_tokens": 2570,
        "completion_tokens": 40,
        "cost": 0.0000161,
        "prompt_tokens_details": {"cached_tokens": 2560, "cache_write_tokens": 0},
    },
}


def provider_with(handler: Any, **kwargs: Any) -> OpenRouterProvider:
    return OpenRouterProvider(
        "sk-test", transport=httpx.MockTransport(handler), max_backoff=0, **kwargs
    )


def request() -> ModelRequest:
    return ModelRequest(
        model="deepseek/deepseek-v4.1-flash",
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        tools=[ToolSpec(name="current_time", description="d", parameters={"type": "object"})],
        session_id="orion:t1",
    )


async def test_request_shape_and_response_parsing() -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer sk-test"
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=OK_BODY)

    provider = provider_with(handler)
    response = await provider.complete(request())
    await provider.aclose()

    body = seen[0]
    assert body["session_id"] == "orion:t1"
    assert body["usage"] == {"include": True}
    assert body["tools"][0]["function"]["name"] == "current_time"

    assert response.tool_calls[0].name == "current_time"
    assert response.usage.cached_tokens == 2560
    assert response.usage.cache_hit_ratio == pytest.approx(2560 / 2570)
    assert response.provider == "Morph"
    assert "reasoning" not in response.message  # only content and tool calls are kept
    assert response.message["tool_calls"][0]["function"]["arguments"] == "{}"


async def test_retries_retryable_status_then_succeeds() -> None:
    statuses = iter([429, 503, 200])

    def handler(_req: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return httpx.Response(status, json=OK_BODY if status == 200 else {"error": "busy"})

    provider = provider_with(handler)
    response = await provider.complete(request())
    await provider.aclose()
    assert response.attempts == 3


async def test_non_retryable_error_is_raised_immediately() -> None:
    calls = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    provider = provider_with(handler)
    with pytest.raises(ProviderError) as info:
        await provider.complete(request())
    await provider.aclose()
    assert calls == 1
    assert info.value.status == 400 and not info.value.retryable


async def test_gives_up_after_max_attempts() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    provider = provider_with(handler, max_attempts=2)
    with pytest.raises(ProviderError) as info:
        await provider.complete(request())
    await provider.aclose()
    assert info.value.retryable


async def test_error_inside_200_body_is_surfaced() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"code": 400, "message": "context too long"}})

    provider = provider_with(handler)
    with pytest.raises(ProviderError, match="context too long"):
        await provider.complete(request())
    await provider.aclose()
