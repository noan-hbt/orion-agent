from __future__ import annotations

from pathlib import Path

import orion_toolbox
from orion_config import OrionConfig
from tool_manager import ToolManager


ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "tool_packages"


class _NoToolClient:
    def register_tool(self, *args, **kwargs):
        raise AssertionError("runtime metadata modules must not register Python tools")


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "orion.toml"
    path.write_text(
        '[tools]\n'
        'directory = "tools"\n'
        'state_path = "data/installed_tools.json"\n'
        'enabled = []\n'
        'disabled = []\n',
        encoding="utf-8",
    )
    return path


def _build_config(tmp_path: Path, data: dict | None = None) -> OrionConfig:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "ORION_CORE.md").write_text("core", encoding="utf-8")
    config = OrionConfig.from_mapping(
        {
            "reflection": {"enabled": False},
            "context": {"reflection_enabled": False},
            "memory": {"enabled": False},
            **(data or {}),
        }
    )
    config.config_path = tmp_path / "orion.toml"
    return config


def test_default_config_is_capability_empty():
    config = OrionConfig.from_mapping({})

    assert config.tools.enabled == []
    assert config.subagents.enabled is False
    assert config.subagents.default_tools == []
    assert config.scheduler.enabled is False
    assert config.tasks.enabled is False
    assert config.teams.enabled is False


def test_repository_sample_is_capability_empty():
    config = OrionConfig.from_file(ROOT / "orion.toml")

    assert config.tools.enabled == []
    assert config.subagents.enabled is False
    assert config.subagents.default_tools == []
    assert config.scheduler.enabled is False
    assert config.tasks.enabled is False
    assert config.teams.enabled is False


def test_legacy_subagent_tool_names_migrate_to_consolidated_callables():
    config = OrionConfig.from_mapping(
        {
            "subagents": {
                "default_tools": [
                    "web_search",
                    "fetch_json_api",
                    "read_file",
                    "search_files",
                    "terminal",
                ]
            }
        }
    )

    assert config.subagents.default_tools == ["web", "files", "terminal"]


def test_bundled_runtime_modules_load_metadata_and_guidance_without_python(tmp_path: Path):
    manager = ToolManager(
        install_dir=tmp_path / "tools",
        root_dir=tmp_path,
        bundled_dir=BUNDLED,
        config={"enabled": ["orion.tasks", "orion.subagents", "orion.team"]},
    )

    loaded = manager.load_all(_NoToolClient())

    assert [(item.id, item.kind) for item in loaded] == [
        ("orion.subagents", "module"),
        ("orion.tasks", "module"),
        ("orion.team", "module"),
    ]
    assert set(manager.loaded_guidance()) == {
        "orion.tasks",
        "orion.subagents",
        "orion.team",
    }


def test_toolbox_manages_bundled_module_without_constructing_github_catalog(
    tmp_path: Path, monkeypatch
):
    config_path = _config_file(tmp_path)

    class _ForbiddenCatalog:
        def __init__(self, *args, **kwargs):
            raise AssertionError("GitHub must not be required for bundled module management")

    monkeypatch.setattr(orion_toolbox, "GithubToolCatalog", _ForbiddenCatalog)

    assert orion_toolbox.main(["--config", str(config_path), "--list"]) == 0
    assert (
        orion_toolbox.main(
            ["--config", str(config_path), "--enable", "orion.tasks"]
        )
        == 0
    )
    enabled = OrionConfig.from_file(config_path)
    assert enabled.tools.enabled == ["orion.tasks"]
    assert enabled.tools.disabled == []

    assert (
        orion_toolbox.main(
            ["--config", str(config_path), "--disable", "orion.tasks"]
        )
        == 0
    )
    disabled = OrionConfig.from_file(config_path)
    assert disabled.tools.enabled == []
    assert disabled.tools.disabled == ["orion.tasks"]


def test_bundled_module_configuration_writes_declared_core_table(tmp_path: Path):
    config_path = _config_file(tmp_path)
    config = OrionConfig.from_file(config_path)
    manager = orion_toolbox._manager(config)
    manifest = manager.find("orion.team")
    assert manifest is not None

    answers = iter(["worker-a", "research", "2.5", "9000"])
    changed = manager.configure(
        manifest,
        config_path=config_path,
        env_path=tmp_path / ".env",
        input_fn=lambda _prompt: next(answers),
    )

    assert changed is True
    parsed = OrionConfig.from_file(config_path)
    assert parsed.teams.instance_id == "worker-a"
    assert parsed.teams.team == "research"
    assert parsed.teams.poll_interval == 2.5
    assert parsed.teams.max_message_chars == 9000
    # Configuration must not implicitly activate a capability.
    assert parsed.tools.enabled == []


def test_empty_product_wiring_exposes_no_model_tools(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    config = _build_config(tmp_path)

    app = config.build()
    try:
        assert app.scheduler is None
        assert app.subagents is None
        assert app.team_bus is None
        assert app.runtime.runtime_surfaces == set()
        assert app.runtime._tool_definitions() == []
        assert app.runtime.tool_guidance == {}
        assert app.runtime.task_store.__class__.__name__ == "InMemoryTaskStore"
    finally:
        app.stop()


def test_enabled_modules_construct_services_surfaces_and_prompt_guidance(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    config = _build_config(
        tmp_path,
        {
            "tools": {
                "enabled": ["orion.tasks", "orion.subagents", "orion.team"]
            },
        },
    )

    app = config.build()
    try:
        assert app.scheduler is not None
        assert app.subagents is not None
        assert app.team_bus is not None
        assert app.runtime.task_store.__class__.__name__ == "JsonTaskStore"
        assert app.runtime.runtime_surfaces == {"task", "event", "subagent", "team"}
        names = {
            item["function"]["name"] for item in app.runtime._tool_definitions()
        }
        assert names == {"task", "event", "subagent", "team"}
        assert set(app.runtime.tool_guidance) == {
            "orion.tasks",
            "orion.subagents",
            "orion.team",
        }
        system = app.runtime._system_instructions()
        assert "### orion.tasks" in system
        assert "### orion.subagents" in system
        assert "### orion.team" in system
    finally:
        app.stop()


def test_legacy_service_flags_remain_compatible_but_explicit_module_disable_wins(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    legacy = _build_config(
        tmp_path / "legacy",
        {"subagents": {"enabled": True, "workers": 1}},
    )
    legacy_app = legacy.build()
    try:
        assert legacy_app.subagents is not None
        assert legacy_app.runtime.runtime_surfaces == {"subagent", "event"}
    finally:
        legacy_app.stop()

    blocked = _build_config(
        tmp_path / "blocked",
        {
            "subagents": {"enabled": True, "workers": 1},
            "tools": {"disabled": ["orion.subagents"]},
        },
    )
    blocked_app = blocked.build()
    try:
        assert blocked_app.subagents is None
        assert blocked_app.runtime.runtime_surfaces == set()
    finally:
        blocked_app.stop()
