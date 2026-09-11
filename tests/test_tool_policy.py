"""Focused tests for the central tool policy/capability layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from tool_manager import ToolManager, ToolManifest, ToolPackageError
from tool_policy import (
    ToolClassification,
    ToolPackagePolicy,
    ToolPolicy,
    ToolPolicyDenied,
    ToolPolicyError,
)


def test_privileged_tool_requires_approval_but_other_classes_do_not():
    policy = ToolPolicy(
        {
            "read": ToolClassification.READ_ONLY,
            "write": ToolClassification.SIDE_EFFECT,
            "terminal": ToolClassification.PRIVILEGED,
        }
    )

    assert policy.decide("read").allowed is True
    assert policy.decide("write").allowed is True
    denied = policy.decide("terminal")
    assert denied.allowed is False
    assert denied.approval_required is True
    assert "approval" in (denied.reason or "").lower()
    assert policy.decide("terminal", approved=True).allowed is True

    with pytest.raises(ToolPolicyDenied):
        policy.require("terminal")


def test_disabled_tool_is_denied_even_when_privileged_approval_exists():
    policy = ToolPolicy({"terminal": "privileged"})

    decision = policy.decide("terminal", enabled=False, approved=True)

    assert decision.allowed is False
    assert "disabled" in (decision.reason or "").lower()


def test_operator_policy_can_raise_but_not_lower_manifest_risk():
    policy = ToolPolicy.from_config(
        {"read_only": ["terminal"], "side_effect": [], "privileged": []}
    )
    package = ToolPackagePolicy.from_mapping(
        {
            "classification": "privileged",
            "tool_classifications": {"terminal": "privileged"},
        }
    )

    policy.register_package("orion.terminal", package)

    assert policy.rule_for("terminal").classification is ToolClassification.PRIVILEGED


def test_operator_policy_rejects_conflicting_classification_lists():
    with pytest.raises(ToolPolicyError, match="both"):
        ToolPolicy.from_config(
            {"read_only": ["same"], "side_effect": ["same"], "privileged": []}
        )


def test_manifest_policy_parses_and_serializes(tmp_path: Path):
    path = tmp_path / "tool.toml"
    path.write_text(
        '\n'.join(
            [
                'id = "example.secure"',
                'name = "Secure"',
                'version = "1.0.0"',
                '',
                '[policy]',
                'classification = "privileged"',
                'explicit_enable = true',
                'tool_classifications = { dangerous = "privileged" }',
            ]
        ),
        encoding="utf-8",
    )

    manifest = ToolManifest.from_file(path)

    assert manifest.policy.classification is ToolClassification.PRIVILEGED
    assert manifest.policy.requires_explicit_enable is True
    assert manifest.to_dict()["policy"]["tool_classifications"] == {
        "dangerous": "privileged"
    }


def test_privileged_manifest_requires_declared_callable_names(tmp_path: Path):
    path = tmp_path / "tool.toml"
    path.write_text(
        'id = "example.bad"\nname = "Bad"\nversion = "1.0.0"\n'
        '[policy]\nclassification = "privileged"\n',
        encoding="utf-8",
    )

    with pytest.raises(ToolPackageError, match="tool_classifications"):
        ToolManifest.from_file(path)


def test_builtin_terminal_and_web_manifests_declare_expected_risk():
    root = Path(__file__).resolve().parents[1]
    terminal = ToolManifest.from_file(root / "tool_packages" / "terminal" / "tool.toml")
    web = ToolManifest.from_file(root / "tool_packages" / "web" / "tool.toml")

    assert terminal.policy.classification is ToolClassification.PRIVILEGED
    assert terminal.policy.requires_explicit_enable is True
    assert dict(terminal.policy.tool_classifications)["terminal"] is ToolClassification.PRIVILEGED
    web_rules = dict(web.policy.tool_classifications)
    assert web.policy.classification is ToolClassification.READ_ONLY
    assert web_rules == {"web": ToolClassification.READ_ONLY}


def test_privileged_callable_makes_package_explicit_even_if_package_default_is_read_only():
    package = ToolPackagePolicy.from_mapping(
        {
            "classification": "read_only",
            "tool_classifications": {"safe": "read_only", "dangerous": "privileged"},
        }
    )

    assert package.requires_explicit_enable is True


def test_package_classification_is_a_floor_for_declared_tool_risk():
    policy = ToolPolicy()
    package = ToolPackagePolicy.from_mapping(
        {
            "classification": "privileged",
            "tool_classifications": {"terminal": "read_only"},
        }
    )

    policy.register_package("orion.terminal", package)

    assert policy.rule_for("terminal").classification is ToolClassification.PRIVILEGED


class _RecordingClient:
    def __init__(self) -> None:
        self.registered: list[str] = []

    def register_tool(self, name, *args, **kwargs):
        self.registered.append(str(name))


def _write_package(
    install_dir: Path,
    package_id: str,
    *,
    classification: str = "side_effect",
    explicit_enable: bool = False,
    tool_name: str = "example_tool",
    import_marker: Path | None = None,
) -> None:
    package_dir = install_dir / package_id
    package_dir.mkdir(parents=True)
    import_side_effect = ""
    if import_marker is not None:
        import_side_effect = (
            "from pathlib import Path\n"
            f"Path({str(import_marker)!r}).write_text('imported', encoding='utf-8')\n"
        )
    (package_dir / "tool.py").write_text(
        import_side_effect
        + f'def register(client):\n    client.register_tool("{tool_name}", lambda: None)\n',
        encoding="utf-8",
    )
    (package_dir / "tool.toml").write_text(
        '\n'.join(
            [
                f'id = "{package_id}"',
                f'name = "{package_id}"',
                'version = "1.0.0"',
                '',
                '[policy]',
                f'classification = "{classification}"',
                f'explicit_enable = {str(explicit_enable).lower()}',
                f'tool_classifications = {{ {tool_name} = "{classification}" }}',
            ]
        ),
        encoding="utf-8",
    )


def test_tool_manager_does_not_auto_load_privileged_package(tmp_path: Path):
    install_dir = tmp_path / "tools"
    import_marker = tmp_path / "privileged-imported.txt"
    _write_package(
        install_dir,
        "orion.terminal",
        classification="privileged",
        explicit_enable=True,
        tool_name="terminal",
        import_marker=import_marker,
    )
    client = _RecordingClient()
    manager = ToolManager(install_dir=install_dir, root_dir=tmp_path, config={"enabled": []})

    loaded = manager.load_all(client)

    assert loaded == []
    assert client.registered == []
    assert not import_marker.exists()
    decision = manager.tool_policy_decision("terminal")
    assert decision.enabled is False
    assert decision.classification is ToolClassification.PRIVILEGED


def test_tool_manager_explicit_enable_loads_terminal_but_policy_still_requires_approval(tmp_path: Path):
    install_dir = tmp_path / "tools"
    _write_package(
        install_dir,
        "orion.terminal",
        classification="privileged",
        explicit_enable=True,
        tool_name="terminal",
    )
    client = _RecordingClient()
    manager = ToolManager(
        install_dir=install_dir,
        root_dir=tmp_path,
        config={"enabled": ["orion.terminal"]},
    )

    loaded = manager.load_all(client)

    assert [manifest.id for manifest in loaded] == ["orion.terminal"]
    assert client.registered == ["terminal"]
    assert manager.tool_policy_decision("terminal").allowed is False
    assert manager.tool_policy_decision("terminal", approved=True).allowed is True


def test_tool_manager_does_not_import_explicit_enable_package_until_enabled(tmp_path: Path):
    install_dir = tmp_path / "tools"
    import_marker = tmp_path / "explicit-imported.txt"
    _write_package(
        install_dir,
        "example.explicit",
        classification="read_only",
        explicit_enable=True,
        tool_name="inspect",
        import_marker=import_marker,
    )
    manager = ToolManager(install_dir=install_dir, root_dir=tmp_path, config={"enabled": []})

    # Catalogue/manifest inspection must remain side-effect free.
    installed = manager.installed()
    assert [manifest.id for manifest, _package_dir in installed] == ["example.explicit"]
    assert not import_marker.exists()

    loaded = manager.load_all(_RecordingClient())
    assert loaded == []
    assert not import_marker.exists()


def test_operator_privileged_package_override_blocks_import_until_explicit_enable(tmp_path: Path):
    install_dir = tmp_path / "tools"
    import_marker = tmp_path / "operator-package-imported.txt"
    _write_package(
        install_dir,
        "example.legacy",
        classification="side_effect",
        tool_name="legacy_action",
        import_marker=import_marker,
    )
    manager = ToolManager(
        install_dir=install_dir,
        root_dir=tmp_path,
        config={
            "enabled": [],
            "policy": {"read_only": [], "side_effect": [], "privileged": ["example.legacy"]},
        },
    )

    assert manager.load_all(_RecordingClient()) == []
    assert not import_marker.exists()


def test_operator_privileged_callable_override_blocks_package_import(tmp_path: Path):
    install_dir = tmp_path / "tools"
    import_marker = tmp_path / "operator-callable-imported.txt"
    _write_package(
        install_dir,
        "example.callable",
        classification="read_only",
        tool_name="sensitive_lookup",
        import_marker=import_marker,
    )
    manager = ToolManager(
        install_dir=install_dir,
        root_dir=tmp_path,
        config={
            "enabled": [],
            "policy": {"read_only": [], "side_effect": [], "privileged": ["sensitive_lookup"]},
        },
    )

    assert manager.load_all(_RecordingClient()) == []
    assert not import_marker.exists()

    explicitly_enabled = ToolManager(
        install_dir=install_dir,
        root_dir=tmp_path,
        config={
            "enabled": ["example.callable"],
            "policy": {"read_only": [], "side_effect": [], "privileged": ["sensitive_lookup"]},
        },
    )
    loaded = explicitly_enabled.load_all(_RecordingClient())
    assert [manifest.id for manifest in loaded] == ["example.callable"]
    assert import_marker.read_text(encoding="utf-8") == "imported"


@pytest.mark.parametrize("config", [{}, {"enabled": []}])
def test_unknown_package_is_not_auto_imported_without_operator_allowlist(
    tmp_path: Path, config: dict[str, object]
):
    install_dir = tmp_path / "tools"
    import_marker = tmp_path / "legacy-imported.txt"
    package_dir = install_dir / "example.legacy"
    package_dir.mkdir(parents=True)
    (package_dir / "tool.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(import_marker)!r}).write_text('imported', encoding='utf-8')\n"
        "def register(client):\n    client.register_tool('legacy_action', lambda: None)\n",
        encoding="utf-8",
    )
    (package_dir / "tool.toml").write_text(
        'id = "example.legacy"\nname = "Legacy"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    client = _RecordingClient()
    manager = ToolManager(install_dir=install_dir, root_dir=tmp_path, config=config)

    loaded = manager.load_all(client)

    assert loaded == []
    assert client.registered == []
    assert not import_marker.exists()
    assert manager.tool_policy().rule_for("example.legacy").classification is ToolClassification.SIDE_EFFECT


def test_unknown_package_imports_and_registers_when_explicitly_allowlisted(tmp_path: Path):
    install_dir = tmp_path / "tools"
    import_marker = tmp_path / "allowlisted-imported.txt"
    _write_package(
        install_dir,
        "example.allowlisted",
        classification="read_only",
        tool_name="inspect",
        import_marker=import_marker,
    )
    client = _RecordingClient()
    manager = ToolManager(
        install_dir=install_dir,
        root_dir=tmp_path,
        config={"enabled": ["example.allowlisted"]},
    )

    loaded = manager.load_all(client)

    assert [manifest.id for manifest in loaded] == ["example.allowlisted"]
    assert client.registered == ["inspect"]
    assert import_marker.read_text(encoding="utf-8") == "imported"


def test_builtin_looking_read_only_manifest_does_not_bypass_explicit_allowlist(tmp_path: Path):
    install_dir = tmp_path / "tools"
    import_marker = tmp_path / "spoofed-builtin-imported.txt"
    _write_package(
        install_dir,
        "orion.web",
        classification="read_only",
        tool_name="web",
        import_marker=import_marker,
    )
    client = _RecordingClient()
    manager = ToolManager(install_dir=install_dir, root_dir=tmp_path, config={"enabled": []})

    assert manager.load_all(client) == []
    assert client.registered == []
    assert not import_marker.exists()


def test_disabled_package_stays_blocked_even_when_explicitly_allowlisted(tmp_path: Path):
    install_dir = tmp_path / "tools"
    import_marker = tmp_path / "disabled-imported.txt"
    _write_package(
        install_dir,
        "example.disabled",
        classification="read_only",
        tool_name="inspect",
        import_marker=import_marker,
    )
    client = _RecordingClient()
    manager = ToolManager(
        install_dir=install_dir,
        root_dir=tmp_path,
        config={
            "enabled": ["example.disabled"],
            "disabled": ["example.disabled"],
        },
    )

    assert manager.load_all(client) == []
    assert client.registered == []
    assert not import_marker.exists()


def test_explicit_package_activation_persists_and_allows_next_load(tmp_path: Path):
    install_dir = tmp_path / "tools"
    _write_package(
        install_dir,
        "example.enabled",
        classification="read_only",
        tool_name="inspect",
    )
    config_path = tmp_path / "orion.toml"
    config_path.write_text(
        '[tools]\n'
        'directory = "tools"\n'
        'enabled = ["orion.web"]\n'
        'disabled = ["example.enabled"]\n',
        encoding="utf-8",
    )
    manager = ToolManager(
        install_dir=install_dir,
        root_dir=tmp_path,
        config={"enabled": ["orion.web"], "disabled": ["example.enabled"]},
    )

    manager.set_package_enabled("example.enabled", True, config_path=config_path)

    text = config_path.read_text(encoding="utf-8")
    assert 'enabled = ["orion.web", "example.enabled"]' in text
    assert 'disabled = []' in text
    client = _RecordingClient()
    reloaded = ToolManager(
        install_dir=install_dir,
        root_dir=tmp_path,
        config={"enabled": ["orion.web", "example.enabled"], "disabled": []},
    )
    loaded = reloaded.load_all(client)
    assert [manifest.id for manifest in loaded] == ["example.enabled"]
    assert client.registered == ["inspect"]


def test_explicit_package_disable_removes_enablement(tmp_path: Path):
    install_dir = tmp_path / "tools"
    _write_package(
        install_dir,
        "example.enabled",
        classification="read_only",
        tool_name="inspect",
    )
    config_path = tmp_path / "orion.toml"
    config_path.write_text(
        '[tools]\nenabled = ["example.enabled"]\ndisabled = []\n',
        encoding="utf-8",
    )
    manager = ToolManager(
        install_dir=install_dir,
        root_dir=tmp_path,
        config={"enabled": ["example.enabled"], "disabled": []},
    )

    manager.set_package_enabled("example.enabled", False, config_path=config_path)

    text = config_path.read_text(encoding="utf-8")
    assert 'enabled = []' in text
    assert 'disabled = ["example.enabled"]' in text


def test_privileged_tool_yolo_mode_keeps_classification_but_skips_approval():
    policy = ToolPolicy({"terminal": "privileged"}, approvals_enabled=False)

    decision = policy.decide("terminal", enabled=True, approved=False)

    assert decision.classification is ToolClassification.PRIVILEGED
    assert decision.approval_required is False
    assert decision.allowed is True
    assert decision.reason is None
