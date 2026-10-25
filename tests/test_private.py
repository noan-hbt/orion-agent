"""Files only Orion's user may read are created that way."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from orion.core.private import write_private


def test_a_private_file_is_never_readable_by_others(tmp_path: Path) -> None:
    old = os.umask(0)  # even with a permissive umask
    try:
        path = tmp_path / "key"
        write_private(path, "secret")
        assert path.stat().st_mode & 0o777 == 0o600 and path.read_text() == "secret"
        path.chmod(0o644)
        write_private(path, b"again")
        assert path.stat().st_mode & 0o777 == 0o600 and path.read_bytes() == b"again"
        with pytest.raises(FileExistsError):
            write_private(path, "other", exclusive=True)
        assert path.read_bytes() == b"again"
        assert [p.name for p in tmp_path.iterdir()] == ["key"]  # no temporary file left
    finally:
        os.umask(old)
