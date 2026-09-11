"""Terminal contrôlé pour Orion.

La commande est volontairement explicite et la sortie est bornée. Le tool
doit être activé uniquement dans une installation où l'utilisateur accepte
qu'Orion puisse exécuter des commandes locales.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Any, Mapping


_BASE_ENV_NAMES = (
    "PATH",
    "PATHEXT",
    "COMSPEC",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "TMPDIR",
)


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


def _minimal_environment(settings: Mapping[str, Any]) -> dict[str, str]:
    """Build a small inherited environment instead of exposing Orion's process env.

    ``env_allowlist`` may name additional variables that an operator explicitly
    wants inherited. Secrets are therefore unavailable to shell commands unless
    their exact variable names are deliberately allowed.
    """
    raw_allowlist = settings.get("env_allowlist", ())
    if isinstance(raw_allowlist, str):
        raw_allowlist = [raw_allowlist]
    if not isinstance(raw_allowlist, (list, tuple, set, frozenset)):
        raise ValueError("terminal.env_allowlist doit être une liste de noms de variables.")

    requested: list[str] = []
    for item in raw_allowlist:
        name = str(item).strip()
        if not name or "=" in name or "\x00" in name:
            raise ValueError("terminal.env_allowlist contient un nom de variable invalide.")
        requested.append(name)

    names = [*_BASE_ENV_NAMES, *requested]
    result: dict[str, str] = {}
    if os.name == "nt":
        inherited = {key.upper(): (key, value) for key, value in os.environ.items()}
        for name in names:
            found = inherited.get(name.upper())
            if found is not None:
                original, value = found
                result[original] = value
    else:
        for name in names:
            if name in os.environ:
                result[name] = os.environ[name]
    return result


def _popen_group_options() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _terminate_windows_tree(process: subprocess.Popen[str], *, grace_seconds: float) -> None:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=max(0.1, grace_seconds),
            check=False,
            env=_minimal_environment({}),
        )
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except OSError:
            pass


def _terminate_process_tree(process: subprocess.Popen[str], *, grace_seconds: float = 1.0) -> None:
    """Best-effort termination of the shell and descendants after timeout."""
    if os.name == "nt":
        _terminate_windows_tree(process, grace_seconds=grace_seconds)
    else:
        # Do not return merely because the shell leader already exited.  A
        # descendant can still belong to the session/process group created for
        # this invocation and keep inherited stdout/stderr pipes open.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
        try:
            process.wait(timeout=max(0.1, grace_seconds))
        except subprocess.TimeoutExpired:
            pass

        # The leader may have exited while descendants remain.  Probe the
        # process group itself before deciding whether a forceful second pass
        # is needed.
        try:
            os.killpg(process.pid, 0)
        except (OSError, ProcessLookupError):
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass

    try:
        process.wait(timeout=max(0.1, grace_seconds))
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass


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
    child_env = _minimal_environment(settings)
    encoding = str(settings.get("encoding", "utf-8"))

    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=str(workdir),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding=encoding,
            errors="replace",
            env=child_env,
            **_popen_group_options(),
        )
        stdout, stderr = process.communicate(timeout=effective_timeout)
    except subprocess.TimeoutExpired as exc:
        if process is not None:
            _terminate_process_tree(process)
            try:
                stdout, stderr = process.communicate(timeout=1.0)
            except subprocess.TimeoutExpired as drain_exc:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    stdout, stderr = process.communicate(timeout=0.5)
                except subprocess.TimeoutExpired as final_exc:
                    # An escaped descendant may still own an inherited pipe.
                    # Never let output draining turn a bounded command timeout
                    # into an unbounded wait; retain whatever communicate()
                    # captured so far instead.
                    stdout = final_exc.stdout if final_exc.stdout is not None else drain_exc.stdout
                    stderr = final_exc.stderr if final_exc.stderr is not None else drain_exc.stderr
                    stdout = stdout if stdout is not None else exc.stdout or ""
                    stderr = stderr if stderr is not None else exc.stderr or ""
                    for stream in (process.stdout, process.stderr):
                        if stream is not None:
                            try:
                                stream.close()
                            except OSError:
                                pass
        else:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(encoding, errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(encoding, errors="replace")
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

    stdout, stdout_truncated = _clip(stdout or "", max_output_chars)
    stderr, stderr_truncated = _clip(stderr or "", max_output_chars)
    return {
        "command": command,
        "cwd": str(workdir),
        "timed_out": False,
        "timeout_seconds": effective_timeout,
        "exit_code": process.returncode if process is not None else None,
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
            "Ne pas utiliser pour une action destructive sans confirmation explicite. "
            "Ce tool limite cwd/env/timeout mais n'isole pas fortement le filesystem ni le réseau."
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
