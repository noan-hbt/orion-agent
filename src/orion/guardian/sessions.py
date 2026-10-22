"""Logged-in site sessions, kept by the guardian (D49).

To act on the owner's account (book with their Doctolib, check an order), the browser needs
the site's cookies. They are a secret, so the guardian keeps them, like the mail passwords:

1. `orion login <name> <url>` on a machine with a screen opens a browser; the owner signs in,
   and the session (cookies, local storage) is written to a file;
2. `orion sessions add <name> <file>` on the server, as the guardian's user, files it under
   the guardian's directory (0600) and records which sites it is for;
3. for a sign-in the owner approved (`browser` login with that session, D113) or one Orion may
   do alone on those sites, the guardian lends it; the core holds it in memory, in that
   browser only, and every loan is recorded.

    guardian.session.added  guardian.session.removed  guardian.session.lent
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from orion.config import OrionConfig
from orion.guardian.ledger import SESSION_ADDED, SESSION_REMOVED, Ledger

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


class SessionError(ValueError):
    """Not a usable session."""


def sessions_dir(config: OrionConfig) -> Path:
    return config.guardian.data_dir / "sessions"


def cookie_domains(state: dict[str, Any]) -> list[str]:
    cookies: list[dict[str, Any]] = list(state.get("cookies") or [])
    return sorted({str(c.get("domain", "")).lstrip(".").lower() for c in cookies} - {""})


def read_state(path: Path) -> dict[str, Any]:
    try:
        loaded: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionError(f"{path} is not a session file: {exc}") from exc
    if not isinstance(loaded, dict) or "cookies" not in loaded:
        raise SessionError(f"{path} holds no cookies: make it with `orion login`")
    state: dict[str, Any] = loaded  # pyright: ignore[reportUnknownVariableType]
    return state


async def add_session(
    config: OrionConfig, ledger: Ledger, name: str, state: dict[str, Any], domains: list[str]
) -> list[str]:
    if not NAME.match(name):
        raise SessionError("a session name is lowercase letters, digits, - or _")
    sites = sorted({d.lower().lstrip(".") for d in domains} or set(cookie_domains(state)))
    if not sites:
        raise SessionError("the session is for no site: give --domains")
    folder = sessions_dir(config)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    path = folder / f"{name}.json"
    data = json.dumps(state)
    path.write_text(data, encoding="utf-8")
    path.chmod(0o600)
    await ledger.record(
        SESSION_ADDED,
        {"name": name, "domains": sites, "digest": hashlib.sha256(data.encode()).hexdigest()},
    )
    return sites


async def remove_session(config: OrionConfig, ledger: Ledger, name: str) -> bool:
    path = sessions_dir(config) / f"{name}.json"
    existed = path.exists()
    path.unlink(missing_ok=True)
    await ledger.record(SESSION_REMOVED, {"name": name})
    return existed
