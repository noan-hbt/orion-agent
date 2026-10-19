"""Provider-neutral model interface.

Messages use the OpenAI chat format, which OpenRouter and most providers accept. The exact
message dicts sent to a provider are what gets journaled, so a conversation replays byte for
byte: that is what keeps the prompt prefix stable and the provider cache warm.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

Message = dict[str, Any]
"""One chat message in OpenAI format: role, content, and optionally tool_calls / tool_call_id."""


class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: str = Field(description="Raw JSON string as produced by the model")


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )

    @property
    def cache_hit_ratio(self) -> float:
        return self.cached_tokens / self.prompt_tokens if self.prompt_tokens else 0.0


class ModelRequest(BaseModel):
    model: str
    messages: list[Message]
    tools: list[ToolSpec] = Field(default_factory=list[ToolSpec])
    session_id: str | None = Field(
        default=None, description="Stable per conversation, for cache-preserving sticky routing"
    )
    max_tokens: int | None = None
    temperature: float | None = None
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None


class ModelResponse(BaseModel):
    message: Message = Field(description="Assistant message exactly as it will be journaled")
    text: str
    tool_calls: list[ToolCall]
    finish_reason: str | None
    usage: Usage
    model: str
    provider: str | None = None
    attempts: int = 1


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class ModelProvider(Protocol):
    name: str

    async def complete(self, request: ModelRequest) -> ModelResponse: ...
