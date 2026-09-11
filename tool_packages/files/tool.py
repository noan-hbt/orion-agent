"""Read-only workspace file tools for Orion.

The package deliberately exposes no mutation primitive. Every requested path is
resolved before use and must remain under ``ToolContext.root_dir``. Common
credential files are refused before any content is read.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any, Mapping


DEFAULT_MAX_READ_CHARS = 40_000
DEFAULT_MAX_FILE_BYTES = 2_000_000
HARD_MAX_READ_CHARS = 200_000
HARD_MAX_FILE_BYTES = 10_000_000
HARD_MAX_RESULTS = 500

_SENSITIVE_BASENAMES = {
    ".env",
    ".netrc",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "known_hosts.old",
}
_SENSITIVE_SUFFIXES = {
    ".key",
    ".p12",
    ".pfx",
    ".pem",
}
_SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
_SECRET_ASSIGNMENT = re.compile(
    r"(?im)^(?P<prefix>\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_.-]*"
    r"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential)"
    r"[A-Za-z0-9_.-]*\s*[:=]\s*)(?P<value>.*)$"
)
_URL_USERINFO = re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@")
_URL_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:token|key|secret|password|passwd|authorization|credential)=)[^&#\s]+"
)


def _settings(context: Any) -> Mapping[str, Any]:
    if context is None:
        return {}
    config = getattr(context, "config", {})
    if not isinstance(config, Mapping):
        return {}
    value = config.get("files", {})
    return value if isinstance(value, Mapping) else {}


def _root(context: Any) -> Path:
    value = getattr(context, "root_dir", None) if context is not None else None
    return Path(value or Path.cwd()).resolve()


def _bounded_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    return max(minimum, min(result, maximum))


def _relative_display(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    text = relative.as_posix()
    return text or "."


def _is_sensitive(path: Path) -> bool:
    name = path.name.lower()
    if name in _SENSITIVE_BASENAMES or name.startswith(".env."):
        return True
    if path.suffix.lower() in _SENSITIVE_SUFFIXES:
        return True
    return False


def _resolve(context: Any, value: str, *, expect: str | None = None) -> tuple[Path, Path]:
    root = _root(context)
    raw = str(value or ".").strip()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"Chemin introuvable dans le workspace : {raw}") from exc
    if resolved != root and not resolved.is_relative_to(root):
        raise PermissionError("Le chemin demandé sort du workspace autorisé.")
    if _is_sensitive(resolved):
        raise PermissionError("Ce fichier sensible n'est pas lisible par les tools workspace.")
    if expect == "file" and not resolved.is_file():
        raise ValueError(f"Le chemin n'est pas un fichier : {_relative_display(resolved, root)}")
    if expect == "dir" and not resolved.is_dir():
        raise ValueError(f"Le chemin n'est pas un répertoire : {_relative_display(resolved, root)}")
    return resolved, root


def _redact_text(text: str) -> str:
    text = _SECRET_ASSIGNMENT.sub(lambda match: match.group("prefix") + "[REDACTED]", text)
    text = _URL_USERINFO.sub(r"\1[REDACTED]@", text)
    return _URL_QUERY_SECRET.sub(r"\1[REDACTED]", text)


def _read_text(path: Path, *, context: Any) -> str:
    settings = _settings(context)
    max_file_bytes = _bounded_int(
        settings.get("max_file_bytes"),
        DEFAULT_MAX_FILE_BYTES,
        minimum=1,
        maximum=HARD_MAX_FILE_BYTES,
    )
    size = path.stat().st_size
    if size > max_file_bytes:
        raise ValueError(
            f"Fichier trop volumineux ({size} octets, limite {max_file_bytes})."
        )
    raw = path.read_bytes()
    if b"\x00" in raw[:8192]:
        raise ValueError("Le fichier semble binaire et ne peut pas être lu comme texte.")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Le fichier n'est pas un texte UTF-8 lisible.") from exc


def list_files(
    path: str = ".",
    pattern: str = "*",
    recursive: bool = True,
    max_results: int = 200,
    *,
    _context: Any = None,
) -> dict[str, Any]:
    """List workspace files without reading their contents."""
    base, root = _resolve(_context, path, expect="dir")
    limit = _bounded_int(max_results, 200, minimum=1, maximum=HARD_MAX_RESULTS)
    wanted = str(pattern or "*")
    results: list[dict[str, Any]] = []
    truncated = False

    if recursive:
        walker = os.walk(base, followlinks=False)
        for current, dirs, files in walker:
            dirs[:] = [name for name in dirs if name not in _SKIP_DIRS]
            current_path = Path(current)
            for name in sorted(files, key=str.lower):
                candidate = current_path / name
                try:
                    resolved = candidate.resolve(strict=True)
                except (FileNotFoundError, OSError):
                    continue
                if resolved != root and not resolved.is_relative_to(root):
                    continue
                if _is_sensitive(resolved):
                    continue
                relative = _relative_display(resolved, root)
                if not (fnmatch.fnmatch(name, wanted) or fnmatch.fnmatch(relative, wanted)):
                    continue
                results.append({"path": relative, "size": resolved.stat().st_size})
                if len(results) >= limit:
                    truncated = True
                    break
            if truncated:
                break
    else:
        for candidate in sorted(base.iterdir(), key=lambda item: item.name.lower()):
            if not candidate.is_file() or _is_sensitive(candidate):
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except (FileNotFoundError, OSError):
                continue
            if resolved != root and not resolved.is_relative_to(root):
                continue
            relative = _relative_display(resolved, root)
            if not (fnmatch.fnmatch(candidate.name, wanted) or fnmatch.fnmatch(relative, wanted)):
                continue
            results.append({"path": relative, "size": resolved.stat().st_size})
            if len(results) >= limit:
                truncated = True
                break

    return {
        "path": _relative_display(base, root),
        "files": results,
        "count": len(results),
        "truncated": truncated,
    }


def read_file(
    path: str,
    start_line: int = 1,
    end_line: int | None = None,
    max_chars: int | None = None,
    *,
    _context: Any = None,
) -> dict[str, Any]:
    """Read a bounded line range from one UTF-8 workspace file."""
    target, root = _resolve(_context, path, expect="file")
    text = _read_text(target, context=_context)
    lines = text.splitlines()
    first = max(1, int(start_line))
    last = len(lines) if end_line is None else max(first, int(end_line))
    selected = lines[first - 1 : last]
    rendered = "\n".join(selected)

    settings = _settings(_context)
    configured = _bounded_int(
        settings.get("max_read_chars"),
        DEFAULT_MAX_READ_CHARS,
        minimum=1,
        maximum=HARD_MAX_READ_CHARS,
    )
    limit = configured if max_chars is None else _bounded_int(
        max_chars, configured, minimum=1, maximum=min(configured, HARD_MAX_READ_CHARS)
    )
    rendered = _redact_text(rendered)
    truncated = len(rendered) > limit
    if truncated:
        rendered = rendered[:limit]

    returned_lines = rendered.count("\n") + (1 if rendered else 0)
    return {
        "path": _relative_display(target, root),
        "start_line": first,
        "end_line": first + returned_lines - 1 if returned_lines else first - 1,
        "total_lines": len(lines),
        "content": rendered,
        "truncated": truncated or last < len(lines),
    }


def search_files(
    query: str,
    path: str = ".",
    pattern: str = "*",
    max_results: int = 50,
    *,
    _context: Any = None,
) -> dict[str, Any]:
    """Search literal text across bounded UTF-8 files in the workspace."""
    needle = str(query)
    if not needle:
        raise ValueError("La recherche ne peut pas être vide.")
    listing = list_files(
        path,
        pattern,
        True,
        HARD_MAX_RESULTS,
        _context=_context,
    )
    limit = _bounded_int(max_results, 50, minimum=1, maximum=HARD_MAX_RESULTS)
    matches: list[dict[str, Any]] = []
    lowered = needle.casefold()
    skipped_files = 0
    for item in listing["files"]:
        try:
            target, root = _resolve(_context, item["path"], expect="file")
            text = _read_text(target, context=_context)
        except (OSError, ValueError, PermissionError):
            skipped_files += 1
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if lowered not in line.casefold():
                continue
            matches.append(
                {
                    "path": _relative_display(target, root),
                    "line": line_number,
                    "text": _redact_text(line)[:1000],
                }
            )
            if len(matches) >= limit:
                return {
                    "query": needle,
                    "matches": matches,
                    "count": len(matches),
                    "truncated": True,
                    "skipped_files": skipped_files,
                }
    return {
        "query": needle,
        "matches": matches,
        "count": len(matches),
        "truncated": bool(listing.get("truncated")),
        "skipped_files": skipped_files,
    }


def files(
    action: str,
    *,
    path: str | None = None,
    pattern: str = "*",
    recursive: bool = True,
    max_results: int | None = None,
    start_line: int = 1,
    end_line: int | None = None,
    max_chars: int | None = None,
    query: str | None = None,
    _context: Any = None,
) -> dict[str, Any]:
    """Single model-facing entrypoint for safe, read-only workspace access."""
    selected = str(action or "").strip().lower()
    if selected == "list":
        return list_files(
            path or ".",
            pattern,
            recursive,
            200 if max_results is None else max_results,
            _context=_context,
        )
    if selected == "read":
        if path is None or not str(path).strip():
            raise ValueError("L'action 'read' exige un chemin de fichier non vide.")
        return read_file(
            str(path),
            start_line,
            end_line,
            max_chars,
            _context=_context,
        )
    if selected == "search":
        if query is None or not str(query):
            raise ValueError("L'action 'search' exige une recherche non vide.")
        return search_files(
            str(query),
            path or ".",
            pattern,
            50 if max_results is None else max_results,
            _context=_context,
        )
    raise ValueError("Action files inconnue. Valeurs autorisées : list, read, search.")


def register(client: Any, context: Any = None) -> None:
    client.register_tool(
        "files",
        lambda action, path=None, pattern="*", recursive=True, max_results=None,
        start_line=1, end_line=None, max_chars=None, query=None: files(
            action,
            path=path,
            pattern=pattern,
            recursive=recursive,
            max_results=max_results,
            start_line=start_line,
            end_line=end_line,
            max_chars=max_chars,
            query=query,
            _context=context,
        ),
        description=(
            "Accéder aux fichiers texte du workspace en lecture seule avec un seul point d'entrée. "
            "Contrats exacts : files(action='list'[, path='.', pattern='*', recursive=true, max_results]); "
            "files(action='read', path=<fichier>[, start_line, end_line, max_chars]); "
            "files(action='search', query=<texte>[, path='.', pattern='*', max_results]). "
            "N'envoie que les champs pertinents pour l'action choisie."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "read", "search"],
                    "description": (
                        "Choisir exactement une action. read exige path; search exige query; list n'exige aucun autre champ."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": "Fichier pour 'read', ou répertoire pour 'list'/'search'.",
                },
                "pattern": {"type": "string", "description": "Glob de nom/chemin, par exemple '*.toml'."},
                "recursive": {"type": "boolean", "description": "Optionnel, seulement pour list."},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": HARD_MAX_RESULTS,
                    "description": "Optionnel pour list/search.",
                },
                "start_line": {"type": "integer", "minimum": 1, "description": "Optionnel, seulement pour read."},
                "end_line": {"type": ["integer", "null"], "minimum": 1, "description": "Optionnel, seulement pour read."},
                "max_chars": {"type": ["integer", "null"], "minimum": 1, "maximum": HARD_MAX_READ_CHARS, "description": "Optionnel, seulement pour read."},
                "query": {
                    "type": "string",
                    "description": "Requis pour action='search' : texte littéral à chercher.",
                },
            },
            "required": ["action"],
            "allOf": [
                {
                    "if": {"properties": {"action": {"const": "read"}}, "required": ["action"]},
                    "then": {"required": ["action", "path"]},
                },
                {
                    "if": {"properties": {"action": {"const": "search"}}, "required": ["action"]},
                    "then": {"required": ["action", "query"]},
                },
            ],
            "additionalProperties": False,
        },
    )


__all__ = ["files", "register"]
