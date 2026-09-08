"""Terminal contrôlé pour Orion.

La commande est volontairement explicite et la sortie est bornée. Le tool
doit être activé uniquement dans une installation où l'utilisateur accepte
qu'Orion puisse exécuter des commandes locales.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Mapping


def _settings(context: Any) -> Mapping[str, Any]:
    if context is None:
        return {}
    value = context.config.get("terminal", {})
    return value if isinstance(value, Mapping) else {}


def _clip(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _working_directory(context: Any, cwd: str | None, allow_outside_root: bool) -> Path:
    root = Path(context.root_dir if context is not None else Path.cwd()).resolve()
    candidate = (root / cwd if cwd and not Path(cwd).is_absolute() else Path(cwd or root)).resolve()
    if not allow_outside_root and not candidate.is_relative_to(root):
        raise ValueError(f"Le répertoire de travail doit rester sous {root}.")
    if not candidate.is_dir():
        raise ValueError(f"Répertoire de travail introuvable : {candidate}")
    return candidate


def run_terminal(
    command: str,
    cwd: str | None = None,
    timeout: int | None = None,
    *,
    _context: Any = None,
) -> dict[str, Any]:
    """Exécute une commande et renvoie une observation JSON compacte."""
    if not command or not command.strip():
        raise ValueError("La commande ne peut pas être vide.")
    if "\x00" in command:
        raise ValueError("La commande contient un caractère nul.")

    settings = _settings(_context)
    max_timeout = max(1, int(settings.get("max_timeout", 120)))
    requested_timeout = max_timeout if timeout is None else int(timeout)
    if requested_timeout < 1:
        raise ValueError("timeout doit être supérieur ou égal à 1 seconde.")
    effective_timeout = min(requested_timeout, max_timeout)
    max_output_chars = max(100, int(settings.get("max_output_chars", 12000)))
    allow_outside_root = bool(settings.get("allow_outside_root", False))
    workdir = _working_directory(_context, cwd, allow_outside_root)

    try:
        completed = subprocess.run(
            command,
            cwd=str(workdir),
            shell=True,
            capture_output=True,
            text=True,
            encoding=str(settings.get("encoding", "utf-8")),
            errors="replace",
            timeout=effective_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        stdout, stdout_truncated = _clip(stdout, max_output_chars)
        stderr, stderr_truncated = _clip(stderr, max_output_chars)
        return {
            "command": command,
            "cwd": str(workdir),
            "timed_out": True,
            "timeout_seconds": effective_timeout,
            "exit_code": None,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": stdout_truncated or stderr_truncated,
        }

    stdout, stdout_truncated = _clip(completed.stdout or "", max_output_chars)
    stderr, stderr_truncated = _clip(completed.stderr or "", max_output_chars)
    return {
        "command": command,
        "cwd": str(workdir),
        "timed_out": False,
        "timeout_seconds": effective_timeout,
        "exit_code": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": stdout_truncated or stderr_truncated,
    }


def register(client: Any, context: Any = None) -> None:
    client.register_tool(
        "terminal",
        lambda command, cwd=None, timeout=None: run_terminal(
            command,
            cwd,
            timeout,
            _context=context,
        ),
        description=(
            "Exécuter une commande shell locale lorsque l'utilisateur le demande "
            "ou qu'une tâche l'exige. Retourne stdout, stderr et le code de sortie. "
            "Ne pas utiliser pour une action destructive sans confirmation explicite."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Commande shell à exécuter."},
                "cwd": {"type": "string", "description": "Répertoire relatif au projet, optionnel."},
                "timeout": {"type": "integer", "minimum": 1, "description": "Timeout en secondes."},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        side_effect=True,
        dedupe_window=0,
    )
