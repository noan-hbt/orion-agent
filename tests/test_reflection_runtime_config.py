from __future__ import annotations

import json
from typing import get_type_hints

import pytest

from event_handler import Event
from orion_config import OrionConfig
from runtime import AgentRuntime, RunContext


def _context() -> RunContext:
    return RunContext(
        event=Event("message", {"text": "inspect"}, source="test"),
        task=None,
        run_id="reflection-test",
        loaded_state={},
    )


@pytest.mark.parametrize("reflection_format", ["legacy_text", "advisory_json"])
def test_build_passes_context_reflection_format_to_engine(
    tmp_path, monkeypatch, reflection_format
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    (tmp_path / "ORION_CORE.md").write_text("core", encoding="utf-8")
    (tmp_path / "REFLECTION_CORE.md").write_text("reflection", encoding="utf-8")
    (tmp_path / "tools").mkdir()

    config = OrionConfig.from_mapping(
        {
            "context": {"reflection_format": reflection_format},
            "subagents": {"enabled": False},
            "scheduler": {"enabled": False},
            "memory": {"enabled": False},
            "tools": {"directory": "tools"},
        }
    )
    config.config_path = tmp_path / "orion.toml"

    app = config.build()
    try:
        assert app.runtime.reflection_engine is not None
        assert app.runtime.reflection_engine.reflection_format == reflection_format
    finally:
        app.stop()


def test_legacy_runtime_serializes_structured_reflection_deterministically():
    runtime = AgentRuntime(context_mode="legacy", action_ledger_path=":memory:")
    reflection = {
        "schema": "orion.reflection.v1",
        "summary": "inspect",
        "facts": [{"text": "known", "source": "event"}],
        "uncertainties": [],
        "risks": [],
        "checks": [],
        "confidence": 0.8,
    }

    messages = runtime._initial_run_messages(_context(), reflection=reflection)
    rendered = json.dumps(
        reflection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    assert any(
        message.get("role") == "system" and rendered in message.get("content", "")
        for message in messages
    )


def test_contract_runtime_preserves_structured_advisory_reflection():
    runtime = AgentRuntime(context_mode="contract", action_ledger_path=":memory:")
    reflection = {
        "schema": "orion.reflection.v1",
        "summary": "inspect",
        "facts": [{"text": "known", "source": "event"}],
        "uncertainties": ["unknown"],
        "risks": [],
        "checks": ["verify"],
        "confidence": 0.8,
    }

    messages = runtime._initial_run_messages(_context(), reflection=reflection)
    evidence = next(
        message["content"]
        for message in messages
        if message.get("role") == "user"
        and str(message.get("content", "")).startswith("BEGIN_ORION_EVIDENCE")
    )
    payload = json.loads(
        evidence.removeprefix("BEGIN_ORION_EVIDENCE\n").removesuffix(
            "\nEND_ORION_EVIDENCE"
        )
    )

    assert payload["data"]["reflection"] == reflection


def test_guard_context_type_hints_resolve():
    hints = get_type_hints(AgentRuntime._guard_context)

    assert "tools" in hints
