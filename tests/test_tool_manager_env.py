"""Focused tests for ToolManager dotenv secret persistence."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from dotenv import dotenv_values

from tool_manager import ToolManager


def test_write_env_values_round_trips_special_secret_values(tmp_path: Path):
    env_path = tmp_path / ".env"
    secrets = {
        "SPACE_SECRET": "  leading and trailing spaces  ",
        "HASH_SECRET": "prefix#not-a-comment",
        "EQUAL_SECRET": "a=b=c=d",
        "QUOTE_SECRET": "single ' and double \" quotes",
        "BACKSLASH_SECRET": r"C:\path\\server\share\trailing\\",
        "MULTILINE_SECRET": "line one\nline # two=still-value\nline 'three' \\ tail",
    }

    ToolManager._write_env_values(env_path, secrets)

    loaded = dotenv_values(env_path)
    for key, value in secrets.items():
        assert loaded[key] == value


def test_write_env_values_preserves_existing_multiline_values_and_comments(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "KEEP='first line\nsecond # line=value'\n# keep this comment\nTARGET='old'\n",
        encoding="utf-8",
    )

    ToolManager._write_env_values(env_path, {"TARGET": "new # value=with\\slashes"})

    loaded = dotenv_values(env_path)
    assert loaded["KEEP"] == "first line\nsecond # line=value"
    assert loaded["TARGET"] == "new # value=with\\slashes"
    assert "# keep this comment" in env_path.read_text(encoding="utf-8")


def test_write_env_values_replaces_atomically_and_leaves_original_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    env_path = tmp_path / ".env"
    original = "EXISTING='keep me'\n"
    env_path.write_text(original, encoding="utf-8")

    import tool_manager

    def fail_replace(*args, **kwargs):
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(tool_manager.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated atomic replace failure"):
        ToolManager._write_env_values(env_path, {"SECRET": "replacement"})

    assert env_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("..env.*.tmp")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX file mode semantics only")
def test_write_env_values_best_effort_restricts_posix_permissions(tmp_path: Path):
    env_path = tmp_path / ".env"

    ToolManager._write_env_values(env_path, {"SECRET": "sensitive"})

    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
