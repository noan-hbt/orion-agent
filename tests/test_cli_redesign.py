"""Regression contracts for the CLI transcript redesign.

These tests intentionally exercise the public-ish adapter/UI boundary rather
than Rich internals, so they remain useful with the stdlib fallback renderer.
"""

from __future__ import annotations

import io

from channels import AgentOutput
from channel_adapters import CLIAdapter
from cli_ui import CLIConsole


def _console(**kwargs) -> CLIConsole:
    return CLIConsole(
        output=io.StringIO(),
        input_stream=io.StringIO(),
        show_banner=False,
        history_path=None,
        **kwargs,
    )


def test_usage_summary_can_omit_model_for_model_already_in_header() -> None:
    console = _console(
        model="z-ai/glm-5.3-flash",
        usage_provider={
            "model": "z-ai/glm-5.3-flash",
            "started_calls": 1,
            "known_cost_usd": 0.0024,
            "total_tokens": 21060,
        },
    )

    compact = console.usage_summary(include_model=False)

    assert "z-ai/glm-5.3-flash" not in compact
    assert "$0.0024" in compact


def test_assistant_accepts_explicit_request_id_and_keeps_progress_separate() -> None:
    console = _console()
    request_id = console.requests.create("continue voxel")

    # The explicit correlation must work even if another request becomes
    # active later; intermediate output must not be appended to final prose.
    console.assistant("agent still working", intermediate=True, request_id=request_id)
    console.assistant("voxel work complete", request_id=request_id)

    assert not console._stream_open
    rendered = console.output.getvalue()
    assert "agent still working" in rendered
    assert "voxel work complete" in rendered


def test_render_event_deduplicates_seq_zero() -> None:
    console = _console()
    event = {
        "kind": "job.update",
        "request_id": "req-1",
        "seq": 0,
        "meta": {"summary": "job started"},
    }

    console.render_event(event)
    console.render_event(event)

    assert console.output.getvalue().count("job started") == 1


def test_cli_adapter_forwards_falsy_intermediate_flag_and_request_id() -> None:
    adapter = CLIAdapter(output=io.StringIO(), banner=False, history_path=None)
    calls: list[dict[str, object]] = []

    def assistant(content: str, **kwargs: object) -> None:
        calls.append({"content": content, **kwargs})

    adapter.console.assistant = assistant  # type: ignore[method-assign]
    adapter.send(AgentOutput("done", event_id="req-42", metadata={"intermediate": 0}))

    assert calls == [{"content": "done", "intermediate": False, "timestamp": None, "request_id": "req-42"}]


def test_reported_error_is_not_rendered_as_normal_assistant_reply() -> None:
    adapter = CLIAdapter(output=io.StringIO(), banner=False, history_path=None)
    calls: list[str] = []

    adapter.console.assistant = lambda content, **kwargs: calls.append(str(content))  # type: ignore[method-assign]

    class Event:
        id = "req-error"
        type = "message"

    adapter.report_error(Event(), RuntimeError("provider failed"))
    adapter.send(AgentOutput("provider failed", event_id="req-error", metadata={"error": True}))

    assert calls == []
