from __future__ import annotations

from pathlib import Path

from orion_config import OrionConfig
from orion_tools import main
from tool_manager import ToolManager


def _write_package(root: Path) -> Path:
    package = root / "package"
    package.mkdir(parents=True)
    (package / "tool.toml").write_text(
        'id = "example.cli"\n'
        'name = "CLI tool"\n'
        'version = "1.0.0"\n'
        '[policy]\n'
        'classification = "read_only"\n'
        'tool_classifications = { cli_lookup = "read_only" }\n',
        encoding="utf-8",
    )
    (package / "tool.py").write_text(
        "def register(client):\n"
        "    client.register_tool('cli_lookup', lambda: 'ok')\n",
        encoding="utf-8",
    )
    return package


class _Client:
    def __init__(self) -> None:
        self.registered: list[str] = []

    def register_tool(self, name, *args, **kwargs):
        self.registered.append(str(name))


def test_cli_install_enable_persists_activation_and_loads_on_restart(tmp_path: Path):
    config_path = tmp_path / "orion.toml"
    config_path.write_text(
        '[tools]\n'
        'directory = "tools"\n'
        'state_path = "data/installed_tools.json"\n'
        'enabled = []\n'
        'disabled = []\n',
        encoding="utf-8",
    )
    package = _write_package(tmp_path)

    assert main(["--config", str(config_path), "install", str(package), "--enable"]) == 0

    config = OrionConfig.from_file(config_path)
    assert config.tools.enabled == ["example.cli"]
    manager = ToolManager(
        config.path(config.tools.directory),
        state_path=config.path(config.tools.state_path),
        root_dir=config.base_dir,
        config={"enabled": config.tools.enabled, "disabled": config.tools.disabled},
    )
    client = _Client()
    loaded = manager.load_all(client)
    assert [manifest.id for manifest in loaded] == ["example.cli"]
    assert client.registered == ["cli_lookup"]


def test_cli_install_without_enable_stays_fail_closed(tmp_path: Path):
    config_path = tmp_path / "orion.toml"
    config_path.write_text(
        '[tools]\n'
        'directory = "tools"\n'
        'state_path = "data/installed_tools.json"\n'
        'enabled = []\n'
        'disabled = []\n',
        encoding="utf-8",
    )
    package = _write_package(tmp_path)

    assert main(["--config", str(config_path), "install", str(package)]) == 0

    assert OrionConfig.from_file(config_path).tools.enabled == []
