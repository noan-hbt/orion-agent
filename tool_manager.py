"""Chargement et gestion des extensions Python d'Orion.

Un tool installé est un petit paquet autonome contenant un ``tool.toml`` et
un module Python exposant une fonction ``register``. Le paquet enregistre ses
tools sur le client OpenRouter existant ; le runtime n'a donc pas besoin de
connaître son implémentation.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import tempfile
import urllib.request
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from importlib import util as importlib_util
from pathlib import Path
from typing import Any, Mapping

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


MANIFEST_NAME = "tool.toml"
TOOL_API_VERSION = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


class ToolPackageError(RuntimeError):
    """Le paquet d'extension est invalide ou impossible à charger."""


@dataclass(frozen=True)
class ToolManifest:
    """Métadonnées publiques et point d'entrée d'un paquet Orion."""

    id: str
    name: str
    version: str
    entrypoint: str = "tool:register"
    description: str = ""
    api_version: int = TOOL_API_VERSION
    permissions: tuple[str, ...] = ()

    @classmethod
    def from_file(cls, path: str | Path) -> ToolManifest:
        manifest_path = Path(path)
        try:
            with manifest_path.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, ValueError) as exc:
            raise ToolPackageError(f"Manifeste illisible : {manifest_path}") from exc
        if not isinstance(data, Mapping):
            raise ToolPackageError("Le manifeste doit être un objet TOML.")
        try:
            manifest = cls(
                id=str(data["id"]),
                name=str(data.get("name", data["id"])),
                version=str(data["version"]),
                entrypoint=str(data.get("entrypoint", "tool:register")),
                description=str(data.get("description", "")),
                api_version=int(data.get("api_version", TOOL_API_VERSION)),
                permissions=tuple(str(item) for item in data.get("permissions", [])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolPackageError(
                f"Le manifeste doit contenir id et version : {manifest_path}"
            ) from exc
        manifest.validate()
        return manifest

    def validate(self) -> None:
        if not _SAFE_ID.fullmatch(self.id):
            raise ToolPackageError(
                "L'identifiant d'un tool doit contenir uniquement des lettres, "
                "chiffres, '.', '-' ou '_'."
            )
        if not self.name.strip() or not self.version.strip():
            raise ToolPackageError("Un tool doit avoir un nom et une version.")
        if self.api_version != TOOL_API_VERSION:
            raise ToolPackageError(
                f"API de tool incompatible : {self.api_version}; "
                f"version supportée : {TOOL_API_VERSION}."
            )
        if ":" not in self.entrypoint:
            raise ToolPackageError("entrypoint doit avoir le format module:fonction.")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["permissions"] = list(self.permissions)
        return result


@dataclass(frozen=True)
class ToolContext:
    """Contexte stable fourni à une extension lors de son enregistrement."""

    root_dir: Path
    data_dir: Path
    install_dir: Path
    config: Mapping[str, Any] = field(default_factory=dict)


class ToolManager:
    """Installe, découvre et charge les paquets Orion."""

    def __init__(
        self,
        install_dir: str | Path = "tools",
        *,
        state_path: str | Path | None = None,
        root_dir: str | Path | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        self.install_dir = Path(install_dir).resolve()
        self.root_dir = Path(root_dir or self.install_dir.parent).resolve()
        self.data_dir = self.root_dir / "data"
        self.state_path = Path(state_path or self.data_dir / "installed_tools.json").resolve()
        self.config = dict(config or {})

    def _manifest_path(self, package_dir: Path) -> Path:
        path = package_dir / MANIFEST_NAME
        if not path.is_file():
            raise ToolPackageError(f"Manifest absent : {path}")
        return path

    def _find_package_root(self, root: Path) -> Path:
        if (root / MANIFEST_NAME).is_file():
            return root
        candidates = [path.parent for path in root.rglob(MANIFEST_NAME)]
        if len(candidates) != 1:
            raise ToolPackageError(
                "L'archive doit contenir exactement un tool.toml à sa racine "
                "ou dans un unique sous-dossier."
            )
        return candidates[0]

    def _read_state(self) -> dict[str, dict[str, Any]]:
        if not self.state_path.is_file():
            return {}
        try:
            with self.state_path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, ValueError) as exc:
            raise ToolPackageError(f"État des tools illisible : {self.state_path}") from exc
        return dict(value) if isinstance(value, Mapping) else {}

    def _write_state(self, state: Mapping[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(f".{self.state_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, self.state_path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
        destination = destination.resolve()
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if target != destination and destination not in target.parents:
                raise ToolPackageError("Archive refusée : chemin de fichier dangereux.")
        archive.extractall(destination)

    def _source_root(self, source: str | Path, temporary_root: Path) -> tuple[Path, str | None]:
        source_text = str(source)
        if source_text.startswith(("https://", "http://")):
            archive_path = temporary_root / "package.zip"
            try:
                urllib.request.urlretrieve(source_text, archive_path)
            except OSError as exc:
                raise ToolPackageError(f"Téléchargement impossible : {source_text}") from exc
            with zipfile.ZipFile(archive_path) as archive:
                extracted = temporary_root / "extracted"
                extracted.mkdir()
                self._safe_extract(archive, extracted)
            return self._find_package_root(extracted), source_text

        local_path = Path(source).expanduser().resolve()
        if local_path.is_dir():
            return self._find_package_root(local_path), str(local_path)
        if local_path.is_file() and local_path.suffix.lower() == ".zip":
            extracted = temporary_root / "extracted"
            extracted.mkdir()
            with zipfile.ZipFile(local_path) as archive:
                self._safe_extract(archive, extracted)
            return self._find_package_root(extracted), str(local_path)
        raise ToolPackageError(f"Source de tool introuvable : {source}")

    def install(
        self,
        source: str | Path,
        *,
        force: bool = False,
        source_record: str | None = None,
    ) -> ToolManifest:
        """Installe un dossier, une archive zip ou une URL d'archive."""
        self.install_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="orion-tool-") as temporary_name:
            package_root, detected_source = self._source_root(source, Path(temporary_name))
            manifest = ToolManifest.from_file(self._manifest_path(package_root))
            destination = self.install_dir / manifest.id
            if destination.exists() and not force:
                raise ToolPackageError(
                    f"Le tool {manifest.id} est déjà installé. Utilise --force pour le remplacer."
                )
            staging = self.install_dir / f".{manifest.id}.install-{uuid.uuid4().hex}"
            shutil.copytree(package_root, staging)
            backup: Path | None = None
            try:
                if destination.exists():
                    backup = self.install_dir / f".{manifest.id}.backup-{uuid.uuid4().hex}"
                    destination.rename(backup)
                staging.rename(destination)
            except Exception:
                if destination.exists() and not staging.exists():
                    shutil.rmtree(destination)
                if backup is not None and backup.exists():
                    backup.rename(destination)
                raise
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
                if backup is not None and backup.exists():
                    shutil.rmtree(backup)

            state = self._read_state()
            state[manifest.id] = {
                **manifest.to_dict(),
                "source": source_record or detected_source,
                "installed_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write_state(state)
            return manifest

    def remove(self, tool_id: str) -> None:
        """Désinstalle un tool installé."""
        if not _SAFE_ID.fullmatch(tool_id):
            raise ToolPackageError(f"Identifiant de tool invalide : {tool_id}")
        destination = self.install_dir / tool_id
        if not destination.is_dir():
            raise ToolPackageError(f"Tool non installé : {tool_id}")
        shutil.rmtree(destination)
        state = self._read_state()
        state.pop(tool_id, None)
        self._write_state(state)

    def installed(self) -> list[tuple[ToolManifest, Path]]:
        """Retourne les paquets installés valides, triés par identifiant."""
        if not self.install_dir.is_dir():
            return []
        result: list[tuple[ToolManifest, Path]] = []
        for child in sorted(self.install_dir.iterdir(), key=lambda item: item.name.lower()):
            if child.is_dir() and not child.name.startswith(".") and (child / MANIFEST_NAME).is_file():
                result.append((ToolManifest.from_file(child / MANIFEST_NAME), child))
        return result

    def load_all(self, client: Any) -> list[ToolManifest]:
        """Charge les tools activés sur un client OpenRouter."""
        enabled = {str(item) for item in self.config.get("enabled", []) if str(item).strip()}
        disabled = {str(item) for item in self.config.get("disabled", []) if str(item).strip()}
        load_all = not enabled
        context = ToolContext(
            root_dir=self.root_dir,
            data_dir=self.data_dir,
            install_dir=self.install_dir,
            config=self.config,
        )
        loaded: list[ToolManifest] = []
        for manifest, package_dir in self.installed():
            if manifest.id in disabled or (not load_all and manifest.id not in enabled):
                continue
            self._load_one(client, manifest, package_dir, context)
            loaded.append(manifest)
        return loaded

    @staticmethod
    def _load_one(client: Any, manifest: ToolManifest, package_dir: Path, context: ToolContext) -> None:
        module_name, function_name = manifest.entrypoint.split(":", 1)
        module_path = package_dir / (module_name.replace(".", os.sep) + ".py")
        if not module_path.is_file():
            raise ToolPackageError(f"Point d'entrée introuvable pour {manifest.id} : {module_path}")
        import_name = f"orion_plugin_{manifest.id.replace('.', '_').replace('-', '_')}"
        spec = importlib_util.spec_from_file_location(import_name, module_path)
        if spec is None or spec.loader is None:
            raise ToolPackageError(f"Module du tool impossible à charger : {module_path}")
        module = importlib_util.module_from_spec(spec)
        spec.loader.exec_module(module)
        register = getattr(module, function_name, None)
        if not callable(register):
            raise ToolPackageError(f"Fonction d'enregistrement absente : {manifest.entrypoint}")
        try:
            signature = inspect.signature(register)
            positional = [
                parameter for parameter in signature.parameters.values()
                if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
            ]
            if len(positional) >= 2:
                register(client, context)
            else:
                register(client)
        except (TypeError, ValueError) as exc:
            raise ToolPackageError(f"Signature invalide pour {manifest.id} : {manifest.entrypoint}") from exc


__all__ = [
    "MANIFEST_NAME",
    "TOOL_API_VERSION",
    "ToolContext",
    "ToolManager",
    "ToolManifest",
    "ToolPackageError",
]
