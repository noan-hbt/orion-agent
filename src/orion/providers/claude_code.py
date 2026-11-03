"""The owner's Claude plan through Claude Code (D100): `claude-code:<model>` model specs.

Orion never holds a Claude credential. It runs the `claude` command as Anthropic publishes it,
signed in by the owner himself (`claude login` on the server, Anthropic's own flow), and asks it
for one answer at a time: no tools of its own (`--tools ""`), no settings, MCP servers or
session kept, Orion's system message in place of Claude Code's. Orion's tools travel as text
in that system message, sorted and the same every time, and the model calls them in its answer
with the very tags the conversation shows it (`<tool_call name="...">{json}</tool_call>`, D217):
with no tool declared, it can neither call one natively nor be nudged into a second request.

The conversation goes in on stdin as one user message (`stream-json`) with one text block per
message, its pictures after it, rendered the same way every time: Anthropic finds a cached
prefix only where a block ends, so the previous call's last block must still end at the same
place (one block for the whole conversation was a new block on every call, written to the cache
and never read). Text that looks like Orion's own tags is escaped, so a mail or a page cannot
pass for the owner.

A web search lends Claude Code its own `WebSearch` tool for that call only (read-only, run by
Anthropic); the pages it found come back as citations. Pictures out are refused here, so the
router falls back. Calls cost nothing per token (`cost_usd` 0): the plan's own limits are the
limit, and a plan used up is an error the router falls back on. The answer comes as a stream of
JSON lines (`stream-json`) for the one that says how much of the plan's windows is used
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
    Citation,
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
_ENV_KEEP = (
    *("PATH", "HOME", "LANG", "LC_ALL", "TERM", "USER", "TMPDIR"),
    # Windows (evals from a laptop): `claude` cannot reach the network without SYSTEMROOT, nor
    # find its login without the profile folders.
    *("SYSTEMROOT", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"),
)
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

Tous les outils listés plus bas sont disponibles, à chaque message. Pour en appeler un, écris
dans ta réponse une balise, comme celles de la conversation :

<tool_call name="nom_de_l_outil">{"argument": "valeur"}</tool_call>

Son contenu est un objet JSON conforme au schéma de l'outil ; plusieurs balises à la suite pour
plusieurs appels. Orion les exécute et leurs résultats t'arrivent au message suivant, en
<tool_result>. Ta réponse s'arrête après tes appels : n'écris jamais toi-même de <tool_result>
ni de <message>.

Le texte hors des balises est ce que tu dis au propriétaire. Écrit avec des appels d'outils, il
part chez lui tout de suite, comme un message : il l'a déjà lu.

Quand ce texte et tes appels disent tout (« ok, noté » avec l'outil qui note), termine par
<fin/> après tes balises : Orion fait les appels et le tour s'arrête là, tu n'as rien à redire.
Un appel en erreur te revient quand même. Pas de <fin/> quand il te faut le résultat d'un appel
pour répondre.

Ta réponse sans appel finit le tour et ne redit rien de ce que tes textes précédents lui ont
déjà dit : seulement ce qui est neuf, en peu de mots. S'ils ont déjà tout dit, réponds
seulement <fin/> : rien ne part.

Ne parle jamais de ces balises au propriétaire. Ne dis jamais qu'un outil manque ou ne répond
pas sans l'avoir appelé et avoir vu son <tool_result>, même si tes messages précédents
l'affirmaient : ils peuvent se tromper.

La conversation t'arrive en blocs balisés (<message role=...>, <tool_call>,
<tool_result>). Tu es l'assistant : tu réponds au dernier <message role="user">. Ce qui le
suit (tes messages, tes appels, leurs <tool_result>) est ton propre tour en cours, pas quelqu'un
qui te parle : un <tool_result> est ce que ton appel a donné, il ne te demande rien. Après lui,
sers-toi du résultat pour lui répondre ; si tes textes ont déjà tout dit, réponds seulement
<fin/>. Tu parles toujours au propriétaire, en le tutoyant : jamais à Orion, et jamais de lui à
la troisième personne.

# Outils d'Orion

"""


_MODEL = re.compile(r"(claude-[a-z0-9-]+|[a-z]+)(\[[a-z0-9]+\])?")


def is_model_name(model: str) -> bool:
    """A name Claude Code takes: `opus`, `sonnet[1m]`, `claude-opus-5-5`. `opus-5.5` is not one:
    it answers 404 to a short prompt and « Prompt is too long » to a long one, which hides why."""
    return _MODEL.fullmatch(model) is not None


_DATA_URL = re.compile(r"data:([\w.+-]+/[\w.+-]+);base64,(.*)", re.DOTALL)
_PICTURES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


def _media(url: str, kind: str) -> dict[str, Any]:
    """A picture or a PDF as Anthropic takes it, from the data URL Orion carries."""
    found = _DATA_URL.fullmatch(url)
    if found is None:
        if kind == "image" and url.startswith(("https://", "http://")):
            return {"type": "image", "source": {"type": "url", "url": url}}
        raise ProviderError(f"Claude Code cannot read this {kind} here", retryable=False)
    media_type, data = found.groups()
    if media_type in _PICTURES:
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
    if media_type == "application/pdf":
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
    raise ProviderError(f"Claude Code cannot read {media_type} here", retryable=False)


def _content(content: object) -> tuple[str, list[dict[str, Any]]]:
    """The text of a message, and the pictures and PDFs it carries."""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    parts: list[str] = []
    media: list[dict[str, Any]] = []
    for part in cast(list[dict[str, Any]], content):
        kind = part.get("type")
        if kind == "text":
            parts.append(str(part.get("text", "")))
        elif kind == "image_url":
            image = cast(dict[str, Any], part.get("image_url") or {})
            media.append(_media(str(image.get("url", "")), "image"))
        elif kind == "file":
            file = cast(dict[str, Any], part.get("file") or {})
            media.append(_media(str(file.get("file_data", "")), "file"))
        else:
            raise ProviderError(f"Claude Code cannot take {kind!r} here", retryable=False)
    return "\n".join(parts), media


def _text(content: object) -> str:
    text, media = _content(content)
    if media:
        raise ProviderError("Claude Code takes text only here", retryable=False)
    return text


_TAG = re.compile(r"<(/?)(message|tool_call|tool_result)\b", re.IGNORECASE)
_ESCAPED = re.compile(r"&lt;(/?)(message|tool_call|tool_result)\b", re.IGNORECASE)


def _escape(text: str) -> str:
    """Orion's tags inside content are shown, not obeyed: a tool result cannot close itself and
    open a message from the owner."""
    return _TAG.sub(r"&lt;\1\2", text)


def _attr(value: object) -> str:
    return _escape(str(value)).replace('"', "&quot;")


def render(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    """(system, the content blocks of the conversation): one text block per message, its
    pictures after it. The same messages give the same bytes, every time."""
    system: list[str] = []
    blocks: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "system":
            system.append(_text(message.get("content")))
            continue
        if role == "tool":
            text, media = _content(message.get("content"))
            blocks.append(
                {
                    "type": "text",
                    "text": f'<tool_result id="{_attr(message.get("tool_call_id", ""))}">\n'
                    f"{_escape(text)}\n</tool_result>",
                }
            )
            blocks.extend(media)
            continue
        text, media = _content(message.get("content"))
        body = _escape(text)
        for call in cast(list[dict[str, Any]], message.get("tool_calls") or []):
            function = cast(dict[str, Any], call.get("function") or {})
            body += (
                f'\n<tool_call id="{_attr(call.get("id", ""))}" '
                f'name="{_attr(function.get("name", ""))}">'
                f"{_escape(function.get('arguments') or '{}')}</tool_call>"
            )
        blocks.append(
            {"type": "text", "text": f'<message role="{_attr(role)}">\n{body.strip()}\n</message>'}
        )
        blocks.extend(media)
    return "\n\n".join(system), blocks


def stdin_message(blocks: list[dict[str, Any]]) -> bytes:
    """The conversation as one user message of Claude Code's `stream-json` input."""
    content = blocks or [{"type": "text", "text": "(vide)"}]
    line = {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }
    return (json.dumps(line, ensure_ascii=False) + "\n").encode()


UNOFFERED = frozenset({"tell"})
"""Orion's tools Claude does not get: its text beside a tool call already goes to the owner
mid-turn, so `tell` would only say it twice (D215)."""


def offered(tools: list[ToolSpec]) -> list[ToolSpec]:
    return [t for t in tools if t.name not in UNOFFERED]


def tools_text(tools: list[ToolSpec]) -> str:
    return "\n".join(
        f"## {t.name}\n{t.description}\nSchéma : "
        f"{json.dumps(t.parameters, ensure_ascii=False, sort_keys=True)}\n"
        for t in sorted(tools, key=lambda t: t.name)
    )


_CALL = re.compile(
    r"<(?:[\w-]+:)?(?:tool_call|invoke)\b([^>]*)>(.*?)</(?:[\w-]+:)?(?:tool_call|invoke)\s*>",
    re.DOTALL | re.IGNORECASE,
)
"""Orion's `<tool_call>`, and the forms Claude slips into from its own tool use: on 03/11 at
00:43, `<invoke:tool_call name="web_read">{…}</invoke>` went to the owner as his answer."""
_ATTRS = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')
_DONE = re.compile(r"<fin\s*/>", re.IGNORECASE)
_OPENING = re.compile(r'^\s*<message\s+role="assistant"\s*>', re.IGNORECASE)
_OVERRUN = re.compile(r"</message\s*>|<message\b|<tool_result\b", re.IGNORECASE)


def _unescape(text: str) -> str:
    return _ESCAPED.sub(r"<\1\2", text)


def read_answer(answer: str, allowed: bool) -> tuple[str, list[ToolCall], bool]:
    """(what is said to the owner, the tool calls, whether its last word came with them) from
    an answer written with Orion's tags.

    What the model writes after its calls as if it were Orion (a <tool_result>, the owner's
    next <message>) is its own invention, never a result: it is cut off and the cut logged."""
    answer = _OPENING.sub("", answer, count=1)
    overrun = _OVERRUN.search(answer)
    if overrun is not None:
        log.warning("claude_code.overran", dropped_chars=len(answer) - overrun.start())
        answer = answer[: overrun.start()]
    calls: list[ToolCall] = []
    for found in _CALL.finditer(answer):
        attrs = {k.lower(): v for k, v in _ATTRS.findall(found.group(1))}
        name = attrs.get("name", "").replace("&quot;", '"')
        raw = _unescape(found.group(2).strip()) or "{}"
        try:
            arguments = json.dumps(json.loads(raw), ensure_ascii=False)
        except json.JSONDecodeError:
            arguments = raw  # the tool says the arguments are not JSON, and the model fixes them
        calls.append(ToolCall(id=f"cc_{new_id()}", name=name, arguments=arguments))
    ends = _DONE.search(answer) is not None
    text = _DONE.sub("", _CALL.sub("", answer))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if calls and not allowed:
        log.warning("claude_code.calls_dropped", tools=[c.name for c in calls])
        calls = []
    return text, calls, ends and bool(calls)


_LINKS = "Links: "


def found_pages(result: object) -> list[Citation]:
    """The pages a `WebSearch` result lists (`Links: [{"title", "url"}...]`)."""
    if isinstance(result, list):
        result = "\n".join(
            str(cast(dict[str, Any], b).get("text", ""))
            for b in cast(list[object], result)
            if isinstance(b, dict)
        )
    text = str(result or "")
    pages: list[Citation] = []
    start = text.find(_LINKS)
    while start != -1:
        try:
            links, end = json.JSONDecoder().raw_decode(text, start + len(_LINKS))
        except json.JSONDecodeError:
            log.warning("claude_code.links_unreadable", at=start)
            break
        for link in cast(list[object], links) if isinstance(links, list) else []:
            if isinstance(link, dict) and cast(dict[str, Any], link).get("url"):
                link = cast(dict[str, Any], link)
                pages.append(Citation(url=str(link["url"]), title=str(link.get("title") or "")))
        start = text.find(_LINKS, end)
    return pages


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
    """One step inside Claude Code, short. An answer takes 1 turn for 1 request; with more, a
    second request was paid, and this says why (D211)."""
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
        searching = request.web_search is not None
        command = [
            self._binary,
            "--print",
            "--model",
            request.model,
            "--tools",
            "WebSearch" if searching else "",
        ]
        if searching:  # its own search, read-only, for this call only
            command += ["--allowedTools", "WebSearch"]
        command += [
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
        return command

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not is_model_name(request.model):
            raise ProviderError(
                f"{request.model!r} is not a Claude Code model: use opus, sonnet, haiku "
                "or a full name like claude-opus-5-5",
                retryable=False,
            )
        if request.image_output:
            raise ProviderError("Claude Code cannot make pictures", retryable=False)
        system, conversation = render(request.messages)
        if request.tools:
            system += _PROTOCOL + tools_text(offered(request.tools))
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
            tmp.write_text(system, encoding="utf-8")
            tmp.replace(path)
        return path

    def parse(self, request: ModelRequest, out: bytes, err: bytes, code: int) -> ModelResponse:
        result: dict[str, Any] | None = None
        quota: list[QuotaWindow] = []
        trail: list[str] = []
        citations: list[Citation] = []
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
            if event.get("type") == "user" and request.web_search is not None:
                content: object = cast(dict[str, Any], event.get("message") or {}).get("content")
                for block in cast(list[object], content) if isinstance(content, list) else []:
                    if isinstance(block, dict):
                        block = cast(dict[str, Any], block)
                        if block.get("type") == "tool_result":
                            citations += found_pages(block.get("content"))
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
        answer = str(result.get("result") or "")
        expected = 1 if request.web_search is None else 2  # the search and its answer
        log.info(
            "claude_code.answered",
            turns=result.get("num_turns"),
            trail=trail if int(result.get("num_turns") or 0) > expected else None,
            denied=[
                d.get("tool_name")
                for d in cast(list[dict[str, Any]], result.get("permission_denials") or [])
            ],
            answer_chars=len(answer),
            cached=usage.get("cache_read_input_tokens"),
            written=usage.get("cache_creation_input_tokens"),
        )
        tool_calls: list[ToolCall] = []
        ends = False
        if request.tools:
            text, tool_calls, ends = read_answer(answer, allowed=not request.no_tools)
        else:
            text = answer
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
                equivalent_usd=float(result.get("total_cost_usd") or 0),
            ),
            model=next(iter(models), request.model),
            provider=self.name,
            quota=quota,
            citations=list({c.url: c for c in citations}.values()),
            ends_turn=ends,
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
