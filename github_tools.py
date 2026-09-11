"""Catalogue et téléchargement de tools depuis un dépôt GitHub public."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping
from urllib.parse import quote, urlparse

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from tool_manager import ToolManager, ToolManifest, ToolPackageError


@dataclass(frozen=True)
class GithubTool:
    manifest: ToolManifest
    path: str
    expected_sha256: tuple[tuple[str, str], ...] = ()
    manifest_verified: bool = False


def normalize_repository(value: str) -> str:
    value = value.strip().rstrip("/")
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if value.endswith(".git"):
        value = value[:-4]
    parts = value.split("/")
    if len(parts) != 2 or not all(parts):
        raise ToolPackageError("Le dépôt GitHub doit être au format owner/repository.")
    return "/".join(parts)


class GithubToolCatalog:
    """Explore un dépôt et installe les dossiers contenant un tool.toml."""

    DEFAULT_MAX_RESPONSE_BYTES = 10_000_000
    DEFAULT_MAX_FILE_BYTES = 5_000_000
    DEFAULT_MAX_PACKAGE_BYTES = 25_000_000

    class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if urlparse(newurl).scheme.lower() != "https":
                raise ToolPackageError("Redirection GitHub refusée : HTTPS est obligatoire.")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    def __init__(
        self,
        repository: str,
        *,
        ref: str = "main",
        timeout: int = 20,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_package_bytes: int = DEFAULT_MAX_PACKAGE_BYTES,
    ) -> None:
        self.repository = normalize_repository(repository)
        self.ref = ref.strip() or "main"
        self.timeout = max(1, int(timeout))
        self.max_response_bytes = max(1, int(max_response_bytes))
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.max_package_bytes = max(1, int(max_package_bytes))
        self._tree: list[dict[str, Any]] | None = None
        self._tools: list[GithubTool] | None = None
        self._opener = urllib.request.build_opener(self._HttpsOnlyRedirectHandler())

    @property
    def source_label(self) -> str:
        return f"github://{self.repository}@{self.ref}"

    @staticmethod
    def _validate_remote_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ToolPackageError("URL GitHub refusée : HTTPS est obligatoire.")

    @staticmethod
    def _safe_repo_path(value: str) -> str:
        raw = str(value)
        if not raw or "\\" in raw or "\x00" in raw:
            raise ToolPackageError(f"Chemin GitHub suspect refusé : {raw!r}")
        if raw.startswith("/") or posixpath.isabs(raw):
            raise ToolPackageError(f"Chemin GitHub absolu refusé : {raw}")
        parts = raw.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ToolPackageError(f"Chemin GitHub suspect refusé : {raw}")
        normalized = posixpath.normpath(raw)
        if normalized != raw or normalized.startswith("../"):
            raise ToolPackageError(f"Chemin GitHub suspect refusé : {raw}")
        return raw

    @staticmethod
    def _git_blob_sha(content: bytes) -> str:
        header = f"blob {len(content)}\0".encode("ascii")
        return hashlib.sha1(header + content).hexdigest()

    @staticmethod
    def _valid_hex_digest(value: str, length: int) -> bool:
        text = str(value).strip().lower()
        return len(text) == length and all(char in "0123456789abcdef" for char in text)

    def _manifest_integrity(self, data: Mapping[str, Any], package_path: str) -> tuple[tuple[str, str], ...]:
        integrity = data.get("integrity", {})
        if integrity in (None, {}):
            return ()
        if not isinstance(integrity, Mapping):
            raise ToolPackageError("integrity doit être un objet TOML.")
        files = integrity.get("files", {})
        if not isinstance(files, Mapping):
            raise ToolPackageError("integrity.files doit être un objet TOML.")
        result: list[tuple[str, str]] = []
        for raw_path, raw_digest in files.items():
            relative = self._safe_repo_path(str(raw_path))
            digest = str(raw_digest).strip().lower()
            if digest.startswith("sha256:"):
                digest = digest[len("sha256:"):]
            if not self._valid_hex_digest(digest, 64):
                raise ToolPackageError(f"Digest SHA-256 invalide pour {relative}.")
            # Integrity paths are relative to the tool package, not repository root.
            if package_path:
                self._safe_repo_path(f"{package_path}/{relative}")
            result.append((relative, digest))
        return tuple(result)

    def _request(self, url: str, *, max_bytes: int | None = None) -> bytes:
        self._validate_remote_url(url)
        limit = self.max_response_bytes if max_bytes is None else max(1, int(max_bytes))
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "Orion-Toolbox/1.0",
        }
        token = os.getenv("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with self._opener.open(urllib.request.Request(url, headers=headers), timeout=self.timeout) as response:
                final_url = str(getattr(response, "geturl", lambda: url)())
                self._validate_remote_url(final_url)
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        declared = int(content_length)
                    except ValueError:
                        declared = None
                    if declared is not None and declared > limit:
                        raise ToolPackageError(
                            f"Téléchargement GitHub trop volumineux : {declared} octets (limite {limit})."
                        )
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = response.read(min(64 * 1024, limit - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise ToolPackageError(
                            f"Téléchargement GitHub trop volumineux : limite {limit} octets dépassée."
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
        except urllib.error.HTTPError as exc:
            raise ToolPackageError(f"GitHub a répondu HTTP {exc.code} pour {url}.") from exc
        except urllib.error.URLError as exc:
            raise ToolPackageError(f"GitHub est inaccessible : {exc.reason}") from exc

    def _load_tree(self) -> list[dict[str, Any]]:
        if self._tree is not None:
            return self._tree
        url = (
            f"https://api.github.com/repos/{self.repository}/git/trees/"
            f"{quote(self.ref, safe='')}?recursive=1"
        )
        try:
            payload = json.loads(self._request(url).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ToolPackageError("Réponse GitHub invalide.") from exc
        tree = payload.get("tree") if isinstance(payload, Mapping) else None
        if not isinstance(tree, list):
            raise ToolPackageError("GitHub n'a pas renvoyé l'arborescence du dépôt.")
        validated: list[dict[str, Any]] = []
        for item in tree:
            if not isinstance(item, Mapping):
                continue
            copied = dict(item)
            if copied.get("path") is not None:
                copied["path"] = self._safe_repo_path(str(copied["path"]))
            validated.append(copied)
        self._tree = validated
        return self._tree

    def discover(self) -> list[GithubTool]:
        if self._tools is not None:
            return list(self._tools)
        tools: list[GithubTool] = []
        for item in self._load_tree():
            path = str(item.get("path", ""))
            if item.get("type") != "blob" or Path(path).name != "tool.toml":
                continue
            package_path = path.rsplit("/", 1)[0] if "/" in path else ""
            raw_url = (
                f"https://raw.githubusercontent.com/{self.repository}/"
                f"{quote(self.ref, safe='/')}/{quote(path, safe='/')}"
            )
            try:
                raw_manifest = self._request(raw_url, max_bytes=self.max_file_bytes)
                manifest_sha = str(item.get("sha", "")).strip().lower()
                manifest_verified = False
                if manifest_sha:
                    if not self._valid_hex_digest(manifest_sha, 40):
                        raise ToolPackageError(f"Blob SHA GitHub invalide pour {path}.")
                    if self._git_blob_sha(raw_manifest) != manifest_sha:
                        raise ToolPackageError(f"Intégrité GitHub invalide pour {path}.")
                    manifest_verified = True
                data = tomllib.loads(raw_manifest.decode("utf-8"))
                manifest = ToolManifest(
                    id=str(data["id"]),
                    name=str(data.get("name", data["id"])),
                    version=str(data["version"]),
                    entrypoint=str(data.get("entrypoint", "tool:register")),
                    description=str(data.get("description", "")),
                    api_version=int(data.get("api_version", 1)),
                    permissions=tuple(str(value) for value in data.get("permissions", [])),
                )
                manifest.validate()
                expected_sha256 = self._manifest_integrity(data, package_path)
            except (KeyError, TypeError, ValueError, UnicodeDecodeError, ToolPackageError) as exc:
                raise ToolPackageError(f"Manifeste invalide dans GitHub : {path}") from exc
            tools.append(
                GithubTool(
                    manifest,
                    package_path,
                    expected_sha256=expected_sha256,
                    manifest_verified=manifest_verified,
                )
            )
        self._tools = sorted(tools, key=lambda item: item.manifest.id.lower())
        return list(self._tools)

    def search(self, query: str = "") -> list[GithubTool]:
        needle = query.strip().lower()
        tools = self.discover()
        if not needle:
            return tools
        return [
            item
            for item in tools
            if needle in item.manifest.id.lower()
            or needle in item.manifest.name.lower()
            or needle in item.manifest.description.lower()
        ]

    def install(self, tool: GithubTool, manager: ToolManager, *, force: bool = False) -> ToolManifest:
        tool_path = self._safe_repo_path(tool.path) if tool.path else ""
        prefix = f"{tool.path}/" if tool.path else ""
        entries = [
            item
            for item in self._load_tree()
            if item.get("type") == "blob" and str(item.get("path", "")).startswith(prefix)
        ]
        if not entries:
            raise ToolPackageError(f"Aucun fichier trouvé pour {tool.manifest.id}.")
        expected_sha256 = dict(getattr(tool, "expected_sha256", ()) or ())
        manifest_verified = bool(getattr(tool, "manifest_verified", False))
        package_total = 0
        for item in entries:
            path = self._safe_repo_path(str(item.get("path", "")))
            relative = path[len(prefix):] if prefix else path
            self._safe_repo_path(relative)
            sha = str(item.get("sha", "")).strip().lower()
            has_git_sha = self._valid_hex_digest(sha, 40)
            has_trusted_sha256 = manifest_verified and relative in expected_sha256
            if not has_git_sha and not has_trusted_sha256:
                raise ToolPackageError(
                    f"Installation refusée pour {tool.manifest.id} : intégrité GitHub absente ou invalide pour {path}."
                )
            size = item.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ToolPackageError(
                    f"Installation refusée pour {tool.manifest.id} : taille GitHub absente ou invalide pour {path}."
                )
            if size > self.max_file_bytes:
                raise ToolPackageError(
                    f"Fichier GitHub trop volumineux : {path} ({size} octets, limite {self.max_file_bytes})."
                )
            package_total += size
            if package_total > self.max_package_bytes:
                raise ToolPackageError(
                    f"Package GitHub trop volumineux : limite {self.max_package_bytes} octets dépassée."
                )
        with TemporaryDirectory(prefix="orion-github-tool-") as temporary_name:
            package_dir = Path(temporary_name)
            downloaded_total = 0
            for item in entries:
                path = self._safe_repo_path(str(item["path"]))
                relative = path[len(prefix):] if prefix else path
                self._safe_repo_path(relative)
                destination = package_dir / relative
                resolved_destination = destination.resolve()
                resolved_root = package_dir.resolve()
                if resolved_destination != resolved_root and resolved_root not in resolved_destination.parents:
                    raise ToolPackageError(f"Chemin de package GitHub hors périmètre refusé : {path}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                raw_url = (
                    f"https://raw.githubusercontent.com/{self.repository}/"
                    f"{quote(self.ref, safe='/')}/{quote(path, safe='/')}"
                )
                expected_size = int(item["size"])
                content = self._request(raw_url, max_bytes=min(self.max_file_bytes, expected_size + 1))
                if len(content) != expected_size:
                    raise ToolPackageError(
                        f"Taille GitHub inattendue pour {path}: {len(content)} reçus, {expected_size} attendus."
                    )
                expected_sha = str(item.get("sha", "")).strip().lower()
                if self._valid_hex_digest(expected_sha, 40):
                    actual_sha = self._git_blob_sha(content)
                    if actual_sha != expected_sha:
                        raise ToolPackageError(
                            f"Intégrité GitHub invalide pour {path}: contenu différent du blob annoncé."
                        )
                else:
                    expected_digest = expected_sha256[relative]
                    actual_digest = hashlib.sha256(content).hexdigest()
                    if actual_digest != expected_digest:
                        raise ToolPackageError(
                            f"Intégrité SHA-256 invalide pour {path}: contenu différent du digest annoncé."
                        )
                downloaded_total += len(content)
                if downloaded_total > self.max_package_bytes:
                    raise ToolPackageError(
                        f"Package GitHub trop volumineux : limite {self.max_package_bytes} octets dépassée."
                    )
                destination.write_bytes(content)
            return manager.install(
                package_dir,
                force=force,
                source_record=f"{self.source_label}/{tool_path}" if tool_path else self.source_label,
            )


def install_github_source(source: str, manager: ToolManager, *, force: bool = True) -> ToolManifest:
    """Met à jour un tool dont la source est un identifiant github://."""
    if not source.startswith("github://"):
        raise ToolPackageError(f"Source GitHub invalide : {source}")
    value = source[len("github://"):]
    repository, separator_at, ref_and_path = value.partition("@")
    ref, separator, package_path = ref_and_path.partition("/")
    if not separator_at or not repository or not ref or not package_path:
        raise ToolPackageError(f"Source GitHub invalide : {source}")
    catalog = GithubToolCatalog(repository, ref=ref)
    selected = next((item for item in catalog.discover() if item.path == package_path), None)
    if selected is None:
        raise ToolPackageError(f"Tool introuvable dans la source GitHub : {source}")
    return catalog.install(selected, manager, force=force)


__all__ = ["GithubTool", "GithubToolCatalog", "install_github_source", "normalize_repository"]
