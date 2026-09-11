"""Offline regression tests for secure GitHub tool catalog downloads."""

from __future__ import annotations

import hashlib
import io
import json
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

from github_tools import GithubToolCatalog
from tool_manager import ToolManifest, ToolPackageError


def _blob_sha(content: bytes) -> str:
    return hashlib.sha1(f"blob {len(content)}\0".encode("ascii") + content).hexdigest()


class _Response(io.BytesIO):
    def __init__(self, content: bytes, url: str, *, content_length: int | None = None):
        super().__init__(content)
        self._url = url
        headers = Message()
        if content_length is not None:
            headers["Content-Length"] = str(content_length)
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def geturl(self):
        return self._url


class _Manager:
    def __init__(self) -> None:
        self.seen: Path | None = None

    def install(self, source, **kwargs):
        root = Path(source)
        assert (root / "tool.toml").is_file()
        assert (root / "tool.py").read_text(encoding="utf-8") == "VALUE = 1\n"
        self.seen = root
        return ToolManifest.from_file(root / "tool.toml")


def _manifest() -> bytes:
    return b'id = "example.secure"\nname = "Secure"\nversion = "1.0.0"\n'


def _tree(
    manifest: bytes,
    tool_py: bytes,
    *,
    path: str = "pkg/tool.py",
    include_sha: bool = True,
):
    entries = [
        {
            "path": "pkg/tool.toml",
            "type": "blob",
            "size": len(manifest),
            **({"sha": _blob_sha(manifest)} if include_sha else {}),
        },
        {
            "path": path,
            "type": "blob",
            "size": len(tool_py),
            **({"sha": _blob_sha(tool_py)} if include_sha else {}),
        },
    ]
    return entries


def _catalog_with_tree(entries):
    catalog = GithubToolCatalog("owner/repo")
    catalog._tree = [dict(item) for item in entries]
    return catalog


def test_request_rejects_http_and_https_to_http_downgrade(monkeypatch):
    catalog = GithubToolCatalog("owner/repo")
    with pytest.raises(ToolPackageError, match="HTTPS"):
        catalog._request("http://example.invalid/file")

    monkeypatch.setattr(
        catalog._opener,
        "open",
        lambda *args, **kwargs: _Response(b"x", "http://example.invalid/final"),
    )
    with pytest.raises(ToolPackageError, match="HTTPS"):
        catalog._request("https://example.invalid/file")


def test_request_enforces_streaming_size_limit(monkeypatch):
    catalog = GithubToolCatalog("owner/repo", max_response_bytes=4)
    monkeypatch.setattr(
        catalog._opener,
        "open",
        lambda *args, **kwargs: _Response(b"12345", "https://example.invalid/file"),
    )

    with pytest.raises(ToolPackageError, match="volumineux"):
        catalog._request("https://example.invalid/file")


def test_load_tree_rejects_path_traversal(monkeypatch):
    catalog = GithubToolCatalog("owner/repo")
    payload = json.dumps(
        {"tree": [{"path": "pkg/../evil.py", "type": "blob"}]}
    ).encode()
    monkeypatch.setattr(catalog, "_request", lambda *args, **kwargs: payload)

    with pytest.raises(ToolPackageError, match="suspect"):
        catalog._load_tree()


def test_install_rejects_missing_integrity_metadata():
    manifest = _manifest()
    tool_py = b"VALUE = 1\n"
    catalog = _catalog_with_tree(_tree(manifest, tool_py, include_sha=False))
    tool = SimpleNamespace(
        manifest=ToolManifest(id="example.secure", name="Secure", version="1.0.0"),
        path="pkg",
    )

    with pytest.raises(ToolPackageError, match="intégrité"):
        catalog.install(tool, _Manager())


def test_discovery_remains_available_without_manifest_blob_sha(monkeypatch):
    manifest = _manifest()
    catalog = GithubToolCatalog("owner/repo")
    catalog._tree = [{"path": "pkg/tool.toml", "type": "blob", "size": len(manifest)}]
    monkeypatch.setattr(catalog, "_request", lambda *args, **kwargs: manifest)

    tools = catalog.discover()

    assert [tool.manifest.id for tool in tools] == ["example.secure"]
    assert tools[0].manifest_verified is False


def test_verified_manifest_sha256_can_cover_blob_without_github_sha(monkeypatch):
    tool_py = b"VALUE = 1\n"
    digest = hashlib.sha256(tool_py).hexdigest()
    manifest = (
        b'id = "example.secure"\nname = "Secure"\nversion = "1.0.0"\n'
        + f'[integrity.files]\n"tool.py" = "sha256:{digest}"\n'.encode()
    )
    entries = _tree(manifest, tool_py)
    entries[1].pop("sha")
    catalog = _catalog_with_tree(entries)
    catalog._tree = None
    tree_payload = json.dumps({"tree": entries}).encode()

    def fake_request(url, **kwargs):
        if "git/trees/" in url:
            return tree_payload
        if url.endswith("tool.toml"):
            return manifest
        return tool_py

    monkeypatch.setattr(catalog, "_request", fake_request)
    selected = catalog.discover()[0]
    manager = _Manager()

    installed = catalog.install(selected, manager)

    assert installed.id == "example.secure"
    assert selected.manifest_verified is True


def test_install_rejects_tampered_blob(monkeypatch):
    manifest = _manifest()
    tool_py = b"VALUE = 1\n"
    catalog = _catalog_with_tree(_tree(manifest, tool_py))
    tool = SimpleNamespace(
        manifest=ToolManifest(id="example.secure", name="Secure", version="1.0.0"),
        path="pkg",
    )

    def fake_request(url, **kwargs):
        return manifest if url.endswith("tool.toml") else b"VALUE = 2\n"

    monkeypatch.setattr(catalog, "_request", fake_request)
    with pytest.raises(ToolPackageError, match="Intégrité"):
        catalog.install(tool, _Manager())


def test_install_rejects_oversize_file_before_download():
    manifest = _manifest()
    tool_py = b"VALUE = 1\n"
    entries = _tree(manifest, tool_py)
    entries[1]["size"] = 101
    catalog = _catalog_with_tree(entries)
    catalog.max_file_bytes = 100
    tool = SimpleNamespace(
        manifest=ToolManifest(id="example.secure", name="Secure", version="1.0.0"),
        path="pkg",
    )

    with pytest.raises(ToolPackageError, match="volumineux"):
        catalog.install(tool, _Manager())


def test_valid_install_verifies_all_blobs_before_manager_install(monkeypatch):
    manifest = _manifest()
    tool_py = b"VALUE = 1\n"
    catalog = _catalog_with_tree(_tree(manifest, tool_py))
    tool = SimpleNamespace(
        manifest=ToolManifest(id="example.secure", name="Secure", version="1.0.0"),
        path="pkg",
    )

    def fake_request(url, **kwargs):
        return manifest if url.endswith("tool.toml") else tool_py

    monkeypatch.setattr(catalog, "_request", fake_request)
    manager = _Manager()

    installed = catalog.install(tool, manager)

    assert installed.id == "example.secure"
