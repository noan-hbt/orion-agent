"""Focused tests for hardened remote ToolManager installs."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest

import tool_manager
from tool_manager import ToolManager, ToolPackageError


def _package_zip(*, execute_marker: str | None = None) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "tool.toml",
            'id = "example.remote"\nname = "Remote"\nversion = "1.0.0"\n',
        )
        body = "def register(client):\n    return None\n"
        if execute_marker:
            body = f'from pathlib import Path\nPath(r"{execute_marker}").write_text("executed")\n' + body
        archive.writestr("tool.py", body)
    return payload.getvalue()


class _Response:
    def __init__(self, payload: bytes, *, content_length: int | None = None) -> None:
        self._stream = io.BytesIO(payload)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Opener:
    def __init__(self, response: _Response) -> None:
        self.response = response

    def open(self, *args, **kwargs):
        return self.response


def _fake_remote(monkeypatch, payload: bytes, *, content_length: int | None = None) -> None:
    monkeypatch.setattr(
        tool_manager.urllib.request,
        "build_opener",
        lambda *handlers: _Opener(_Response(payload, content_length=content_length)),
    )


def _manager(tmp_path: Path) -> ToolManager:
    return ToolManager(install_dir=tmp_path / "tools", root_dir=tmp_path)


def test_remote_install_rejects_http_before_network(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        tool_manager.urllib.request,
        "build_opener",
        lambda *args, **kwargs: pytest.fail("HTTP source must be rejected before network access"),
    )

    with pytest.raises(ToolPackageError, match="HTTPS"):
        _manager(tmp_path).install(
            "http://example.invalid/tool.zip",
            expected_sha256="0" * 64,
        )


def test_remote_install_requires_sha256_unless_explicitly_unverified(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        tool_manager.urllib.request,
        "build_opener",
        lambda *args, **kwargs: pytest.fail("missing digest must fail before download"),
    )

    with pytest.raises(ToolPackageError, match="SHA-256"):
        _manager(tmp_path).install("https://example.invalid/tool.zip")


def test_remote_install_verifies_digest_before_extraction_or_execution(monkeypatch, tmp_path: Path):
    marker = tmp_path / "executed.txt"
    payload = _package_zip(execute_marker=str(marker).replace("\\", "/"))
    _fake_remote(monkeypatch, payload)

    with pytest.raises(ToolPackageError, match="SHA-256 invalide"):
        _manager(tmp_path).install(
            "https://example.invalid/tool.zip",
            expected_sha256="0" * 64,
        )

    assert not marker.exists()
    assert not (tmp_path / "tools" / "example.remote").exists()


def test_remote_install_streams_with_strict_size_limit(monkeypatch, tmp_path: Path):
    payload = _package_zip()
    _fake_remote(monkeypatch, payload)

    with pytest.raises(ToolPackageError, match="trop volumineuse"):
        _manager(tmp_path).install(
            "https://example.invalid/tool.zip",
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            max_download_bytes=len(payload) - 1,
        )


def test_remote_install_accepts_verified_https_archive_without_executing_module(monkeypatch, tmp_path: Path):
    marker = tmp_path / "executed.txt"
    payload = _package_zip(execute_marker=str(marker).replace("\\", "/"))
    _fake_remote(monkeypatch, payload, content_length=len(payload))

    manifest = _manager(tmp_path).install(
        "https://example.invalid/tool.zip",
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )

    assert manifest.id == "example.remote"
    assert (tmp_path / "tools" / "example.remote" / "tool.py").is_file()
    assert not marker.exists()


def test_allow_unverified_is_explicit_opt_in(monkeypatch, tmp_path: Path):
    payload = _package_zip()
    _fake_remote(monkeypatch, payload)

    manifest = _manager(tmp_path).install(
        "https://example.invalid/tool.zip",
        allow_unverified=True,
    )

    assert manifest.id == "example.remote"


def test_https_redirect_handler_rejects_http_downgrade():
    handler = tool_manager._HttpsOnlyRedirectHandler()
    request = tool_manager.urllib.request.Request("https://example.invalid/tool.zip")

    with pytest.raises(ToolPackageError, match="rester en HTTPS"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "http://example.invalid/tool.zip",
        )


def test_local_directory_install_does_not_require_digest(tmp_path: Path):
    package = tmp_path / "source"
    package.mkdir()
    (package / "tool.toml").write_text(
        'id = "example.local"\nname = "Local"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    (package / "tool.py").write_text("def register(client):\n    return None\n", encoding="utf-8")

    manifest = _manager(tmp_path).install(package)

    assert manifest.id == "example.local"
