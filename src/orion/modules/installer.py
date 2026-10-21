"""Installing and removing native modules (D105).

A native module is a Python package exposing a `Module` subclass under the `orion.modules`
entry point group. It runs inside Orion (the core) with the same access as Orion itself, the
owner's memory included: so it comes only from the catalog (`catalog/modules/<name>.toml`), at
a pinned source, with the owner's yes to that exact source, never from a conversation holding
outside content.

The core installs it, into its own data folder (`modules/site/<name>`, listed in
`modules/installed.toml`): a package's build scripts then run with exactly the rights the
module will have, and never near the guardian's secrets. The guardian never imports a
module's code; it only reads which ones are installed (D28). The code's own environment is not
touched, so an update (`uv sync`) keeps them, and removing one is deleting its folder.

Installing: into a temporary folder; a separate Python process checks that the entry point
loads and is the module it claims to be; only then is it moved into place and recorded.
Turning it on, or off before removing it, is a config change the guardian writes with the same
yes (`orion.modules.tools`).
"""

from __future__ import annotations

import importlib
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog
import tomlkit
from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from orion.config import OrionConfig

log = structlog.get_logger(__name__)

REPO = Path(__file__).resolve().parents[3]
CATALOG = REPO / "catalog" / "modules"
"""The public catalog, beside the code (a checkout of Orion's repository)."""
LOCK = "installed.toml"
NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
COMMIT = re.compile(r"@[0-9a-f]{40}(#|$)")
INSTALL_TIMEOUT = 600.0


class InstallError(RuntimeError):
    """A module cannot be installed or removed; the message says why, for the owner."""


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=NAME.pattern)
    description: str = Field(min_length=1, max_length=300)
    source: str = Field(
        description="`path:<folder in Orion's repository>`, `<package>==<version>`, or "
        "`git+https://…@<full commit>`: always one exact version"
    )
    accesses: str = Field(default="", max_length=300, description="What it reaches, in French")
    settings: str = Field(default="", max_length=300, description="What to set before it runs")


def pinned(source: str) -> bool:
    """One exact version: what the owner approves is what gets installed."""
    if source.startswith("path:"):
        return ".." not in Path(source.removeprefix("path:")).parts
    if source.startswith("git+"):
        return COMMIT.search(source) is not None
    return re.fullmatch(r"[A-Za-z0-9_.\-]+==[A-Za-z0-9_.\-+]+", source) is not None


def read_catalog(directory: Path = CATALOG) -> dict[str, CatalogEntry]:
    found: dict[str, CatalogEntry] = {}
    for path in sorted(directory.glob("*.toml")) if directory.is_dir() else []:
        try:
            entry = CatalogEntry.model_validate(tomllib.loads(path.read_text("utf-8")))
        except (tomllib.TOMLDecodeError, ValidationError) as exc:
            log.warning("modules.catalog_unreadable", file=path.name, error=str(exc))
            continue
        if not pinned(entry.source):
            log.warning("modules.catalog_unpinned", module=entry.name, source=entry.source)
            continue
        found[entry.name] = entry
    return found


def modules_dir(config: OrionConfig) -> Path:
    """In the core's data folder: the core writes there, the guardian only reads the list."""
    return config.data_dir / "modules"


@dataclass(frozen=True)
class Installed:
    name: str
    source: str
    installed_at: str


def installed(directory: Path) -> dict[str, Installed]:
    lock = directory / LOCK
    if not lock.is_file():
        return {}
    data = tomllib.loads(lock.read_text("utf-8"))
    entries = cast(dict[str, dict[str, Any]], data.get("module", {}))
    return {
        name: Installed(name, str(e.get("source", "")), str(e.get("installed_at", "")))
        for name, e in entries.items()
    }


def _write_lock(directory: Path, modules: dict[str, Installed]) -> None:
    doc = tomlkit.document()
    doc.add(tomlkit.comment("Modules installed by Orion (`orion modules`); do not edit by hand."))
    table = tomlkit.table()
    for m in sorted(modules.values(), key=lambda m: m.name):
        table[m.name] = {"source": m.source, "installed_at": m.installed_at}
    doc["module"] = table
    tmp = directory / f".{LOCK}.tmp"
    tmp.write_text(tomlkit.dumps(doc), "utf-8")
    tmp.replace(directory / LOCK)


def site(directory: Path, name: str) -> Path:
    return directory / "site" / name


class _Process:
    imports_modules = True
    """False in the guardian process: it never runs a module's code (`trust_no_module`)."""


def imports_modules() -> bool:
    return _Process.imports_modules


def trust_no_module() -> None:
    _Process.imports_modules = False


def activate(directory: Path) -> None:
    """Make the installed modules importable: their folders go after Orion's own packages, so
    a module never replaces what Orion runs on."""
    if not _Process.imports_modules:
        return
    added = False
    for name in installed(directory):
        folder = str(site(directory, name))
        if Path(folder).is_dir() and folder not in sys.path:
            sys.path.append(folder)
            added = True
    if added:
        importlib.invalidate_caches()


_VERIFY = """
import sys
from importlib.metadata import distributions
sys.path.append(sys.argv[1])
from orion.modules.base import Module
found = [
    e for d in distributions(path=[sys.argv[1]]) for e in d.entry_points
    if e.group == "orion.modules"
]
if [e.name for e in found] != [sys.argv[2]]:
    sys.exit(f"it declares {[e.name for e in found]} instead of {sys.argv[2]!r}")
kind = found[0].load()
if not (isinstance(kind, type) and issubclass(kind, Module) and kind.name == sys.argv[2]):
    sys.exit("its entry point is not an orion Module of that name")
print("ok")
"""

Runner = Callable[[list[str], dict[str, str]], tuple[int, str]]
"""(command, environment) → (exit code, output): how the installer runs a process."""


def run_process(command: list[str], env: dict[str, str]) -> tuple[int, str]:
    try:
        done = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()


class Installer:
    def __init__(
        self,
        config: OrionConfig,
        catalog: Path = CATALOG,
        run: Runner = run_process,
        uv: str | None = None,
    ) -> None:
        self.config = config
        self.directory = modules_dir(config)
        self.catalog_dir = catalog
        self._run = run
        self._uv = uv or shutil.which("uv") or "uv"

    def catalog(self) -> dict[str, CatalogEntry]:
        return read_catalog(self.catalog_dir)

    def installed(self) -> dict[str, Installed]:
        return installed(self.directory)

    def _spec(self, entry: CatalogEntry) -> str:
        if entry.source.startswith("path:"):
            folder = (REPO / entry.source.removeprefix("path:")).resolve()
            if not folder.is_relative_to(REPO) or not folder.is_dir():
                raise InstallError(f"{entry.name}: its folder is not in Orion's repository")
            return str(folder)
        return entry.source

    def install(self, name: str) -> CatalogEntry:
        """Install `name` from the catalog (not yet on). Raises InstallError."""
        from orion.modules.registry import builtin_modules

        entry = self.catalog().get(name)
        if entry is None:
            raise InstallError(f"« {name} » n'est pas dans le catalogue des modules")
        if name in {kind.name for kind in builtin_modules()}:
            raise InstallError(f"« {name} » fait déjà partie d'Orion")
        if name in self.installed():
            raise InstallError(f"« {name} » est déjà installé")
        (self.directory / "site").mkdir(parents=True, exist_ok=True)
        tmp = site(self.directory, f".{name}.new")
        shutil.rmtree(tmp, ignore_errors=True)
        env = {**os.environ, "UV_CACHE_DIR": str(self.directory / ".cache")}
        command = [self._uv, "pip", "install", "--quiet", "--python", sys.executable]
        code, out = self._run([*command, "--target", str(tmp), self._spec(entry)], env)
        if code != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise InstallError(f"l'installation de « {name} » a échoué : {out[-400:]}")
        code, out = self._run([sys.executable, "-c", _VERIFY, str(tmp), name], env)
        if code != 0 or not out.endswith("ok"):
            shutil.rmtree(tmp, ignore_errors=True)
            raise InstallError(f"« {name} » ne se charge pas : {out[-400:]}")
        shutil.rmtree(site(self.directory, name), ignore_errors=True)
        tmp.rename(site(self.directory, name))
        modules = self.installed()
        modules[name] = Installed(name, entry.source, datetime.now(UTC).isoformat())
        _write_lock(self.directory, modules)
        activate(self.directory)
        log.info("modules.installed", module=name, source=entry.source)
        return entry

    def remove(self, name: str) -> None:
        """Delete an installed module (turned off first). Raises InstallError."""
        remaining = self.installed()
        if name not in remaining:
            raise InstallError(f"« {name} » n'est pas un module installé")
        del remaining[name]
        _write_lock(self.directory, remaining)
        shutil.rmtree(site(self.directory, name), ignore_errors=True)
        log.info("modules.removed", module=name)
