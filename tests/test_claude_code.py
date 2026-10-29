"""The owner's Claude plan through the `claude` command (D100)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from orion.providers.base import ModelRequest, ProviderError, ToolSpec
from orion.providers.claude_code import ClaudeCodeProvider, render

ECHO = ToolSpec(
    name="echo",
    description="Repeat",
    parameters={"type": "object", "properties": {"text": {"type": "string"}}},
)


def fake_claude(tmp_path: Path, answer: dict[str, Any]) -> str:
    """A `claude` that keeps its arguments, stdin and environment, then answers `answer`."""
    script = tmp_path / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"seen = {str(tmp_path / 'seen.json')!r}\n"
        "json.dump({'argv': sys.argv[1:], 'stdin': sys.stdin.read(), 'cwd': os.getcwd(),\n"
        "           'env': sorted(os.environ)}, open(seen, 'w'))\n"
        f"print(json.dumps({answer!r}))\n"
    )
    script.chmod(0o755)
    return str(script)


def seen(tmp_path: Path) -> dict[str, Any]:
    got: dict[str, Any] = json.loads((tmp_path / "seen.json").read_text())
    argv: list[str] = got["argv"]
    got["system"] = Path(argv[argv.index("--system-prompt-file") + 1]).read_text()
    return got


def request(**extra: object) -> ModelRequest:
    return ModelRequest(
        model="sonnet",
        messages=[
            {"role": "system", "content": "Tu es Orion."},
            {"role": "user", "content": "Répète 42"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "echo", "arguments": '{"text": "42"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "42"},
        ],
        tools=[ECHO],
        **extra,  # pyright: ignore[reportArgumentType]
    )


async def test_tool_calls_come_back_and_nothing_of_claude_code_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")  # Orion itself under Claude Code
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-orion")  # another of Orion's keys
    answer: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "structured_output": {
            "text": "",
            "tool_calls": [{"name": "echo", "arguments": {"text": "7"}}],
        },
        "usage": {
            "input_tokens": 3,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 100,
            "output_tokens": 12,
        },
        "modelUsage": {"claude-sonnet-5-5": {}},
    }
    provider = ClaudeCodeProvider(tmp_path / "work", fake_claude(tmp_path, answer))
    response = await provider.complete(request())
    [call] = response.tool_calls
    assert (call.name, json.loads(call.arguments)) == ("echo", {"text": "7"})
    assert response.message["tool_calls"][0]["id"] == call.id
    assert (response.usage.prompt_tokens, response.usage.cached_tokens) == (1003, 900)
    assert response.usage.cost_usd == 0 and response.model == "claude-sonnet-5-5"
    got = seen(tmp_path)
    argv: list[str] = got["argv"]
    for flag in ("--tools", "--strict-mcp-config", "--no-session-persistence", "--json-schema"):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == ""
    system: str = got["system"]
    assert system.startswith("Tu es Orion.") and "## echo" in system
    assert got["cwd"] == str(tmp_path / "work")
    assert "CLAUDECODE" not in got["env"] and "OPENROUTER_API_KEY" not in got["env"]
    assert '<tool_result id="c1">\n42\n</tool_result>' in str(got["stdin"])


def test_the_same_conversation_renders_the_same_bytes() -> None:
    """The prefix Anthropic caches must not move between two calls of one turn."""
    first = render(request().messages)
    longer = [*request().messages, {"role": "assistant", "content": "42."}]
    system, conversation = render(longer)
    assert system == first[0] and conversation.startswith(first[1])


async def test_a_plan_used_up_is_an_error_the_router_falls_back_on(tmp_path: Path) -> None:
    answer: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": "Claude AI usage limit reached",
        "api_error_status": 429,
    }
    provider = ClaudeCodeProvider(tmp_path / "work", fake_claude(tmp_path, answer))
    with pytest.raises(ProviderError, match="usage limit") as caught:
        await provider.complete(request())
    assert caught.value.status == 429


async def test_pictures_are_refused_for_another_provider(tmp_path: Path) -> None:
    provider = ClaudeCodeProvider(tmp_path / "work", fake_claude(tmp_path, {}))
    picture = request()
    picture.messages[1] = {
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}],
    }
    with pytest.raises(ProviderError, match="text only") as caught:
        await provider.complete(picture)
    assert not caught.value.retryable


async def test_a_claude_that_cannot_start_is_a_provider_error(tmp_path: Path) -> None:
    from orion.providers.base import ProviderError

    provider = ClaudeCodeProvider(tmp_path / "work", str(tmp_path / "no-such-claude"))
    with pytest.raises(ProviderError, match="cannot start"):
        await provider.complete(request())


async def test_a_cancelled_turn_leaves_no_claude_running(tmp_path: Path) -> None:
    import asyncio
    import os

    pid_file = tmp_path / "pid"
    script = tmp_path / "claude"
    script.write_text(
        f"#!{sys.executable}\nimport os, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\ntime.sleep(30)\n"
    )
    script.chmod(0o755)
    provider = ClaudeCodeProvider(tmp_path / "work", str(script))
    turn = asyncio.create_task(provider.complete(request()))
    async with asyncio.timeout(5):
        while not pid_file.exists() or not pid_file.read_text():  # noqa: ASYNC110
            await asyncio.sleep(0.02)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def test_the_plans_windows_come_with_the_answer(tmp_path: Path) -> None:
    lines: list[dict[str, Any]] = [
        {"type": "system", "subtype": "init"},
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "unifiedWindows": {
                    "five_hour": {"utilization": 0.17, "resetsAt": 1793295000},
                    "seven_day": {"utilization": 0.68, "resetsAt": 1793530800},
                }
            },
        },
        {"type": "result", "subtype": "success", "is_error": False, "result": "ok", "usage": {}},
    ]
    out = "\n".join(json.dumps(line) for line in lines).encode()
    response = ClaudeCodeProvider(tmp_path).parse(ModelRequest(model="m", messages=[]), out, b"", 0)
    assert response.text == "ok"
    assert [(w.name, w.used) for w in response.quota] == [("five_hour", 0.17), ("seven_day", 0.68)]
