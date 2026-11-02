"""The owner's Claude plan through Claude Code (D100): `claude-code:<model>` model specs.

Orion never holds a Claude credential. It runs the `claude` command as Anthropic publishes it,
signed in by the owner himself (`claude login` on the server, Anthropic's own flow), and asks it
for one answer at a time: no tools of its own (`--tools ""`), no settings, MCP servers or
session kept, Orion's system message in place of Claude Code's. Orion's tools travel as text
in that system message, sorted and the same every time, and the answer comes back as a JSON
object checked against a schema (`--json-schema`): the text, and the tool calls with their
arguments. The conversation goes in on stdin as one user message (`stream-json`) with one text
block per message, rendered the same way every time: Anthropic finds a cached prefix only where
a block ends, so the previous call's last block must still end at the same place (one block for
the whole conversation was a new block on every call, written to the cache and never read).
Text that looks like Orion's own tags is escaped, so a mail or a page cannot pass for the owner.

What it cannot do is refused here, so the router falls back: pictures in or out, web search.
Calls cost nothing per token (`cost_usd` 0): the plan's own limits are the limit, and a plan
used up is an error the router falls back on. The answer comes as a stream of JSON lines
(`stream-json`) for the one that says how much of the plan's windows is used
(`rate_limit_event`), which the usage screen shows.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import structlog

from orion.core.ids import new_id
from orion.providers.base import (
    Message,
    ModelRequest,
    ModelResponse,
    ProviderError,
    QuotaWindow,
    ToolCall,
    ToolSpec,
    Usage,
)

log = structlog.get_logger(__name__)

_SESSION_ENV = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PID")
"""Set when Orion itself runs under Claude Code: the child must not pass for that session."""
_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "USER", "TMPDIR")
_ENV_PREFIXES = ("CLAUDE", "ANTHROPIC")


def child_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """What a `claude` Orion runs sees: what it needs to run and find its login, never Orion's
    other keys (its provider keys, its bot token…), nor the session Orion itself may run in."""
    source = os.environ if environ is None else environ
    return {
        k: v
        for k, v in source.items()
        if (k in _ENV_KEEP or k.startswith(_ENV_PREFIXES)) and k not in _SESSION_ENV
    }


_PROTOCOL = """

# Comment appeler tes outils ici

Les outils listés plus bas ne sont pas des outils que tu peux appeler toi-même : ici, tu n'en
as qu'un, celui qui rend ta réponse. Ne tente jamais d'appeler `remind`, `recall` ou un autre
outil d'Orion directement : il n'existe pas de ton côté et l'appel échouera. Pour t'en servir,
mets-le dans le champ `tool_calls` de ta réponse (`name` et `arguments`, un objet conforme à
son schéma) : Orion l'exécute et le résultat t'arrive dans le message suivant, comme un
<tool_result>. `text` est ce que tu dis au propriétaire : il peut être vide quand tu appelles
des outils, jamais quand tu n'en appelles plus. Une fois les résultats lus, ta réponse sans
outil est le message qu'il recevra. Ne dis jamais qu'un outil ne répond pas tant que tu n'as
pas vu son <tool_result>.

La conversation t'arrive en blocs balisés (<message role=...>, <tool_call>,
<tool_result>) : réponds au dernier message, comme l'assistant.

# Outils d'Orion (à mettre dans tool_calls)

"""


_MODEL = re.compile(r"(claude-[a-z0-9-]+|[a-z]+)(\[[a-z0-9]+\])?")


def is_model_name(model: str) -> bool:
    """A name Claude Code takes: `opus`, `sonnet[1m]`, `claude-opus-5-5`. `opus-5.5` is not one:
    it answers 404 to a short prompt and « Prompt is too long » to a long one, which hides why."""
    return _MODEL.fullmatch(model) is not None


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


_TAG = re.compile(r"<(/?)(message|tool_call|tool_result)\b", re.IGNORECASE)


def _escape(text: str) -> str:
    """Orion's tags inside content are shown, not obeyed: a tool result cannot close itself and
    open a message from the owner."""
    return _TAG.sub(r"&lt;\1\2", text)


def _attr(value: object) -> str:
    return _escape(str(value)).replace('"', "&quot;")


def render(messages: list[Message]) -> tuple[str, list[str]]:
    """(system, one block per message): the same messages give the same bytes, every time."""
    system: list[str] = []
    blocks: list[str] = []
    for message in messages:
        role = message["role"]
        if role == "system":
            system.append(_text(message.get("content")))
            continue
        if role == "tool":
            blocks.append(
                f'<tool_result id="{_attr(message.get("tool_call_id", ""))}">\n'
                f"{_escape(_text(message.get('content')))}\n</tool_result>"
            )
            continue
        body = _escape(_text(message.get("content")))
        for call in cast(list[dict[str, Any]], message.get("tool_calls") or []):
            function = cast(dict[str, Any], call.get("function") or {})
            body += (
                f'\n<tool_call id="{_attr(call.get("id", ""))}" '
                f'name="{_attr(function.get("name", ""))}">'
                f"{_escape(function.get('arguments') or '{}')}</tool_call>"
            )
        blocks.append(f'<message role="{_attr(role)}">\n{body.strip()}\n</message>')
    return "\n\n".join(system), blocks


def stdin_message(blocks: list[str]) -> bytes:
    """The conversation as one user message of Claude Code's `stream-json` input."""
    content = [{"type": "text", "text": block} for block in blocks or ["(vide)"]]
    line = {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }
    return (json.dumps(line, ensure_ascii=False) + "\n").encode()


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


_EFFORT = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}


def _step(block: object) -> str:
    if not isinstance(block, dict):
        return f"text: {block}"[:100]
    part = cast(dict[str, Any], block)
    if part.get("type") == "text":
        return f"text: {part.get('text')}"[:100]
    return str(part.get("name") or f"{part.get('type')}: {part.get('content')}")[:100]


def _trail(event: dict[str, Any]) -> str:
    """One step inside Claude Code, short. A plain answer takes 2 turns for 1 request (the answer
    tool and its result); with 3, a second request was paid, and this says why (D211)."""
    message = cast(dict[str, Any], event.get("message") or {})
    content: object = message.get("content")
    blocks = cast(list[object], content) if isinstance(content, list) else [content]
    return f"{event.get('type')}: {', '.join(_step(b) for b in blocks)}"


class ClaudeCodeProvider:
    name = "claude-code"

    def __init__(
        self,
        workdir: Path,
        binary: str = "claude",
        timeout: float = 300.0,
        default_effort: str | None = "low",
    ) -> None:
        self._binary = binary
        self._workdir = workdir
        """An empty directory: no project, no CLAUDE.md for Claude Code to pick up."""
        self._timeout = timeout
        self._default_effort = default_effort
        """With no effort asked, Claude Code thinks at its own length: a text reply should not."""

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
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        effort = (
            _EFFORT[request.reasoning_effort] if request.reasoning_effort else self._default_effort
        )
        if effort is not None:
            command += ["--effort", effort]
        if request.tools:
            schema = answer_schema(request.tools, allowed=not request.no_tools)
            command += ["--json-schema", json.dumps(schema, sort_keys=True)]
        return command

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not is_model_name(request.model):
            raise ProviderError(
                f"{request.model!r} is not a Claude Code model: use opus, sonnet, haiku "
                "or a full name like claude-opus-5-5",
                retryable=False,
            )
        if request.image_output or request.web_search is not None:
            raise ProviderError("Claude Code cannot do that here", retryable=False)
        system, conversation = render(request.messages)
        if request.tools:
            system += _PROTOCOL + tools_text(request.tools)
        if request.json_output:
            system += "\n\nRéponds par un seul objet JSON, sans rien autour."
        system_file = self._system_file(system)
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command(request, system_file),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._workdir,
                env=child_env(),
            )
        except OSError as exc:  # no binary, no permission: another provider may answer
            raise ProviderError(f"cannot start Claude Code: {exc}", retryable=False) from exc
        try:
            out, err = await asyncio.wait_for(
                process.communicate(stdin_message(conversation)), self._timeout
            )
        except TimeoutError as exc:
            raise ProviderError("Claude Code did not answer in time", retryable=True) from exc
        finally:
            await stop(process)  # timed out, or the turn was cancelled: never left running
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
        result: dict[str, Any] | None = None
        quota: list[QuotaWindow] = []
        trail: list[str] = []
        for line in out.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # a line that is not an event: the result is what matters
            if not isinstance(event, dict):
                continue
            event = cast(dict[str, Any], event)
            if event.get("type") in ("assistant", "user"):
                trail.append(_trail(event))
            if event.get("type") == "result":
                result = event
            elif event.get("type") == "rate_limit_event":
                quota = plan_windows(cast(dict[str, Any], event.get("rate_limit_info") or {}))
        if result is None:
            detail = (err or out).decode(errors="replace").strip()[-300:]
            raise ProviderError(f"Claude Code failed ({code}): {detail}", retryable=False)
        if result.get("is_error") or result.get("subtype") != "success":
            status = result.get("api_error_status")
            text = str(result.get("result") or result.get("subtype") or "error")[:300]
            retryable = isinstance(status, int) and (status == 429 or status >= 500)
            raise ProviderError(
                f"Claude Code: {text}",
                retryable=retryable,
                status=status if isinstance(status, int) else None,
            )
        usage = cast(dict[str, Any], result.get("usage") or {})
        log.info(
            "claude_code.answered",
            turns=result.get("num_turns"),
            trail=trail if int(result.get("num_turns") or 0) > 2 else None,
            denied=[
                d.get("tool_name")
                for d in cast(list[dict[str, Any]], result.get("permission_denials") or [])
            ],
            answer_chars=len(str(result.get("structured_output") or result.get("result") or "")),
            cached=usage.get("cache_read_input_tokens"),
            written=usage.get("cache_creation_input_tokens"),
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
            quota=quota,
        )


def plan_windows(info: dict[str, Any]) -> list[QuotaWindow]:
    """The plan's windows (five hours, seven days...) from a `rate_limit_event`."""
    windows = cast(dict[str, Any], info.get("unifiedWindows") or {})
    found: list[QuotaWindow] = []
    for name, window in sorted(windows.items()):
        if not isinstance(window, dict):
            continue
        window = cast(dict[str, Any], window)
        used = window.get("utilization")
        if not isinstance(used, int | float):
            continue
        resets = window.get("resetsAt")
        found.append(
            QuotaWindow(
                name=name,
                used=float(used),
                resets_at=datetime.fromtimestamp(resets, UTC).isoformat()
                if isinstance(resets, int | float)
                else None,
            )
        )
    return found


async def stop(process: asyncio.subprocess.Process) -> None:
    """A child still running is killed and reaped (nothing if it already exited)."""
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
