from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tool_manager import ToolManager
from tool_packages.files import tool as files_tool


class _Client:
    def __init__(self) -> None:
        self.registered: dict[str, object] = {}

    def register_tool(self, name, callback, **_kwargs):
        self.registered[str(name)] = callback


def _context(root: Path, **settings):
    return SimpleNamespace(root_dir=root, config={"files": settings})


def test_read_file_is_workspace_bounded_and_redacts_obvious_secret_assignments(tmp_path):
    source = tmp_path / "config.toml"
    source.write_text(
        'model = "demo/model"\nAPI_TOKEN = "must-not-leak"\nanswer = 42\n',
        encoding="utf-8",
    )

    result = files_tool.read_file("config.toml", _context=_context(tmp_path))

    assert result["path"] == "config.toml"
    assert 'model = "demo/model"' in result["content"]
    assert "must-not-leak" not in result["content"]
    assert "[REDACTED]" in result["content"]
    assert "answer = 42" in result["content"]


def test_read_file_refuses_sensitive_files_and_parent_escape(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    outside = tmp_path.parent / "outside-orion-file.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        with pytest.raises(PermissionError):
            files_tool.read_file(".env", _context=_context(tmp_path))
        with pytest.raises(PermissionError):
            files_tool.read_file("../outside-orion-file.txt", _context=_context(tmp_path))
    finally:
        outside.unlink(missing_ok=True)


def test_list_and_search_files_find_toml_without_reading_sensitive_files(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "a.toml").write_text("alpha = 1\nneedle = true\n", encoding="utf-8")
    (tmp_path / "nested" / "b.py").write_text("needle = False\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("needle=secret\n", encoding="utf-8")
    context = _context(tmp_path)

    listing = files_tool.list_files(".", "*.toml", _context=context)
    assert [item["path"] for item in listing["files"]] == ["nested/a.toml"]

    found = files_tool.search_files("needle", ".", "*.toml", _context=context)
    assert found["matches"] == [
        {"path": "nested/a.toml", "line": 2, "text": "needle = true"}
    ]


def test_bundled_files_package_still_requires_explicit_enable(tmp_path):
    bundled_dir = Path(files_tool.__file__).resolve().parents[1]
    disabled_client = _Client()
    disabled = ToolManager(
        tmp_path / "tools-disabled",
        root_dir=tmp_path,
        bundled_dir=bundled_dir,
        config={"enabled": []},
    )
    assert disabled.load_all(disabled_client) == []
    assert disabled_client.registered == {}

    enabled_client = _Client()
    enabled = ToolManager(
        tmp_path / "tools-enabled",
        root_dir=tmp_path,
        bundled_dir=bundled_dir,
        config={"enabled": ["orion.files"]},
    )
    loaded = enabled.load_all(enabled_client)

    assert [manifest.id for manifest in loaded] == ["orion.files"]
    assert set(enabled_client.registered) == {"files"}


def test_files_model_surface_dispatches_all_read_only_actions(tmp_path):
    (tmp_path / "notes.txt").write_text("alpha\nneedle here\nomega\n", encoding="utf-8")
    client = _Client()
    files_tool.register(client, _context(tmp_path))

    callback = client.registered["files"]
    listed = callback(action="list", pattern="*.txt")
    read = callback(action="read", path="notes.txt", start_line=2, end_line=2)
    searched = callback(action="search", query="needle", pattern="*.txt")

    assert [item["path"] for item in listed["files"]] == ["notes.txt"]
    assert read["content"] == "needle here"
    assert searched["matches"] == [
        {"path": "notes.txt", "line": 2, "text": "needle here"}
    ]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"action": "read"}, "chemin"),
        ({"action": "search"}, "recherche"),
        ({"action": "delete"}, "Action files inconnue"),
    ],
)
def test_files_dispatch_rejects_missing_or_unknown_action_arguments(tmp_path, kwargs, message):
    with pytest.raises(ValueError, match=message):
        files_tool.files(_context=_context(tmp_path), **kwargs)


def test_files_schema_exposes_action_specific_required_fields(tmp_path):
    class _SchemaClient:
        def __init__(self):
            self.metadata = None

        def register_tool(self, name, callback, **kwargs):
            assert name == "files"
            self.metadata = kwargs

    client = _SchemaClient()
    files_tool.register(client, _context(tmp_path))
    parameters = client.metadata["parameters"]
    assert parameters["required"] == ["action"]
    rules = {
        item["if"]["properties"]["action"]["const"]: item["then"]["required"]
        for item in parameters["allOf"]
    }
    assert rules == {
        "read": ["action", "path"],
        "search": ["action", "query"],
    }
