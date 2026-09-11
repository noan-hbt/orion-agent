from __future__ import annotations

from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import orion_toolbox
import pytest
from github_tools import GithubTool
from tool_manager import ToolManifest, ToolPackageError
from tool_policy import ToolPackagePolicy


class _Manager:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.config = {
            "enabled": ["orion.tasks"],
            "disabled": ["orion.terminal"],
            "_core": {"subagents": {"default_tools": []}},
        }
        self.configured: list[str] = []
        self._installed_ids = {"orion.web"}
        self.items = [
            (
                ToolManifest(
                    id="orion.tasks",
                    name="Tasks",
                    version="1.0.0",
                    kind="module",
                    entrypoint="",
                    description="Durable task orchestration and scheduling.",
                    configuration={
                        "fields": [{"key": "enabled", "type": "boolean"}]
                    },
                ),
                tmp_path / "bundled" / "tasks",
            ),
            (
                ToolManifest(
                    id="orion.web",
                    name="Web",
                    version="1.3.0",
                    description="Search and read public web resources.",
                    policy=ToolPackagePolicy.from_mapping(
                        {
                            "classification": "read_only",
                            "tool_classifications": {"web": "read_only"},
                        }
                    ),
                ),
                tmp_path / "tools" / "orion.web",
            ),
            (
                ToolManifest(
                    id="orion.terminal",
                    name="Terminal",
                    version="1.0.0",
                    description="Run controlled local commands.",
                    policy=ToolPackagePolicy.from_mapping(
                        {
                            "classification": "privileged",
                            "explicit_enable": True,
                            "tool_classifications": {"terminal": "privileged"},
                        }
                    ),
                ),
                tmp_path / "bundled" / "terminal",
            ),
        ]

    def available(self):
        return list(self.items)

    def installed(self):
        return [item for item in self.items if item[0].id in self._installed_ids]

    def find(self, tool_id: str):
        return next((manifest for manifest, _ in self.items if manifest.id == tool_id), None)

    def set_package_enabled(self, tool_id: str, enabled: bool, *, config_path=None):
        known = {manifest.id for manifest, _ in self.items}
        if tool_id not in known:
            raise ToolPackageError(f"unknown: {tool_id}")
        enabled_ids = list(self.config["enabled"])
        disabled_ids = list(self.config["disabled"])
        if enabled:
            if tool_id not in enabled_ids:
                enabled_ids.append(tool_id)
            disabled_ids = [item for item in disabled_ids if item != tool_id]
        else:
            enabled_ids = [item for item in enabled_ids if item != tool_id]
            if tool_id not in disabled_ids:
                disabled_ids.append(tool_id)
        self.config["enabled"] = enabled_ids
        self.config["disabled"] = disabled_ids

    def configure(self, manifest, **kwargs):
        self.configured.append(manifest.id)
        return True


class _Catalog:
    def __init__(self, manager: _Manager) -> None:
        self.manager = manager
        self._tree = object()
        self._tools = object()
        self.searches: list[str] = []
        self.installs: list[tuple[str, bool]] = []
        self.remote = GithubTool(
            ToolManifest(
                id="example.remote",
                name="Remote Example",
                version="2.0.0",
                description="A remote package used for deterministic UI tests.",
            ),
            "example.remote",
        )

    def search(self, query: str = ""):
        self.searches.append(query)
        return [self.remote]

    def install(self, selected, manager, *, force=False):
        self.installs.append((selected.manifest.id, force))
        manager.items.append(
            (selected.manifest, manager.tmp_path / "tools" / selected.manifest.id)
        )
        manager._installed_ids.add(selected.manifest.id)
        return selected.manifest


def _config(tmp_path: Path):
    return SimpleNamespace(config_path=tmp_path / "orion.toml", base_dir=tmp_path)


def _inputs(*values: str):
    iterator = iter(values)
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return next(iterator)

    return ask, prompts


def test_dashboard_is_sectioned_status_aware_and_plain_when_not_tty(tmp_path: Path):
    manager = _Manager(tmp_path)
    stream = StringIO()
    ui = orion_toolbox.ToolboxUI(stream=stream, width=64, is_tty=False)

    orion_toolbox._render_dashboard(
        manager,
        ui,
        repository=None,
        ref="main",
        remote=None,
    )

    rendered = stream.getvalue()
    assert "ORION TOOLBOX" in rendered
    assert "1 enabled" in rendered
    assert "1 off" in rendered
    assert "1 disabled" in rendered
    assert "BUNDLED MODULES" in rendered
    assert "LOCAL TOOLS" in rendered
    assert "REMOTE" in rendered
    assert "[ON]" in rendered
    assert "[OFF]" in rendered
    assert "[DISABLED]" in rendered
    assert "\x1b[" not in rendered


def test_no_color_disables_ansi_even_for_tty(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    stream = StringIO()
    ui = orion_toolbox.ToolboxUI(
        stream=stream, error_stream=stream, width=52, is_tty=True
    )

    ui.success("done")
    ui.error("bad")

    assert "\x1b[" not in stream.getvalue()


@pytest.mark.parametrize("width", [52, 80, 120])
def test_dashboard_adapts_to_common_terminal_widths_and_cp1252(
    tmp_path: Path, width: int
):
    manager = _Manager(tmp_path)
    stream = StringIO()
    ui = orion_toolbox.ToolboxUI(stream=stream, width=width, is_tty=False)

    orion_toolbox._render_dashboard(
        manager,
        ui,
        repository="owner/repo",
        ref="main",
        remote=None,
    )

    rendered = stream.getvalue()
    # Windows consoles commonly still use a legacy Western code page. Keep the
    # visual chrome representable there even when labels themselves are French.
    rendered.encode("cp1252", errors="strict")
    divider_lines = [line for line in rendered.splitlines() if line.startswith("  --")]
    assert divider_lines
    assert all(len(line) <= width for line in divider_lines)
    assert "BUNDLED MODULES" in rendered
    assert "LOCAL TOOLS" in rendered


def test_detail_shows_risk_settings_and_contextual_actions(tmp_path: Path):
    manager = _Manager(tmp_path)
    stream = StringIO()
    ui = orion_toolbox.ToolboxUI(stream=stream, width=70, is_tty=False)
    item = orion_toolbox._resolve_local(manager, "1")
    assert item is not None

    ui.detail(item, "off")

    rendered = stream.getvalue()
    assert "Risk     side_effect" in rendered
    assert "Settings 1" in rendered
    assert "e enable" in rendered
    assert "d disable" in rendered
    assert "c configure" in rendered
    assert "b back" in rendered


def test_dashboard_distinguishes_orion_activation_from_worker_access(tmp_path: Path):
    manager = _Manager(tmp_path)
    manager.config["enabled"] = ["orion.subagents", "orion.web", "orion.terminal"]
    manager.config["disabled"] = []
    stream = StringIO()
    ui = orion_toolbox.ToolboxUI(stream=stream, width=84, is_tty=False)

    orion_toolbox._render_dashboard(
        manager,
        ui,
        repository=None,
        ref="main",
        remote=None,
    )

    rendered = stream.getvalue()
    assert "WORKERS subagents ON" in rendered
    assert "0/1 active safe tools allowed" in rendered
    assert "Workers: BLOCKED (use w to allow)" in rendered
    assert "Workers: BLOCKED (use w; approval still required)" in rendered


def test_worker_tools_loop_allows_explicit_privileged_tool_with_approval_warning(tmp_path: Path):
    config_path = tmp_path / "orion.toml"
    config_path.write_text(
        '[subagents]\n'
        'default_tools = []\n\n'
        '[tools]\n'
        'enabled = ["orion.subagents", "orion.web", "orion.terminal"]\n'
        'disabled = []\n',
        encoding="utf-8",
    )
    manager = _Manager(tmp_path)
    manager.config["enabled"] = ["orion.subagents", "orion.web", "orion.terminal"]
    manager.config["disabled"] = []
    stream = StringIO()
    ui = orion_toolbox.ToolboxUI(stream=stream, width=84, is_tty=False)
    ask, _prompts = _inputs("1", "2", "b")

    orion_toolbox._worker_tools_loop(
        manager=manager,
        config_path=config_path,
        ui=ui,
        input_fn=ask,
    )

    assert manager.config["_core"]["subagents"]["default_tools"] == ["terminal", "web"]
    text = config_path.read_text(encoding="utf-8")
    assert 'default_tools = ["terminal", "web"]' in text
    rendered = stream.getvalue()
    assert "Web" in rendered and "pour les workers" in rendered
    assert "approbation Orion" in rendered


def test_worker_tools_none_keeps_fail_closed_ceiling(tmp_path: Path):
    config_path = tmp_path / "orion.toml"
    config_path.write_text(
        '[subagents]\n'
        'default_tools = ["web"]\n',
        encoding="utf-8",
    )
    manager = _Manager(tmp_path)
    manager.config["enabled"] = ["orion.subagents", "orion.web"]
    manager.config["disabled"] = []
    manager.config["_core"]["subagents"]["default_tools"] = ["web"]
    stream = StringIO()
    ui = orion_toolbox.ToolboxUI(stream=stream, width=72, is_tty=False)
    ask, _prompts = _inputs("n", "b")

    orion_toolbox._worker_tools_loop(
        manager=manager,
        config_path=config_path,
        ui=ui,
        input_fn=ask,
    )

    assert manager.config["_core"]["subagents"]["default_tools"] == []
    assert "default_tools = []" in config_path.read_text(encoding="utf-8")


def test_interactive_number_detail_can_enable_then_return_to_dashboard(tmp_path: Path):
    manager = _Manager(tmp_path)
    manager.config = {
        "enabled": [],
        "disabled": [],
        "_core": {"subagents": {"default_tools": []}},
    }
    stream = StringIO()
    ui = orion_toolbox.ToolboxUI(stream=stream, width=72, is_tty=False)
    ask, _prompts = _inputs("1", "e", "b", "q")

    result = orion_toolbox._interactive_loop(
        manager=manager,
        config=_config(tmp_path),
        config_path=tmp_path / "orion.toml",
        ui=ui,
        catalog=None,
        repository=None,
        ref="main",
        input_fn=ask,
    )

    assert result == 0
    assert manager.config["enabled"] == ["orion.tasks"]
    assert manager.config["disabled"] == []
    rendered = stream.getvalue()
    assert "DETAIL" in rendered
    assert "orion.tasks" in rendered and "activ" in rendered
    assert rendered.count("STATUS") >= 2


def test_interactive_simple_commands_resolve_numeric_targets_and_configure(tmp_path: Path):
    manager = _Manager(tmp_path)
    stream = StringIO()
    ui = orion_toolbox.ToolboxUI(stream=stream, width=72, is_tty=False)
    ask, _prompts = _inputs("disable 2", "config 1", "q")

    result = orion_toolbox._interactive_loop(
        manager=manager,
        config=_config(tmp_path),
        config_path=tmp_path / "orion.toml",
        ui=ui,
        catalog=None,
        repository=None,
        ref="main",
        input_fn=ask,
    )

    assert result == 0
    assert "orion.web" in manager.config["disabled"]
    assert manager.configured == ["orion.tasks"]


def test_remote_section_is_loaded_separately_and_install_keeps_confirmation(tmp_path: Path):
    manager = _Manager(tmp_path)
    catalog = _Catalog(manager)
    stream = StringIO()
    ui = orion_toolbox.ToolboxUI(stream=stream, width=76, is_tty=False)
    ask, prompts = _inputs("r", "r1", "i", "y", "n", "q")

    result = orion_toolbox._interactive_loop(
        manager=manager,
        config=_config(tmp_path),
        config_path=tmp_path / "orion.toml",
        ui=ui,
        catalog=catalog,
        repository="owner/repo",
        ref="main",
        input_fn=ask,
    )

    assert result == 0
    assert catalog.searches == [""]
    assert catalog.installs == [("example.remote", False)]
    assert "example.remote" not in manager.config["enabled"]
    assert any("Installer Remote Example" in prompt for prompt in prompts)
    assert any("Activer Remote Example" in prompt for prompt in prompts)
    rendered = stream.getvalue()
    assert "R1  Remote Example" in rendered
    assert "REMOTE DETAIL" in rendered


def test_main_without_action_and_without_tty_never_prompts_or_constructs_remote_catalog(
    tmp_path: Path, monkeypatch, capsys
):
    config_path = tmp_path / "orion.toml"
    config_path.write_text(
        '[tools]\n'
        'directory = "tools"\n'
        'state_path = "data/installed_tools.json"\n'
        'enabled = []\n'
        'disabled = []\n\n'
        '[tools.github]\n'
        'repo = "owner/repo"\n'
        'ref = "main"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(orion_toolbox, "_terminal_is_interactive", lambda: False)
    monkeypatch.setattr(
        "builtins.input", lambda _prompt="": pytest.fail("non-TTY mode must not prompt")
    )

    class _ForbiddenCatalog:
        def __init__(self, *args, **kwargs):
            raise AssertionError("non-TTY snapshot must not construct/fetch remote catalog")

    monkeypatch.setattr(orion_toolbox, "GithubToolCatalog", _ForbiddenCatalog)

    assert orion_toolbox.main(["--config", str(config_path)]) == 0
    output = capsys.readouterr().out
    assert "ORION TOOLBOX" in output
    assert "Mode interactif indisponible sans TTY" in output


def test_main_reports_config_load_error_without_traceback(tmp_path: Path, capsys):
    missing = tmp_path / "missing.toml"

    assert orion_toolbox.main(["--config", str(missing), "--list"]) == 1

    captured = capsys.readouterr()
    assert "Configuration invalide" in captured.err
    assert "Traceback" not in captured.err


def test_contradictory_one_shot_flags_fail_before_any_mutation(tmp_path: Path):
    config_path = tmp_path / "orion.toml"
    original = (
        '[tools]\n'
        'directory = "tools"\n'
        'state_path = "data/installed_tools.json"\n'
        'enabled = []\n'
        'disabled = []\n'
    )
    config_path.write_text(original, encoding="utf-8")

    assert (
        orion_toolbox.main(
            [
                "--config",
                str(config_path),
                "--enable",
                "orion.tasks",
                "--disable",
                "orion.tasks",
            ]
        )
        == 2
    )
    assert config_path.read_text(encoding="utf-8") == original
