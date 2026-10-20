from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from orion.providers.base import ModelRequest, ProviderError, ToolSpec
from orion.providers.chatgpt import ChatGPTProvider, to_input
from orion.providers.chatgpt_auth import PLAN_SCOPE, ChatGPTAuth, Credentials
from orion.providers.router import Router

WEATHER = ToolSpec(
    name="weather",
    description="Weather in a city",
    parameters={"type": "object", "properties": {"city": {"type": "string"}}},
)


def _signed_in(tmp_path: Path) -> ChatGPTAuth:
    auth = ChatGPTAuth(tmp_path / "chatgpt")
    auth._save(  # pyright: ignore[reportPrivateUsage]
        Credentials(
            client_id="oaiapp_1",
            host_id="urn:uuid:1",
            subject="user-1",
            id_token="x.e30.y",
            access_token="access-1",
            refresh_token="refresh-1",
            expires_at=time.time() + 3600,
            scopes=[PLAN_SCOPE],
        )
    )
    return auth


def _sse(*events: dict[str, Any]) -> httpx.Response:
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def _completed(**response: Any) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "model": "gpt-6-luna",
            "output": [],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 7,
                "input_tokens_details": {"cached_tokens": 64},
            },
            **response,
        },
    }


def _provider(tmp_path: Path, handler: Any) -> ChatGPTProvider:
    return ChatGPTProvider(
        _signed_in(tmp_path), transport=httpx.MockTransport(handler), max_backoff=0.0
    )


def test_journaled_chat_messages_become_responses_items() -> None:
    messages = [
        {"role": "system", "content": "You are Orion."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is this?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
            ],
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "weather", "arguments": '{"city":"Lyon"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "14°C"},
        {"role": "assistant", "content": "Rainy."},
    ]
    items = to_input(messages)
    assert items == [
        {"role": "system", "content": "You are Orion."},
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "What is this?"},
                {"type": "input_image", "image_url": "data:image/png;base64,AA"},
            ],
        },
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "weather",
            "arguments": '{"city":"Lyon"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "14°C"},
        {"role": "assistant", "content": "Rainy."},
    ]
    # Same messages, same bytes: the prefix of the next request is this one's.
    assert json.dumps(to_input(messages)) == json.dumps(items)


async def test_tool_call_is_returned_in_the_journaled_chat_shape(tmp_path: Path) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer access-1"
        sent.append(json.loads(request.content))
        call = {
            "type": "function_call",
            "call_id": "call_9",
            "name": "weather",
            "arguments": '{"city":"Lyon"}',
        }
        return _sse({"type": "response.output_item.done", "item": call}, _completed())

    provider = _provider(tmp_path, handler)
    response = await provider.complete(
        ModelRequest(
            model="gpt-6-luna",
            messages=[{"role": "user", "content": "Weather in Lyon?"}],
            tools=[WEATHER],
            session_id="conv-1",
            max_tokens=500,
            temperature=0,
        )
    )
    await provider.aclose()

    body = sent[0]
    assert body["store"] is False and body["stream"] is True
    assert body["prompt_cache_key"] == "conv-1"
    assert body["reasoning"] == {"effort": "low"}
    assert body["tools"][0]["name"] == "weather"
    assert "temperature" not in body and "max_output_tokens" not in body  # refused by the plan
    assert response.finish_reason == "tool_calls"
    assert response.message == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_9",
                "type": "function",
                "function": {"name": "weather", "arguments": '{"city":"Lyon"}'},
            }
        ],
    }
    assert response.usage.cached_tokens == 64
    assert response.usage.cost_usd == 0.0


async def test_web_search_text_and_citations(tmp_path: Path) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        message = {
            "type": "message",
            "content": [
                {
                    "type": "output_text",
                    "text": 'Voici :\n```json\n{"answer": "PSG won."}\n```',
                    "annotations": [
                        {"type": "url_citation", "url": "https://lemonde.fr/a", "title": "Le Monde"}
                    ],
                }
            ],
        }
        search = {
            "type": "web_search_call",
            "action": {
                "type": "search",
                "sources": [
                    {"type": "url", "url": "https://lemonde.fr/a"},
                    {"type": "url", "url": "https://lequipe.fr/b"},
                ],
            },
        }
        return _sse(
            {"type": "response.output_item.done", "item": search},
            {"type": "response.output_item.done", "item": message},
            _completed(),
        )

    provider = _provider(tmp_path, handler)
    response = await provider.complete(
        ModelRequest(
            model="gpt-6-luna",
            messages=[{"role": "user", "content": "PSG?"}],
            web_search=3,
            json_output=True,
        )
    )
    await provider.aclose()
    assert sent[0]["tools"] == [{"type": "web_search"}]
    assert "text" not in sent[0]  # JSON mode and web search together: refused by OpenAI
    assert response.text == '{"answer": "PSG won."}'
    assert response.message["content"] == response.text
    assert sent[0]["include"] == ["web_search_call.action.sources"]
    assert [(c.url, c.title) for c in response.citations] == [
        ("https://lemonde.fr/a", "Le Monde"),
        ("https://lequipe.fr/b", ""),
    ]


async def test_used_up_plan_is_not_retried_and_pictures_are_refused(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        failed = {
            "type": "response.failed",
            "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}},
        }
        return _sse(failed)

    provider = _provider(tmp_path, handler)
    request = ModelRequest(model="gpt-6-luna", messages=[{"role": "user", "content": "hi"}])
    with pytest.raises(ProviderError, match="allowance used up") as caught:
        await provider.complete(request)
    assert not caught.value.retryable and calls == 1
    with pytest.raises(ProviderError, match="pictures"):
        await provider.complete(request.model_copy(update={"image_output": True}))
    await provider.aclose()


async def test_rejected_token_is_refreshed_then_retried(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        if len(seen) == 1:
            return httpx.Response(401, json={"error": "expired"})
        return _sse(_completed())

    auth = _signed_in(tmp_path)
    refreshed: list[bool] = []

    async def fresh_token() -> str:
        current = auth.load()
        assert current is not None
        if current.expires_at < time.time():
            refreshed.append(True)
            return "access-2"
        return current.access_token

    auth.access_token = fresh_token  # type: ignore[method-assign]
    provider = ChatGPTProvider(auth, transport=httpx.MockTransport(handler), max_backoff=0.0)
    await provider.complete(
        ModelRequest(model="gpt-6-luna", messages=[{"role": "user", "content": "hi"}])
    )
    await provider.aclose()
    assert seen == ["Bearer access-1", "Bearer access-2"] and refreshed


async def test_router_reaches_the_plan_by_prefix_and_embeds_fail_softly(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _sse(_completed())

    provider = _provider(tmp_path, handler)
    router = Router({"chatgpt": provider}, "chatgpt")
    response = await router.complete(
        ModelRequest(model="chatgpt:gpt-6-luna", messages=[{"role": "user", "content": "hi"}])
    )
    assert response.provider == "chatgpt"
    with pytest.raises(ProviderError):
        await router.embed("openrouter:openai/text-embedding-3-small", ["hello"])
    await router.aclose()
