"""The owner's Claude plan through Claude Code (D100): `claude-code:<model>` model specs.

Orion never holds a Claude credential. It runs the `claude` command as Anthropic publishes it,
signed in by the owner himself (`claude login` on the server, Anthropic's own flow), and asks it
for one answer at a time: no tools of its own (`--tools ""`), no settings, MCP servers or
session kept, Orion's system message in place of Claude Code's. Orion's tools travel as text
in that system message, sorted and the same every time, and the answer comes back as a JSON
object checked against a schema (`--json-schema`): the text, and the tool calls with their
arguments. The conversation goes in on stdin, rendered the same way every time, so the prefix
Anthropic caches stays the same from one call to the next.

What it cannot do is refused here, so the router falls back: pictures in or out, web search.
Calls cost nothing per token (`cost_usd` 0): the plan's own limits are the limit, and a plan
used up is an error the router falls back on.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, cast

import structlog

from orion.core.ids import new_id
from orion.providers.base import (
    Message,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ToolCall,
    ToolSpec,
    Usage,
)

log = structlog.get_logger(__name__)

_SESSION_ENV = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PID")
"""Set when Orion itself runs under Claude Code: the child must not pass for that session."""

_PROTOCOL = """

# Comment appeler tes outils ici

Les outils listés plus bas ne sont pas des outils que tu peux appeler toi-même : ici, tu n'en
as qu'un, celui qui rend ta réponse. Ne tente jamais d'appeler `remind`, `recall` ou un autre
outil d'Orion directement : il n'existe pas de ton côté et l'appel échouera. Pour t'en servir,
mets-le dans le champ `tool_calls` de ta réponse (`name` et `arguments`, un objet conforme à
son schéma) : Orion l'exécute et le résultat t'arrive dans le message suivant, comme un
<tool_result>. `text` est ce que tu dis au propriétaire, vide si tu ne fais qu'appeler des
outils. Ne dis jamais qu'un outil ne répond pas tant que tu n'as pas vu son <tool_result>.

La conversation t'arrive en un seul bloc balisé (<message role=...>, <tool_call>,
<tool_result>) : réponds au dernier message, comme l'assistant.

# Outils d'Orion (à mettre dans tool_calls)

"""


def _text(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in cast(list[dict[str, Any]], content):
        if part.get("type") == "text":
            parts.append(str(part.get("text", "")))
        else:
            raise ProviderError(
                f"Claude Code takes text only here, not {part.get('type')!r}", retryable=False
            )
    return "\n".join(parts)


def render(messages: list[Message]) -> tuple[str, str]:
    """(system, conversation): the same messages give the same bytes, every time."""
    system: list[str] = []
    lines: list[str] = []
    for message in messages:
        role = message["role"]
        if role == "system":
            system.append(_text(message.get("content")))
            continue
        if role == "tool":
            lines.append(
                f'<tool_result id="{message.get("tool_call_id", "")}">\n'
                f"{_text(message.get('content'))}\n</tool_result>"
            )
            continue
        body = _text(message.get("content"))
        for call in cast(list[dict[str, Any]], message.get("tool_calls") or []):
            function = cast(dict[str, Any], call.get("function") or {})
            body += (
                f'\n<tool_call id="{call.get("id", "")}" name="{function.get("name", "")}">'
                f"{function.get('arguments') or '{}'}</tool_call>"
            )
        lines.append(f'<message role="{role}">\n{body.strip()}\n</message>')
    return "\n\n".join(system), "\n\n".join(lines)


def tools_text(tools: list[ToolSpec]) -> str:
    return "\n".join(
        f"## {t.name}\n{t.description}\nSchéma : "
        f"{json.dumps(t.parameters, ensure_ascii=False, sort_keys=True)}\n"
        for t in sorted(tools, key=lambda t: t.name)
    )


def answer_schema(tools: list[ToolSpec], allowed: bool) -> dict[str, Any]:
    call: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "enum": sorted(t.name for t in tools)},
            "arguments": {"type": "object"},
        },
        "required": ["name", "arguments"],
    }
    calls: dict[str, Any] = {"type": "array", "items": call}
    if not allowed:
        calls["maxItems"] = 0
    return {
        "type": "object",
        "properties": {"text": {"type": "string"}, "tool_calls": calls},
        "required": ["text", "tool_calls"],
    }


class ClaudeCodeProvider:
    name = "claude-code"

    def __init__(self, workdir: Path, binary: str = "claude", timeout: float = 300.0) -> None:
        self._binary = binary
        self._workdir = workdir
        """An empty directory: no project, no CLAUDE.md for Claude Code to pick up."""
        self._timeout = timeout

    @staticmethod
    def available(binary: str = "claude") -> bool:
        return shutil.which(binary) is not None

    def command(self, request: ModelRequest, system_file: Path) -> list[str]:
        command = [
            self._binary,
            "--print",
            "--model",
            request.model,
            "--tools",
            "",
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--no-session-persistence",
            "--system-prompt-file",
            str(system_file),
            "--output-format",
            "json",
        ]
        if request.tools:
            schema = answer_schema(request.tools, allowed=not request.no_tools)
            command += ["--json-schema", json.dumps(schema, sort_keys=True)]
        return command

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.image_output or request.web_search is not None:
            raise ProviderError("Claude Code cannot do that here", retryable=False)
        system, conversation = render(request.messages)
        if request.tools:
            system += _PROTOCOL + tools_text(request.tools)
        if request.json_output:
            system += "\n\nRéponds par un seul objet JSON, sans rien autour."
        system_file = self._system_file(system)
        process = await asyncio.create_subprocess_exec(
            *self.command(request, system_file),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._workdir,
            env={k: v for k, v in os.environ.items() if k not in _SESSION_ENV},
        )
        try:
            out, err = await asyncio.wait_for(
                process.communicate(conversation.encode()), self._timeout
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ProviderError("Claude Code did not answer in time", retryable=True) from exc
        return self.parse(request, out, err, process.returncode or 0)

    def _system_file(self, system: str) -> Path:
        """Named after its content: the same system message is written once."""
        self._workdir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(system.encode()).hexdigest()[:16]
        path = self._workdir / f"system-{digest}.md"
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            tmp.write_text(system)
            tmp.replace(path)
        return path

    def parse(self, request: ModelRequest, out: bytes, err: bytes, code: int) -> ModelResponse:
        try:
            result = cast(dict[str, Any], json.loads(out))
        except json.JSONDecodeError as exc:
            detail = (err or out).decode(errors="replace").strip()[-300:]
            raise ProviderError(f"Claude Code failed ({code}): {detail}", retryable=False) from exc
        if result.get("is_error") or result.get("subtype") != "success":
            status = result.get("api_error_status")
            text = str(result.get("result") or result.get("subtype") or "error")[:300]
            retryable = isinstance(status, int) and (status == 429 or status >= 500)
            raise ProviderError(
                f"Claude Code: {text}",
                retryable=retryable,
                status=status if isinstance(status, int) else None,
            )
        log.info(
            "claude_code.answered",
            turns=result.get("num_turns"),
            denied=[
                d.get("tool_name")
                for d in cast(list[dict[str, Any]], result.get("permission_denials") or [])
            ],
            answer=str(result.get("structured_output") or result.get("result"))[:500],
        )
        tool_calls: list[ToolCall] = []
        if request.tools:
            answer = result.get("structured_output")
            if not isinstance(answer, dict):
                raise ProviderError("Claude Code gave no structured answer", retryable=True)
            answer = cast(dict[str, Any], answer)
            text = str(answer.get("text") or "")
            for call in cast(list[dict[str, Any]], answer.get("tool_calls") or []):
                tool_calls.append(
                    ToolCall(
                        id=f"cc_{new_id()}",
                        name=str(call.get("name", "")),
                        arguments=json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                    )
                )
        else:
            text = str(result.get("result") or "")
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
        usage = cast(dict[str, Any], result.get("usage") or {})
        cached = int(usage.get("cache_read_input_tokens") or 0)
        written = int(usage.get("cache_creation_input_tokens") or 0)
        models = cast(dict[str, Any], result.get("modelUsage") or {})
        return ModelResponse(
            message=message,
            text=text,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
            usage=Usage(
                prompt_tokens=int(usage.get("input_tokens") or 0) + cached + written,
                completion_tokens=int(usage.get("output_tokens") or 0),
                cached_tokens=cached,
                cache_write_tokens=written,
            ),
            model=next(iter(models), request.model),
            provider=self.name,
        )
