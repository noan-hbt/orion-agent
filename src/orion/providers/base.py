"""Provider-neutral model interface.

Messages use the OpenAI chat format, which OpenRouter and most providers accept. The exact
message dicts sent to a provider are what gets journaled, so a conversation replays byte for
byte: that is what keeps the prompt prefix stable and the provider cache warm.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

Message = dict[str, Any]
"""One chat message in OpenAI format: role, content, and optionally tool_calls / tool_call_id."""

CACHE_MINUTES = (
    ("claude-code:", 5),
    ("anthropic/", 5),
    ("deepseek/", 240),
)
"""How long a model's prompt cache outlives the last request, by spec prefix (after an
`openrouter:` provider). Anthropic keeps it 5 minutes; DeepSeek keeps it on disk for hours."""
CACHE_MINUTES_ELSE = 60
"""Others (OpenAI and the ChatGPT plan keep theirs up to an hour): the longest they may."""


def cache_minutes(spec: str) -> int:
    """Until when changing the start of a conversation would cost its cache."""
    model = spec.removeprefix("openrouter:")
    return next((m for prefix, m in CACHE_MINUTES if model.startswith(prefix)), CACHE_MINUTES_ELSE)


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
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    json_output: bool = Field(default=False, description="Ask for a single JSON object reply")
    no_tools: bool = Field(
        default=False,
        description="Tools stay declared (the cached prefix is unchanged) but none may be called",
    )
    image_output: bool = Field(
        default=False, description="Ask an image model for pictures (`ModelResponse.images`)"
    )
    web_search: int | None = Field(
        default=None,
        ge=1,
        le=10,
        description="Let the provider search the web first, with this many results (the web "
        "search only: what comes back is web content)",
    )


class Citation(BaseModel):
    """A page the provider's web search read to answer."""

    model_config = ConfigDict(frozen=True)

    url: str
    title: str = ""


class ModelResponse(BaseModel):
    message: Message = Field(description="Assistant message exactly as it will be journaled")
    text: str
    tool_calls: list[ToolCall]
    finish_reason: str | None
    usage: Usage
    model: str
    provider: str | None = None
    attempts: int = 1
    citations: list[Citation] = Field(default_factory=list[Citation])
    images: list[str] = Field(
        default_factory=list[str], description="Pictures an image model made, as data: URLs"
    )


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class ModelProvider(Protocol):
    name: str

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


@runtime_checkable
class Embedder(Protocol):
    """A provider that also turns texts into vectors (semantic memory search)."""

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class Speaker(Protocol):
    """A provider that also says a text aloud, as raw 16-bit mono PCM at 24 kHz."""

    async def speak(self, model: str, text: str, voice: str) -> bytes: ...
