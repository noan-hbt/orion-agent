"""Chargement et gestion des extensions Python d'Orion.

Un tool installé est un petit paquet autonome contenant un ``tool.toml`` et
un module Python exposant une fonction ``register``. Le paquet enregistre ses
tools sur le client OpenRouter existant ; le runtime n'a donc pas besoin de
connaître son implémentation.
"""

from __future__ import annotations

import inspect
import getpass
import hashlib
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
from urllib.parse import urlsplit

from tool_policy import (
    ToolClassification,
    ToolPackagePolicy,
    ToolPolicy,
    ToolPolicyDecision,
    ToolPolicyError,
)

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


MANIFEST_NAME = "tool.toml"
TOOL_API_VERSION = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_SAFE_CONFIG_SECTION = re.compile(r"^[A-Za-z0-9_.-]+$")
_GUIDANCE_SUMMARY_MAX = 500
_GUIDANCE_INSTRUCTIONS_MAX = 4000
_GUIDANCE_CONSTRAINT_MAX = 500
_GUIDANCE_CONSTRAINTS_MAX = 20
DEFAULT_REMOTE_TOOL_MAX_BYTES = 25 * 1024 * 1024


def _dotenv_value(value: str) -> str:
    """Serialize one value so python-dotenv parses it without data loss."""

    value = str(value)
    if (
        value
        and value == value.strip()
        and "\n" not in value
        and "\r" not in value
        and not re.search(r"\s+#", value)
    ):
        # Unquoted dotenv values preserve quotes, equals signs and literal
        # backslashes verbatim.  Prefer this form where possible, notably for
        # values ending in a backslash (ambiguous at a quoted closing delimiter
        # in python-dotenv's parser).
        return value

    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    for character, replacement in (
        ("\a", "\\a"),
        ("\b", "\\b"),
        ("\f", "\\f"),
        ("\n", "\\n"),
        ("\r", "\\r"),
        ("\t", "\\t"),
        ("\v", "\\v"),
    ):
        escaped = escaped.replace(character, replacement)
    return f'"{escaped}"'


class ToolPackageError(RuntimeError):
    """Le paquet d'extension est invalide ou impossible à charger."""


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects that would downgrade a remote tool fetch to plaintext."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(str(newurl)).scheme.lower() != "https":
            raise ToolPackageError("Redirection distante refusée : les tools doivent rester en HTTPS.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class ToolGuidance:
    """Optional, bounded operational instructions declared by a tool."""

    summary: str = ""
    instructions: str = ""
    constraints: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> ToolGuidance:
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise TypeError("guidance must be a TOML object")
        summary = value.get("summary", "")
        instructions = value.get("instructions", "")
        constraints = value.get("constraints", [])
        if not isinstance(summary, str) or not isinstance(instructions, str):
            raise TypeError("guidance.summary and guidance.instructions must be strings")
        if not isinstance(constraints, list) or not all(isinstance(item, str) for item in constraints):
            raise TypeError("guidance.constraints must be a list of strings")
        result = cls(summary.strip(), instructions.strip(), tuple(item.strip() for item in constraints))
        result.validate()
        return result

    def validate(self) -> None:
        if len(self.summary) > _GUIDANCE_SUMMARY_MAX:
            raise ToolPackageError(f"guidance.summary is limited to {_GUIDANCE_SUMMARY_MAX} characters")
        if len(self.instructions) > _GUIDANCE_INSTRUCTIONS_MAX:
            raise ToolPackageError(f"guidance.instructions is limited to {_GUIDANCE_INSTRUCTIONS_MAX} characters")
        if len(self.constraints) > _GUIDANCE_CONSTRAINTS_MAX:
            raise ToolPackageError(f"guidance.constraints is limited to {_GUIDANCE_CONSTRAINTS_MAX} items")
        if any(len(item) > _GUIDANCE_CONSTRAINT_MAX for item in self.constraints):
            raise ToolPackageError(f"each guidance constraint is limited to {_GUIDANCE_CONSTRAINT_MAX} characters")

    def to_dict(self) -> dict[str, Any]:
        return {"summary": self.summary, "instructions": self.instructions, "constraints": list(self.constraints)}


@dataclass(frozen=True)
class ToolManifest:
    """Métadonnées publiques et point d'entrée d'un paquet Orion."""

    id: str
    name: str
    version: str
    kind: str = "tool"
    entrypoint: str = "tool:register"
    description: str = ""
    api_version: int = TOOL_API_VERSION
    permissions: tuple[str, ...] = ()
    configuration: dict[str, Any] = field(default_factory=dict)
    guidance: ToolGuidance = field(default_factory=ToolGuidance)
    policy: ToolPackagePolicy = field(default_factory=ToolPackagePolicy)

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
            configuration = data.get("configuration", {})
            if not isinstance(configuration, Mapping):
                raise TypeError("configuration doit être un objet TOML")
            kind = str(data.get("kind", "tool")).strip().lower()
            manifest = cls(
                id=str(data["id"]),
                name=str(data.get("name", data["id"])),
                version=str(data["version"]),
                kind=kind,
                entrypoint=str(
                    data.get("entrypoint", "tool:register" if kind == "tool" else "")
                ),
                description=str(data.get("description", "")),
                api_version=int(data.get("api_version", TOOL_API_VERSION)),
                permissions=tuple(str(item) for item in data.get("permissions", [])),
                configuration=dict(configuration),
                guidance=ToolGuidance.from_mapping(data.get("guidance")),
                policy=ToolPackagePolicy.from_mapping(data.get("policy")),
            )
        except ToolPolicyError as exc:
            raise ToolPackageError(f"Politique de tool invalide dans {manifest_path}: {exc}") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolPackageError(
                f"Le manifeste doit contenir id et version : {manifest_path}"
            ) from exc
        manifest.validate()
        return manifest

    def validate(self) -> None:
        if not isinstance(self.guidance, ToolGuidance):
            raise ToolPackageError("guidance must be a ToolGuidance object")
        if not isinstance(self.policy, ToolPackagePolicy):
            raise ToolPackageError("policy must be a ToolPackagePolicy object")
        self.guidance.validate()
        if not _SAFE_ID.fullmatch(self.id):
            raise ToolPackageError(
                "L'identifiant d'un tool doit contenir uniquement des lettres, "
                "chiffres, '.', '-' ou '_'."
            )
        if not self.name.strip() or not self.version.strip():
            raise ToolPackageError("Un tool doit avoir un nom et une version.")
        if self.kind not in {"tool", "module"}:
            raise ToolPackageError("kind doit valoir 'tool' ou 'module'.")
        if self.api_version != TOOL_API_VERSION:
            raise ToolPackageError(
                f"API de tool incompatible : {self.api_version}; "
                f"version supportée : {TOOL_API_VERSION}."
            )
        if self.kind == "tool" and ":" not in self.entrypoint:
            raise ToolPackageError("entrypoint doit avoir le format module:fonction.")
        section = str(self.configuration.get("section", self.id.rsplit(".", 1)[-1]))
        if not _SAFE_CONFIG_SECTION.fullmatch(section):
            raise ToolPackageError("configuration.section contient des caractères invalides.")
        table = self.configuration.get("table")
        if table is not None:
            if self.kind != "module":
                raise ToolPackageError("configuration.table est réservé aux modules Orion.")
            if not isinstance(table, str) or not _SAFE_CONFIG_SECTION.fullmatch(table):
                raise ToolPackageError("configuration.table contient des caractères invalides.")
        fields = self.configuration.get("fields", [])
        if not isinstance(fields, list):
            raise ToolPackageError("configuration.fields doit être une liste.")
        seen: set[str] = set()
        allowed_types = {"string", "secret", "choice", "integer", "number", "boolean"}
        for item in fields:
            if not isinstance(item, Mapping) or not str(item.get("key", "")).strip():
                raise ToolPackageError("Chaque champ de configuration doit avoir une clé.")
            key = str(item["key"])
            if not _SAFE_CONFIG_SECTION.fullmatch(key) or key in seen:
                raise ToolPackageError(f"Clé de configuration invalide ou dupliquée : {key}")
            seen.add(key)
            kind = str(item.get("type", "string")).lower()
            if kind not in allowed_types:
                raise ToolPackageError(f"Type de configuration inconnu : {kind}")
            if kind == "choice" and not isinstance(item.get("choices", []), list):
                raise ToolPackageError(f"Les choix de configuration sont invalides : {key}")
            if kind == "secret" and item.get("env") is not None:
                env_name = str(item["env"])
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
                    raise ToolPackageError(f"Nom de variable secret invalide : {env_name}")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["permissions"] = list(self.permissions)
        result["guidance"] = self.guidance.to_dict()
        result["policy"] = self.policy.to_dict()
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
        bundled_dir: str | Path | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        self.install_dir = Path(install_dir).resolve()
        self.root_dir = Path(root_dir or self.install_dir.parent).resolve()
        self.bundled_dir = Path(bundled_dir).resolve() if bundled_dir is not None else None
        self.data_dir = self.root_dir / "data"
        self.state_path = Path(state_path or self.data_dir / "installed_tools.json").resolve()
        self.config = dict(config or {})
        self._loaded_guidance: dict[str, ToolGuidance] = {}
        try:
            self._operator_policy = ToolPolicy.from_config(self.config.get("policy"))
        except ToolPolicyError as exc:
            raise ToolPackageError(str(exc)) from exc
        self._tool_policy = self._operator_policy.copy()
        self._loaded_policy_tools: set[str] = set()

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

    @staticmethod
    def _validated_sha256(value: str | None) -> str | None:
        if value is None:
            return None
        digest = str(value).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ToolPackageError("Le SHA-256 attendu doit contenir exactement 64 caractères hexadécimaux.")
        return digest

    @staticmethod
    def _download_remote_archive(
        source_url: str,
        destination: Path,
        *,
        expected_sha256: str | None,
        allow_unverified: bool,
        max_download_bytes: int,
    ) -> str:
        if urlsplit(source_url).scheme.lower() != "https":
            raise ToolPackageError("Les tools distants doivent utiliser HTTPS; HTTP est refusé.")
        expected = ToolManager._validated_sha256(expected_sha256)
        if expected is None and not allow_unverified:
            raise ToolPackageError(
                "Une URL distante exige un SHA-256 attendu. "
                "L'opt-in allow_unverified=True désactive cette vérification et est dangereux."
            )
        limit = int(max_download_bytes)
        if limit < 1:
            raise ToolPackageError("La limite de téléchargement distant doit être positive.")

        request = urllib.request.Request(
            source_url,
            headers={"User-Agent": "Orion-ToolManager/1.0", "Accept": "application/zip,*/*;q=0.5"},
        )
        digest = hashlib.sha256()
        total = 0
        try:
            opener = urllib.request.build_opener(_HttpsOnlyRedirectHandler())
            with opener.open(request, timeout=30) as response, destination.open("wb") as handle:
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        declared = int(content_length)
                    except ValueError:
                        declared = None
                    if declared is not None and declared > limit:
                        raise ToolPackageError(
                            f"Archive distante trop volumineuse ({declared} octets; limite {limit})."
                        )
                while True:
                    chunk = response.read(min(64 * 1024, limit + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise ToolPackageError(
                            f"Archive distante trop volumineuse (limite {limit} octets)."
                        )
                    digest.update(chunk)
                    handle.write(chunk)
        except ToolPackageError:
            raise
        except (OSError, ValueError) as exc:
            raise ToolPackageError(f"Téléchargement impossible : {source_url}") from exc

        actual = digest.hexdigest()
        if expected is not None and actual != expected:
            try:
                destination.unlink()
            except OSError:
                pass
            raise ToolPackageError(
                f"SHA-256 invalide pour le tool distant: attendu {expected}, reçu {actual}."
            )
        return actual

    def _source_root(
        self,
        source: str | Path,
        temporary_root: Path,
        *,
        expected_sha256: str | None = None,
        allow_unverified: bool = False,
        max_download_bytes: int = DEFAULT_REMOTE_TOOL_MAX_BYTES,
    ) -> tuple[Path, str | None]:
        source_text = str(source)
        if urlsplit(source_text).scheme.lower() in {"https", "http"}:
            archive_path = temporary_root / "package.zip"
            self._download_remote_archive(
                source_text,
                archive_path,
                expected_sha256=expected_sha256,
                allow_unverified=allow_unverified,
                max_download_bytes=max_download_bytes,
            )
            try:
                with zipfile.ZipFile(archive_path) as archive:
                    extracted = temporary_root / "extracted"
                    extracted.mkdir()
                    self._safe_extract(archive, extracted)
            except zipfile.BadZipFile as exc:
                raise ToolPackageError("L'archive distante n'est pas un ZIP valide.") from exc
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
        expected_sha256: str | None = None,
        allow_unverified: bool = False,
        max_download_bytes: int = DEFAULT_REMOTE_TOOL_MAX_BYTES,
    ) -> ToolManifest:
        """Installe un dossier, une archive zip ou une URL d'archive."""
        self.install_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="orion-tool-") as temporary_name:
            package_root, detected_source = self._source_root(
                source,
                Path(temporary_name),
                expected_sha256=expected_sha256,
                allow_unverified=allow_unverified,
                max_download_bytes=max_download_bytes,
            )
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

    @staticmethod
    def _toml_value(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list):
            return json.dumps(value, ensure_ascii=False)
        return json.dumps(str(value), ensure_ascii=False)

    @staticmethod
    def _read_env(path: Path) -> dict[str, str]:
        if not path.is_file():
            return {}
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
            if match:
                values[match.group(1)] = match.group(2)
        return values

    @staticmethod
    def _write_env_values(path: Path, values: Mapping[str, str]) -> None:
        if not values:
            return
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        newline = "\r\n" if "\r\n" in existing else "\n"
        lines = existing.splitlines()
        for raw_key, raw_value in values.items():
            key = str(raw_key)
            serialized = _dotenv_value(str(raw_value))
            matcher = re.compile(rf"^(?P<export>\s*export\s+)?{re.escape(key)}\s*=")
            for index, line in enumerate(lines):
                match = matcher.match(line)
                if match:
                    prefix = "export " if match.group("export") else ""
                    lines[index] = f"{prefix}{key}={serialized}"
                    break
            else:
                if lines and lines[-1].strip():
                    lines.append("")
                lines.append(f"{key}={serialized}")

        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(newline.join(lines) + newline)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                try:
                    os.chmod(temporary_name, 0o600)
                except OSError:
                    pass
            os.replace(temporary_name, path)
            if os.name != "nt":
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
        except BaseException:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise

    @staticmethod
    def _update_toml_table(path: Path, table: str, values: Mapping[str, Any]) -> None:
        """Met à jour une table TOML sans réécrire ni trier toute la configuration."""
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        newline = "\r\n" if "\r\n" in text else "\n"
        lines = text.splitlines()
        header = f"[{table}]"
        start = next((index for index, line in enumerate(lines) if line.strip() == header), None)
        if start is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(header)
            start = len(lines) - 1
        end = next(
            (index for index in range(start + 1, len(lines)) if lines[index].lstrip().startswith("[")),
            len(lines),
        )
        for key, value in values.items():
            pattern = re.compile(rf"^(\s*){re.escape(key)}\s*=")
            existing = next(
                (index for index in range(start + 1, end) if pattern.match(lines[index])),
                None,
            )
            replacement = f"{key} = {ToolManager._toml_value(value)}"
            if existing is not None:
                lines[existing] = replacement
            else:
                lines.insert(end, replacement)
                end += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(newline.join(lines) + newline)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise

    @staticmethod
    def _update_toml_section(path: Path, section: str, values: Mapping[str, Any]) -> None:
        ToolManager._update_toml_table(path, f"tools.{section}", values)

    def set_package_enabled(
        self,
        tool_id: str,
        enabled: bool,
        *,
        config_path: str | Path | None = None,
    ) -> None:
        """Persiste l'autorisation explicite d'import d'un package installé.

        L'installation physique et l'autorisation d'exécuter le Python d'un
        package restent deux opérations distinctes. Cette méthode représente
        précisément la seconde étape et maintient ``enabled``/``disabled``
        mutuellement exclusifs.
        """
        tool_id = str(tool_id).strip()
        if not _SAFE_ID.fullmatch(tool_id):
            raise ToolPackageError(f"Identifiant de tool invalide : {tool_id}")
        known = {
            manifest.id
            for manifest, _package_dir in (*self.bundled(), *self.installed())
        }
        if tool_id not in known:
            raise ToolPackageError(f"Tool non installé : {tool_id}")

        target_config = Path(config_path or self.root_dir / "orion.toml").resolve()
        if not target_config.is_file():
            raise ToolPackageError(f"Configuration Orion introuvable : {target_config}")

        enabled_ids = [
            str(item) for item in self.config.get("enabled", []) if str(item).strip()
        ]
        disabled_ids = [
            str(item) for item in self.config.get("disabled", []) if str(item).strip()
        ]
        if enabled:
            if tool_id not in enabled_ids:
                enabled_ids.append(tool_id)
            disabled_ids = [item for item in disabled_ids if item != tool_id]
        else:
            enabled_ids = [item for item in enabled_ids if item != tool_id]
            if tool_id not in disabled_ids:
                disabled_ids.append(tool_id)

        self._update_toml_table(
            target_config,
            "tools",
            {"enabled": enabled_ids, "disabled": disabled_ids},
        )
        self.config["enabled"] = enabled_ids
        self.config["disabled"] = disabled_ids

    def available(self) -> list[tuple[ToolManifest, Path]]:
        """Return bundled and user-installed packages, with user overrides winning."""

        packages = {manifest.id: (manifest, package_dir) for manifest, package_dir in self.bundled()}
        packages.update(
            {manifest.id: (manifest, package_dir) for manifest, package_dir in self.installed()}
        )
        return [packages[tool_id] for tool_id in sorted(packages)]

    def package_enabled(self, tool_id: str) -> bool:
        """Return the operator's effective explicit activation state for a package."""

        tool_id = str(tool_id).strip()
        enabled = {str(item) for item in self.config.get("enabled", [])}
        disabled = {str(item) for item in self.config.get("disabled", [])}
        return tool_id in enabled and tool_id not in disabled

    def find(self, tool_id: str) -> ToolManifest | None:
        """Find an available package manifest without importing its Python code."""

        tool_id = str(tool_id).strip()
        return next((manifest for manifest, _ in self.available() if manifest.id == tool_id), None)

    def _is_bundled_manifest(self, manifest: ToolManifest) -> bool:
        return any(candidate == manifest for candidate, _ in self.bundled())

    def configure(
        self,
        manifest: ToolManifest,
        *,
        config_path: str | Path | None = None,
        env_path: str | Path | None = None,
        input_fn: Any = input,
        secret_fn: Any = getpass.getpass,
    ) -> bool:
        """Demande les réglages déclarés par un tool et les persiste.

        Les valeurs normales vont dans ``[tools.<section>]``. Les champs
        ``secret`` sont écrits uniquement dans ``.env`` ; seule leur variable
        d'environnement éventuelle est conservée dans TOML.
        """
        configuration = manifest.configuration
        fields = configuration.get("fields", [])
        if not fields:
            return False
        section = str(configuration.get("section", manifest.id.rsplit(".", 1)[-1]))
        table = configuration.get("table")
        if table is not None and not self._is_bundled_manifest(manifest):
            raise ToolPackageError(
                "Seuls les modules bundled Orion peuvent configurer une table core."
            )
        target_config = Path(config_path or self.root_dir / "orion.toml").resolve()
        target_env = Path(env_path or self.root_dir / ".env").resolve()
        core_settings = self.config.get("_core", {})
        if table is not None and isinstance(core_settings, Mapping):
            current = core_settings.get(str(table), {})
        else:
            current = self.config.get(section, {})
        current = dict(current) if isinstance(current, Mapping) else {}
        env_values = self._read_env(target_env)
        normal_values: dict[str, Any] = {}
        secret_values: dict[str, str] = {}

        for raw_field in fields:
            field_config = dict(raw_field)
            key = str(field_config["key"])
            kind = str(field_config.get("type", "string")).lower()
            label = str(field_config.get("label", key))
            config_key = str(field_config.get("config_key", key))
            default = field_config.get("default")
            existing = current.get(config_key, default)
            if kind == "secret":
                env_name = str(field_config.get("env", config_key)).strip()
                existing_secret = env_values.get(env_name) or os.getenv(env_name, "")
                hint = "[déjà définie]" if existing_secret else "[optionnelle]"
                value = str(secret_fn(f"  {label} {hint} (laisser vide pour conserver) : ")).strip()
                if not value and field_config.get("required") and not existing_secret:
                    raise ToolPackageError(f"Le secret {label} est obligatoire.")
                if value:
                    secret_values[env_name] = value
                if field_config.get("config_key"):
                    normal_values[config_key] = env_name
                continue

            prompt_default = "" if existing is None else f" [{existing}]"
            while True:
                value = str(input_fn(f"  {label}{prompt_default} : ")).strip()
                if not value:
                    value = existing
                try:
                    if value is None or value == "":
                        if field_config.get("required"):
                            raise ValueError("ce champ est obligatoire")
                        break
                    if kind == "choice":
                        choices = [str(item) for item in field_config.get("choices", [])]
                        if value not in choices:
                            raise ValueError(f"choisir parmi : {', '.join(choices)}")
                    elif kind == "integer":
                        value = int(value)
                    elif kind == "number":
                        value = float(value)
                    elif kind == "boolean":
                        lowered = str(value).lower()
                        if lowered not in {"true", "false", "oui", "non", "o", "n", "yes", "no", "1", "0"}:
                            raise ValueError("répondre oui/non")
                        value = lowered in {"true", "oui", "o", "yes", "1"}
                    normal_values[config_key] = value
                    break
                except (TypeError, ValueError) as exc:
                    print(f"    Valeur invalide : {exc}")

        if normal_values:
            if table is None:
                self._update_toml_section(target_config, section, normal_values)
                self.config.setdefault(section, {}).update(normal_values)
            else:
                self._update_toml_table(target_config, str(table), normal_values)
                core = self.config.setdefault("_core", {})
                if isinstance(core, dict):
                    core.setdefault(str(table), {}).update(normal_values)
        self._write_env_values(target_env, secret_values)
        return bool(normal_values or secret_values)

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

    @staticmethod
    def _packages_in(directory: Path | None) -> list[tuple[ToolManifest, Path]]:
        if directory is None or not directory.is_dir():
            return []
        result: list[tuple[ToolManifest, Path]] = []
        for child in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
            if child.is_dir() and not child.name.startswith(".") and (child / MANIFEST_NAME).is_file():
                result.append((ToolManifest.from_file(child / MANIFEST_NAME), child))
        return result

    def installed(self) -> list[tuple[ToolManifest, Path]]:
        """Retourne les paquets ajoutés dans le répertoire d'installation utilisateur."""
        return self._packages_in(self.install_dir)

    def bundled(self) -> list[tuple[ToolManifest, Path]]:
        """Retourne les paquets de confiance livrés avec Orion, sans les importer."""
        return self._packages_in(self.bundled_dir)

    def load_all(self, client: Any) -> list[ToolManifest]:
        """Charge uniquement les packages explicitement autorisés par l'opérateur.

        Lire un manifeste est sans effet de bord, mais importer son point
        d'entrée exécute du Python arbitraire.  En l'absence d'une preuve de
        provenance/intégrité attachée au contenu installé, ``enabled = []`` ne
        constitue donc pas une autorisation d'auto-import : le package reste
        installé mais non chargé jusqu'à ce que son identifiant apparaisse dans
        ``enabled``.
        """
        enabled = {str(item) for item in self.config.get("enabled", []) if str(item).strip()}
        disabled = {str(item) for item in self.config.get("disabled", []) if str(item).strip()}
        context = ToolContext(
            root_dir=self.root_dir,
            data_dir=self.data_dir,
            install_dir=self.install_dir,
            config=self.config,
        )
        loaded: list[ToolManifest] = []
        self._loaded_guidance = {}
        self._tool_policy = self._operator_policy.copy()
        self._loaded_policy_tools = set()
        # Bundled packages are shipped with Orion but still require the exact
        # same explicit operator enablement as user-installed Python packages.
        # An explicitly installed package with the same id takes precedence,
        # preserving the existing update/override behavior without ever making
        # ``enabled=[]`` import code implicitly.
        for manifest, package_dir in self.available():
            self._tool_policy.register_package(manifest.id, manifest.policy)
            explicitly_enabled = manifest.id in enabled
            effective_requires_explicit_enable = manifest.policy.requires_explicit_enable
            if self._tool_policy.rule_for(manifest.id).classification is ToolClassification.PRIVILEGED:
                effective_requires_explicit_enable = True
            if any(
                self._tool_policy.rule_for(tool_name).classification is ToolClassification.PRIVILEGED
                for tool_name, _classification in manifest.policy.tool_classifications
            ):
                effective_requires_explicit_enable = True
            if (
                manifest.id in disabled
                or not explicitly_enabled
                or (effective_requires_explicit_enable and not explicitly_enabled)
            ):
                continue
            if manifest.kind == "tool":
                self._load_one(client, manifest, package_dir, context)
            loaded.append(manifest)
            self._loaded_guidance[manifest.id] = manifest.guidance
            self._loaded_policy_tools.add(manifest.id)
            self._loaded_policy_tools.update(name for name, _ in manifest.policy.tool_classifications)
        return loaded

    def tool_policy(self) -> ToolPolicy:
        """Return a copy of the central policy assembled from config/manifests."""
        return self._tool_policy.copy()

    def tool_policy_decision(
        self,
        tool_id: str,
        *,
        approved: bool = False,
        enabled: bool | None = None,
    ) -> ToolPolicyDecision:
        """Evaluate a tool without requiring an LLM or executing any code."""
        effective_enabled = tool_id in self._loaded_policy_tools if enabled is None else bool(enabled)
        return self._tool_policy.decide(tool_id, enabled=effective_enabled, approved=approved)

    def loaded_guidance(
        self, tool_ids: set[str] | list[str] | tuple[str, ...] | None = None
    ) -> dict[str, ToolGuidance]:
        """Return guidance for tools loaded by the most recent ``load_all``.

        If ``tool_ids`` is supplied, only those loaded tools are returned;
        this is useful when constructing instructions for a restricted
        sub-agent. The returned mapping is a copy.
        """
        if tool_ids is None:
            return dict(self._loaded_guidance)
        selected = {str(tool_id) for tool_id in tool_ids}
        return {
            tool_id: guidance
            for tool_id, guidance in self._loaded_guidance.items()
            if tool_id in selected
        }

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
    "ToolGuidance",
    "ToolPackageError",
    "ToolPackagePolicy",
    "ToolPolicy",
    "ToolPolicyDecision",
]
