"""The owner's ChatGPT plan as a model provider (D62): `chatgpt:<slug>` model specs.

Requests go to OpenAI's public Responses API with the Sign in with ChatGPT access token
(`chatgpt_auth`), streamed and never stored (both required). Orion's messages stay in the chat
format it journals; they are translated to Responses input items on the wire only, the same
way every time, so the journal and the prompt prefix are untouched (prompt-cache contract).

What the plan does not offer is dropped or refused here rather than half-done: `temperature`
and `max_output_tokens` are rejected by the server (dropped: a cap is a cost guard, and the
plan has no per-call cost), pictures cannot be generated (refused, so the router falls back),
and there are no embeddings. Calls cost nothing per token: `cost_usd` stays 0, and the plan's
own weekly allowance is the limit.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any, cast

import httpx
import structlog

from orion.providers.base import (
    Citation,
    Message,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ToolCall,
    Usage,
)
from orion.providers.chatgpt_auth import ChatGPTAuth, ChatGPTAuthError

log = structlog.get_logger(__name__)

API_URL = "https://api.openai.com/v1"
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
_PLAN_EXHAUSTED = {
    "subscription_sharing_usage_limit_exceeded",
    "subscription_sharing_usage_unavailable",
}
_EFFORT = {"minimal": "low", "low": "low", "medium": "medium", "high": "high"}


def _content(content: object) -> object:
    """Chat content parts to Responses input parts; plain text stays a string."""
    if not isinstance(content, list):
        return content if content is not None else ""
    parts: list[dict[str, Any]] = []
    for part in cast(list[dict[str, Any]], content):
        kind = part.get("type")
        if kind == "text":
            parts.append({"type": "input_text", "text": part.get("text", "")})
        elif kind == "image_url":
            image = cast(dict[str, Any], part.get("image_url") or {})
            parts.append({"type": "input_image", "image_url": image.get("url", "")})
        else:
            raise ProviderError(f"unsupported message part {kind!r}", retryable=False)
    return parts


def to_input(messages: list[Message]) -> list[dict[str, Any]]:
    """Orion's journaled chat messages as Responses input items, deterministically."""
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            output = message.get("content")
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id", ""),
                    "output": output if isinstance(output, str) else json.dumps(output),
                }
            )
            continue
        if role == "assistant":
            if message.get("content"):
                items.append({"role": "assistant", "content": message["content"]})
            for call in cast(list[dict[str, Any]], message.get("tool_calls") or []):
                function = cast(dict[str, Any], call.get("function") or {})
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.get("id", ""),
                        "name": function.get("name", ""),
                        "arguments": function.get("arguments") or "{}",
                    }
                )
            continue
        if role not in ("system", "developer", "user"):
            raise ProviderError(f"unsupported message role {role!r}", retryable=False)
        items.append({"role": role, "content": _content(message.get("content"))})
    return items


async def plan_models(auth: ChatGPTAuth, http: httpx.AsyncClient) -> list[tuple[str, str]]:
    """The models the signed-in plan offers, as (slug, display name), in the server's order."""
    try:
        token = await auth.access_token()
    except ChatGPTAuthError as exc:
        raise ProviderError(f"ChatGPT sign-in: {exc}", retryable=False) from exc
    response = await http.get(f"{API_URL}/models", headers={"Authorization": f"Bearer {token}"})
    if response.status_code != 200:
        raise ProviderError(
            f"HTTP {response.status_code}: {response.text[:300]}",
            retryable=False,
            status=response.status_code,
        )
    rows = cast(list[dict[str, Any]], response.json().get("models") or [])
    return [
        (str(row["slug"]), str(row.get("display_name") or row["slug"]))
        for row in rows
        if row.get("visibility") == "list" and row.get("slug")
    ]


class ChatGPTProvider:
    name = "chatgpt"

    def __init__(
        self,
        auth: ChatGPTAuth,
        *,
        base_url: str = API_URL,
        timeout: float = 180.0,
        max_attempts: int = 4,
        max_backoff: float = 20.0,
        default_effort: str | None = "low",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._auth = auth
        self._default_effort = default_effort
        """With no effort asked, reasoning models think at length: a text reply should not."""
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)
        self._max_attempts = max_attempts
        self._max_backoff = max_backoff

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.image_output:
            raise ProviderError("the ChatGPT plan cannot make pictures here", retryable=False)
        body = self.body(request)
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._stream(body, attempt)
            except ProviderError as exc:
                if not exc.retryable or attempt >= self._max_attempts:
                    raise
                base = min(self._max_backoff, 0.5 * 2 ** (attempt - 1))
                delay = min(self._max_backoff, base * (0.5 + random.random()))
                log.warning(
                    "provider.retry",
                    provider=self.name,
                    attempt=attempt,
                    status=exc.status,
                    delay=round(delay, 2),
                    error=str(exc),
                )
                await asyncio.sleep(delay)

    def body(self, request: ModelRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model,
            "input": to_input(request.messages),
            "store": False,
            "stream": True,
        }
        tools: list[dict[str, Any]] = [
            {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
                "strict": False,
            }
            for t in request.tools
        ]
        if request.web_search is not None:
            tools.append({"type": "web_search"})
        if tools:
            body["tools"] = tools
            if request.no_tools:
                body["tool_choice"] = "none"
        if request.session_id:
            body["prompt_cache_key"] = request.session_id
        effort = (
            _EFFORT[request.reasoning_effort] if request.reasoning_effort else self._default_effort
        )
        if effort is not None:
            body["reasoning"] = {"effort": effort}
        if request.json_output:
            body["text"] = {"format": {"type": "json_object"}}
        return body

    async def _stream(self, body: dict[str, Any], attempt: int) -> ModelResponse:
        try:
            token = await self._auth.access_token()
        except ChatGPTAuthError as exc:
            raise ProviderError(f"ChatGPT sign-in: {exc}", retryable=False) from exc
        headers = {"Authorization": f"Bearer {token}"}
        items: list[dict[str, Any]] = []
        try:
            async with self._client.stream(
                "POST", "/responses", json=body, headers=headers
            ) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", "replace")
                    if response.status_code == 401:
                        self._auth.expire()
                    raise ProviderError(
                        f"HTTP {response.status_code}: {text[:500]}",
                        retryable=response.status_code in _RETRYABLE_STATUS
                        or response.status_code == 401,
                        status=response.status_code,
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    event = cast(dict[str, Any], json.loads(line[5:]))
                    kind = event.get("type")
                    if kind == "response.output_item.done":
                        items.append(cast(dict[str, Any], event["item"]))
                    elif kind in ("response.completed", "response.incomplete"):
                        return self._parse(cast(dict[str, Any], event["response"]), items, attempt)
                    elif kind in ("response.failed", "error"):
                        raise self._failure(event)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderError(f"transport error: {exc!r}", retryable=True) from exc
        except json.JSONDecodeError as exc:
            raise ProviderError("invalid event from provider", retryable=True) from exc
        raise ProviderError("stream ended before the response completed", retryable=True)

    @staticmethod
    def _failure(event: dict[str, Any]) -> ProviderError:
        response = cast(dict[str, Any], event.get("response") or {})
        error = cast(dict[str, Any], response.get("error") or event.get("error") or event)
        code = str(error.get("code") or "")
        if code in _PLAN_EXHAUSTED:
            return ProviderError(f"ChatGPT plan allowance used up ({code})", retryable=False)
        return ProviderError(
            f"provider error: {error.get('message') or code or 'failed'}",
            retryable=code in ("server_error", "rate_limit_exceeded"),
        )

    def _parse(
        self, response: dict[str, Any], items: list[dict[str, Any]], attempt: int
    ) -> ModelResponse:
        texts: list[str] = []
        tool_calls: list[ToolCall] = []
        citations: list[Citation] = []
        for item in items or cast(list[dict[str, Any]], response.get("output") or []):
            if item.get("type") == "function_call":
                tool_calls.append(
                    ToolCall(
                        id=str(item.get("call_id", "")),
                        name=str(item.get("name", "")),
                        arguments=str(item.get("arguments") or "{}"),
                    )
                )
            elif item.get("type") == "message":
                for part in cast(list[dict[str, Any]], item.get("content") or []):
                    if part.get("type") == "output_text":
                        texts.append(str(part.get("text", "")))
                        for note in cast(list[dict[str, Any]], part.get("annotations") or []):
                            if note.get("type") == "url_citation" and note.get("url"):
                                citations.append(
                                    Citation(url=str(note["url"]), title=str(note.get("title", "")))
                                )
        text = "".join(texts)
        message: Message = {"role": "assistant", "content": text}
        if tool_calls:
            message["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in tool_calls
            ]
        incomplete = response.get("status") == "incomplete"
        usage = cast(dict[str, Any], response.get("usage") or {})
        details = cast(dict[str, Any], usage.get("input_tokens_details") or {})
        return ModelResponse(
            message=message,
            text=text,
            tool_calls=tool_calls,
            finish_reason="length" if incomplete else ("tool_calls" if tool_calls else "stop"),
            usage=Usage(
                prompt_tokens=int(usage.get("input_tokens") or 0),
                completion_tokens=int(usage.get("output_tokens") or 0),
                cached_tokens=int(details.get("cached_tokens") or 0),
                cache_write_tokens=int(details.get("cache_write_tokens") or 0),
            ),
            model=str(response.get("model") or ""),
            provider=self.name,
            attempts=attempt,
            citations=citations,
        )
