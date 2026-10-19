"""OpenRouter chat-completions provider.

Cache strategy: DeepSeek and most open models cache prompt prefixes automatically, but each
upstream host has its own cache. Sending a stable `session_id` per conversation makes OpenRouter
route follow-up requests to the same host, so the prefix written by one turn is read by the next.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, cast

import httpx
import structlog
from pydantic import BaseModel, Field, ValidationError

from orion.providers.base import (
    Message,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ToolCall,
    Usage,
)

log = structlog.get_logger(__name__)

_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


class OpenRouterProvider:
    name = "openrouter"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        *,
        timeout: float = 120.0,
        max_attempts: int = 4,
        max_backoff: float = 20.0,
        app_name: str = "Orion",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Title": app_name,
            },
        )
        self._max_attempts = max_attempts
        self._max_backoff = max_backoff

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        body = self._body(request)
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._parse(await self._post(body), attempts=attempt)
            except ProviderError as exc:
                if not exc.retryable or attempt >= self._max_attempts:
                    raise
                delay = self._backoff(attempt, exc)
                log.warning(
                    "provider.retry",
                    provider=self.name,
                    attempt=attempt,
                    status=exc.status,
                    delay=round(delay, 2),
                    error=str(exc),
                )
                await asyncio.sleep(delay)

    @staticmethod
    def _body(request: ModelRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
            "usage": {"include": True},
        }
        if request.tools:
            body["tools"] = [t.to_openai() for t in request.tools]
        if request.session_id:
            body["session_id"] = request.session_id
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.json_output:
            body["response_format"] = {"type": "json_object"}
        if request.reasoning_effort is not None:
            body["reasoning"] = {"effort": request.reasoning_effort, "exclude": True}
        return body

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post("/chat/completions", json=body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # The request may still have been processed and billed upstream; it is logged as a
            # retry so the cost is at least visible.
            raise ProviderError(f"transport error: {exc!r}", retryable=True) from exc
        if response.status_code >= 400:
            raise ProviderError(
                f"HTTP {response.status_code}: {response.text[:500]}",
                retryable=response.status_code in _RETRYABLE_STATUS,
                status=response.status_code,
            )
        try:
            data: dict[str, Any] = response.json()
        except ValueError as exc:
            raise ProviderError("invalid JSON from provider", retryable=True) from exc
        if "error" in data:
            error: object = data["error"]
            code = cast(dict[str, object], error).get("code") if isinstance(error, dict) else None
            status = code if isinstance(code, int) else None
            raise ProviderError(
                f"provider error: {error}",
                retryable=status in _RETRYABLE_STATUS if status else False,
                status=status,
            )
        return data

    def _backoff(self, attempt: int, exc: ProviderError) -> float:
        base = min(self._max_backoff, 0.5 * 2 ** (attempt - 1))
        return min(self._max_backoff, base * (0.5 + random.random()))

    @staticmethod
    def _parse(data: dict[str, Any], *, attempts: int) -> ModelResponse:
        try:
            parsed = _ChatResponse.model_validate(data)
        except ValidationError as exc:
            raise ProviderError(f"unexpected response shape: {exc}", retryable=True) from exc
        if not parsed.choices:
            raise ProviderError(f"no choices in response: {str(data)[:300]}", retryable=True)
        choice = parsed.choices[0]
        text = choice.message.content or ""
        tool_calls = [
            ToolCall(id=c.id, name=c.function.name, arguments=c.function.arguments or "{}")
            for c in choice.message.tool_calls or []
        ]

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

        raw_usage = parsed.usage or _RawUsage()
        details = raw_usage.prompt_tokens_details or _RawPromptDetails()
        return ModelResponse(
            message=message,
            text=text,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            usage=Usage(
                prompt_tokens=raw_usage.prompt_tokens,
                completion_tokens=raw_usage.completion_tokens,
                cached_tokens=details.cached_tokens or 0,
                cache_write_tokens=details.cache_write_tokens or 0,
                cost_usd=raw_usage.cost or 0.0,
            ),
            model=parsed.model,
            provider=parsed.provider,
            attempts=attempts,
        )


# --- response schema (only the fields Orion uses; everything else is ignored) ---


class _RawFunction(BaseModel):
    name: str
    arguments: str | None = None


class _RawToolCall(BaseModel):
    id: str
    function: _RawFunction


class _RawMessage(BaseModel):
    content: str | None = None
    tool_calls: list[_RawToolCall] | None = None


class _RawChoice(BaseModel):
    message: _RawMessage
    finish_reason: str | None = None


class _RawPromptDetails(BaseModel):
    cached_tokens: int | None = 0
    cache_write_tokens: int | None = 0


class _RawUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = 0.0
    prompt_tokens_details: _RawPromptDetails | None = None


class _ChatResponse(BaseModel):
    model: str = ""
    provider: str | None = None
    choices: list[_RawChoice] = Field(default_factory=list[_RawChoice])
    usage: _RawUsage | None = None
