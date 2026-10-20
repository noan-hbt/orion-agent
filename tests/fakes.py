from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

from orion.providers.base import (
    Message,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ToolCall,
    Usage,
)
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

Script = Callable[[ModelRequest], ModelResponse]


def reply(text: str, *, prompt_tokens: int = 100, cached: int = 0, cost: float = 0.0001) -> Script:
    def make(_request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            message={"role": "assistant", "content": text},
            text=text,
            tool_calls=[],
            finish_reason="stop",
            usage=Usage(prompt_tokens=prompt_tokens, cached_tokens=cached, cost_usd=cost),
            model="fake/model",
        )

    return make


def call_tools(
    *calls: tuple[str, dict[str, object]], cost: float = 0.0001, text: str = ""
) -> Script:
    def make(request: ModelRequest) -> ModelResponse:
        offset = len(request.messages)
        tool_calls = [
            ToolCall(id=f"call_{offset}_{i}", name=name, arguments=json.dumps(args))
            for i, (name, args) in enumerate(calls)
        ]
        message: Message = {
            "role": "assistant",
            "content": text,
            "tool_calls": [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in tool_calls
            ],
        }
        return ModelResponse(
            message=message,
            text=text,
            tool_calls=tool_calls,
            finish_reason="tool_calls",
            usage=Usage(prompt_tokens=100, cost_usd=cost),
            model="fake/model",
        )

    return make


def fail(message: str = "boom", retryable: bool = False) -> Script:
    def make(_request: ModelRequest) -> ModelResponse:
        raise ProviderError(message, retryable=retryable)

    return make


class ScriptedProvider:
    """Plays back a script of responses and records every request it receives."""

    name = "scripted"

    def __init__(self, *script: Script) -> None:
        self._script = list(script)
        self.requests: list[ModelRequest] = []

    def extend(self, *script: Script) -> None:
        self._script.extend(script)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        # Snapshot the messages: the loop keeps appending to the same list afterwards.
        self.requests.append(request.model_copy(update={"messages": list(request.messages)}))
        if not self._script:
            raise AssertionError("provider called more times than scripted")
        return self._script.pop(0)(request)


class _EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


async def _echo(args: Any, _ctx: ToolContext) -> ToolResult:
    return ToolResult(args.text)


def echo_tool() -> Tool:
    """A side-effect-free tool that returns its input, for loop and runtime tests."""
    return Tool(
        name="echo",
        description="Return the text.",
        input_model=_EchoInput,
        handler=_echo,
        effect=Effect.NONE,
        reversibility=Reversibility.REVERSIBLE,
        concurrency_safe=True,
        accepts_untrusted_context=True,
    )
