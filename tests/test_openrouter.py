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


async def test_routing_prefers_the_fastest_host_when_asked() -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=OK_BODY)

    for provider in (provider_with(handler, sort="throughput"), provider_with(handler)):
        await provider.complete(request())
        await provider.aclose()
    assert seen[0]["provider"] == {"sort": "throughput"}
    assert "provider" not in seen[1]


async def test_anthropic_models_get_cache_breakpoints_on_the_wire_only() -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=OK_BODY)

    claude = request().model_copy(update={"model": "anthropic/claude-haiku-5.5"})
    claude.messages.append({"role": "assistant", "content": "", "tool_calls": []})
    provider = provider_with(handler)
    await provider.complete(claude)
    await provider.complete(request())
    await provider.aclose()
    system, user, assistant = seen[0]["messages"]
    breakpoint_ = {"type": "ephemeral"}
    assert system["content"] == [{"type": "text", "text": "s", "cache_control": breakpoint_}]
    assert user["content"] == [{"type": "text", "text": "u", "cache_control": breakpoint_}]
    assert assistant["content"] == ""  # nothing to mark: the last message with text was marked
    assert claude.messages[0]["content"] == "s"  # what the core journals is untouched
    assert seen[1]["messages"][0]["content"] == "s"  # other models cache by themselves


async def test_web_search_runs_on_the_provider_and_its_sources_come_back() -> None:
    seen: list[dict[str, Any]] = []
    body = json.loads(json.dumps(OK_BODY))
    body["choices"][0]["message"]["annotations"] = [
        {"type": "url_citation", "url_citation": {"url": "https://a.fr/x", "title": "A"}},
        {"type": "other"},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=body)

    provider = provider_with(handler)
    response = await provider.complete(request().model_copy(update={"web_search": 4}))
    assert seen[0]["plugins"] == [{"id": "web", "max_results": 4}]
    assert [(c.url, c.title) for c in response.citations] == [("https://a.fr/x", "A")]
    await provider.complete(request())
    assert "plugins" not in seen[1]
    await provider.aclose()


async def test_a_closing_call_keeps_the_tools_but_forbids_them() -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=OK_BODY)

    provider = provider_with(handler)
    await provider.complete(request())
    await provider.complete(request().model_copy(update={"no_tools": True}))
    await provider.aclose()
    assert "tool_choice" not in seen[0]
    assert seen[1]["tool_choice"] == "none"
    assert seen[1]["tools"] == seen[0]["tools"]
