"""Catalogue et téléchargement de tools depuis un dépôt GitHub public."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping
from urllib.parse import quote

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from tool_manager import ToolManager, ToolManifest, ToolPackageError


@dataclass(frozen=True)
class GithubTool:
    manifest: ToolManifest
    path: str


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

    def __init__(self, repository: str, *, ref: str = "main", timeout: int = 20) -> None:
        self.repository = normalize_repository(repository)
        self.ref = ref.strip() or "main"
        self.timeout = max(1, int(timeout))
        self._tree: list[dict[str, Any]] | None = None
        self._tools: list[GithubTool] | None = None

    @property
    def source_label(self) -> str:
        return f"github://{self.repository}@{self.ref}"

    def _request(self, url: str) -> bytes:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "Orion-Toolbox/1.0",
        }
        token = os.getenv("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=self.timeout) as response:
                return response.read()
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
        self._tree = [item for item in tree if isinstance(item, Mapping)]
        return self._tree

    def discover(self) -> list[GithubTool]:
        if self._tools is not None:
            return list(self._tools)
        tools: list[GithubTool] = []
        for item in self._load_tree():
            path = str(item.get("path", ""))
            if item.get("type") != "blob" or Path(path).name != "tool.toml":
                continue
            raw_url = (
                f"https://raw.githubusercontent.com/{self.repository}/"
                f"{quote(self.ref, safe='/')}/{quote(path, safe='/')}"
            )
            try:
                data = tomllib.loads(self._request(raw_url).decode("utf-8"))
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
            except (KeyError, TypeError, ValueError, UnicodeDecodeError, ToolPackageError) as exc:
                raise ToolPackageError(f"Manifeste invalide dans GitHub : {path}") from exc
            package_path = path.rsplit("/", 1)[0] if "/" in path else ""
            tools.append(GithubTool(manifest, package_path))
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
        prefix = f"{tool.path}/" if tool.path else ""
        files = [
            str(item["path"])
            for item in self._load_tree()
            if item.get("type") == "blob" and str(item.get("path", "")).startswith(prefix)
        ]
        if not files:
            raise ToolPackageError(f"Aucun fichier trouvé pour {tool.manifest.id}.")
        with TemporaryDirectory(prefix="orion-github-tool-") as temporary_name:
            package_dir = Path(temporary_name)
            for path in files:
                relative = path[len(prefix):] if prefix else path
                destination = package_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                raw_url = (
                    f"https://raw.githubusercontent.com/{self.repository}/"
                    f"{quote(self.ref, safe='/')}/{quote(path, safe='/')}"
                )
                destination.write_bytes(self._request(raw_url))
            return manager.install(
                package_dir,
                force=force,
                source_record=f"{self.source_label}/{tool.path}" if tool.path else self.source_label,
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
