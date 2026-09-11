from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from cli_cockpit_backend import CockpitBackend
from event_handler import EventHandler
from orion_config import OrionConfig
from tool_policy import ToolClassification


def _build_app(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-secret-key")
    (tmp_path / "ORION_CORE.md").write_text("core", encoding="utf-8")
    (tmp_path / "tools").mkdir()
    config = OrionConfig.from_mapping(
        {
            "approvals": {"enabled": True, "path": "data/custom-approvals.sqlite3"},
            "ledger": {"path": "data/custom-actions.sqlite3"},
            "reflection": {"enabled": False},
            "context": {"reflection_enabled": False},
            "scheduler": {"enabled": False},
            "memory": {"enabled": False},
            "subagents": {
                "enabled": True,
                "workers": 1,
                "default_tools": ["danger"],
            },
            "tools": {
                "directory": "tools",
                "policy": {"privileged": ["danger"]},
            },
        }
    )
    config.config_path = tmp_path / "orion.toml"
    return config, config.build()


def test_config_build_wires_one_approval_store_and_tool_policy(tmp_path, monkeypatch):
    config, app = _build_app(tmp_path, monkeypatch)
    try:
        assert config.approvals.path == "data/custom-approvals.sqlite3"
        assert app.approval_store is app.runtime.approval_store
        assert Path(app.approval_store.path) == tmp_path / "data" / "custom-approvals.sqlite3"
        assert (
            app.runtime.tool_policy.rule_for("danger").classification
            is ToolClassification.PRIVILEGED
        )
        assert app.subagents is not None
        assert app.subagents.action_ledger is app.runtime.action_ledger
        assert Path(app.runtime.action_ledger.path) == tmp_path / "data" / "custom-actions.sqlite3"
        assert (
            app.subagents.tool_policy.rule_for("danger").classification
            is ToolClassification.PRIVILEGED
        )
        assert app.subagents.tool_authorizer is None
        assert callable(app.subagents.tool_approval_broker)
        assert app.subagents.tool_approval_broker.__self__ is app.runtime
    finally:
        app.stop()


def test_missing_approvals_section_defaults_to_yolo_mode():
    config = OrionConfig.from_mapping({})
    assert config.approvals.enabled is False
    assert config.approvals.path == "data/approvals.sqlite3"


def test_real_app_cockpit_lists_and_approves_pending_request(tmp_path, monkeypatch):
    _config, app = _build_app(tmp_path, monkeypatch)
    try:
        approval = app.approval_store.create(
            "runtime",
            "tool:danger",
            {"tool_id": "danger", "args_hash": "abc"},
            approval_id="approval-1",
        )
        backend = CockpitBackend(app)

        pending = backend.execute("/approve")
        assert pending["data"] == [
            {
                "id": approval["id"],
                "status": "pending",
                "requester": "runtime",
                "scope": "tool:danger",
                "created_at": approval["created_at"],
                "expires_at": None,
                "tool_id": "danger",
                "args_hash": "abc",
                "preview": None,
            }
        ]

        decided = backend.execute("/approve approval-1")
        assert decided.get("error") is None
        assert decided["data"]["status"] == "approved"
        assert app.approval_store.get("approval-1")["status"] == "approved"
    finally:
        app.stop()


def test_cockpit_pending_renders_review_preview_without_hidden_payload_fields(tmp_path, monkeypatch):
    _config, app = _build_app(tmp_path, monkeypatch)
    try:
        preview = {
            "version": 1,
            "tool_id": "terminal",
            "args_hash": "f" * 64,
            "kind": "terminal",
            "command": "echo reviewed",
            "cwd": "workspace",
            "timeout": 10,
        }
        app.approval_store.create(
            "runtime",
            "external",
            {
                "tool_id": "terminal",
                "args_hash": "f" * 64,
                "classification": "privileged",
                "preview": preview,
                "internal_field": "must-not-render",
            },
            approval_id="approval-preview",
        )

        result = CockpitBackend(app).execute("/approve")
        encoded = json.dumps(result, sort_keys=True)
        assert result["data"][0]["preview"] == preview
        assert result["data"][0]["tool_id"] == "terminal"
        assert "must-not-render" not in encoded
    finally:
        app.stop()


def test_event_projection_never_exposes_event_payload_or_callback_error():
    events = EventHandler(workers=0)
    events.publish("message", {"token": "payload-secret"})
    events._callback_errors.append(RuntimeError("callback-secret"))
    backend = CockpitBackend({"events": events})

    event_view = backend.execute("/events")
    watch_view = backend.execute("/watch")
    encoded = json.dumps({"events": event_view, "watch": watch_view}, sort_keys=True)

    assert event_view["data"]["queued"] == 1
    assert event_view["data"]["callback_errors"] == 1
    assert watch_view["data"] == event_view["data"]
    assert "payload-secret" not in encoded
    assert "callback-secret" not in encoded


def test_skills_and_model_use_public_non_secret_projections():
    manifest = SimpleNamespace(
        id="terminal",
        name="Terminal",
        version="1.0",
        description="Run commands",
        api_version=1,
        permissions=("filesystem",),
        configuration={"api_key": "manifest-secret"},
    )
    manager = SimpleNamespace(installed=lambda: [(manifest, Path("tools/terminal"))])
    llm = SimpleNamespace(
        model="provider/model",
        base_url="https://example.invalid/v1",
        timeout=30.0,
        max_retries=2,
        api_key="llm-secret",
        _headers={"Authorization": "Bearer llm-secret"},
        default_params={"private": "default-secret"},
    )
    backend = CockpitBackend({"tool_manager": manager, "llm": llm})

    skills = backend.execute("/skills")
    model = backend.execute("/model")
    encoded = json.dumps({"skills": skills, "model": model}, sort_keys=True)

    assert skills["data"] == [
        {
            "id": "terminal",
            "name": "Terminal",
            "version": "1.0",
            "description": "Run commands",
            "api_version": 1,
            "permissions": ["filesystem"],
        }
    ]
    assert model["data"]["model"] == "provider/model"
    assert "llm-secret" not in encoded
    assert "manifest-secret" not in encoded
    assert "default-secret" not in encoded


def test_default_production_mode_is_yolo_for_privileged_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-secret-key")
    (tmp_path / "ORION_CORE.md").write_text("core", encoding="utf-8")
    (tmp_path / "tools").mkdir()
    config = OrionConfig.from_mapping(
        {
            "reflection": {"enabled": False},
            "context": {"reflection_enabled": False},
            "scheduler": {"enabled": False},
            "memory": {"enabled": False},
            "subagents": {
                "enabled": True,
                "workers": 1,
                "default_tools": ["danger"],
            },
            "tools": {
                "directory": "tools",
                "policy": {"privileged": ["danger"]},
            },
        }
    )
    config.config_path = tmp_path / "orion.toml"
    app = config.build()
    try:
        assert config.approvals.enabled is False
        assert app.approval_store is None
        assert app.runtime.tool_policy.approvals_enabled is False
        decision = app.runtime.tool_policy.decide("danger", enabled=True, approved=False)
        assert decision.classification is ToolClassification.PRIVILEGED
        assert decision.approval_required is False
        assert decision.allowed is True
        assert app.subagents is not None
        assert app.subagents.tool_approval_broker is None
    finally:
        app.stop()
