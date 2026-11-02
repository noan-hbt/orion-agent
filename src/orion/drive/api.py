"""What reaches Google Drive, and how: settings, scopes, the calls allowed (D206).

Two levels, `access` in `[modules.drive]`:

- `own` (default): Orion reads the whole Drive (`drive.readonly`) and writes only what it
  created (`drive.file`); Google itself keeps it from touching his files.
- `full` (scope `drive`): Orion may also move, rename, trash and replace his files, each change
  with his yes, which the guardian checks itself (`drive_change`), unless he let it act alone.

The guardian, which holds the refresh token when Orion runs split (D28), relays the calls
listed in `allowed` and nothing else. Never sharing (`permissions`), never deleting for good
(the trash keeps a file 30 days), whatever the level.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Literal

import httpx
import tomlkit
from pydantic import Field
from tomlkit.items import Table

from orion.config import SecretRef
from orion.core.private import write_private
from orion.modules.base import ModuleSettings

GOOGLE_API = "https://www.googleapis.com/"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES: dict[str, tuple[str, ...]] = {
    "own": (
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.file",
    ),
    "full": ("https://www.googleapis.com/auth/drive",),
}

_READS = re.compile(r"drive/v3/(about|files(/[A-Za-z0-9_-]+(/export)?)?)")
_WRITES = re.compile(r"(upload/)?drive/v3/files")
_CHANGES = re.compile(r"(upload/)?drive/v3/files/([A-Za-z0-9_-]+)")


class DriveSettings(ModuleSettings):
    """`[modules.drive]`: the same OAuth client as the calendar, with a refresh token from
    `orion google auth <client id> --drive`."""

    client_id: str = ""
    client_secret: SecretRef = SecretRef(env="GOOGLE_CLIENT_SECRET")
    refresh_token: SecretRef = Field(
        default=SecretRef(env="GOOGLE_REFRESH_TOKEN"), description="From `orion google auth`"
    )
    folder: str = Field(
        default="Orion", min_length=1, max_length=80, description="Where Orion puts its files"
    )
    access: Literal["own", "full"] = Field(
        default="own",
        description="own: reads all, writes only its own files; full: also moves, renames, "
        "trashes and replaces his files, each time with his yes (token from `--drive full`)",
    )


class DriveError(RuntimeError):
    """Drive refused or could not be reached."""


def allowed(method: str, path: str, full: bool = False) -> bool:
    """Reading anything; creating a file or a folder; with full access, changing one. Never
    sharing, never deleting for good."""
    path = path.lstrip("/")
    if method == "GET":
        return _READS.fullmatch(path) is not None
    if method == "POST":
        return _WRITES.fullmatch(path) is not None
    if method == "PATCH":
        return full and changed(path) is not None
    return False


def changed(path: str) -> str | None:
    """The id of the file a change is for."""
    found = _CHANGES.fullmatch(path.lstrip("/"))
    return found.group(2) if found else None


class GoogleAuth(httpx.Auth):
    """Adds an access token, refreshed from the refresh token when it is about to expire."""

    def __init__(self, settings: DriveSettings) -> None:
        if not settings.client_id:
            raise DriveError("[modules.drive] needs the OAuth client_id")
        self._settings = settings
        self._token = ""
        self._expires = 0.0

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        if time.monotonic() >= self._expires:
            settings = self._settings
            answer = yield httpx.Request(
                "POST",
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": settings.client_id,
                    "client_secret": settings.client_secret.resolve().get_secret_value(),
                    "refresh_token": settings.refresh_token.resolve().get_secret_value(),
                    "grant_type": "refresh_token",
                },
            )
            await answer.aread()
            if answer.status_code != 200:
                raise DriveError(f"Google refused the refresh token ({answer.status_code})")
            body = answer.json()
            self._token = str(body["access_token"])
            self._expires = time.monotonic() + float(body.get("expires_in", 3600)) - 60
        request.headers["authorization"] = f"Bearer {self._token}"
        yield request


def enable_in_config(path: Path, client_id: str, access: str) -> None:
    """Turn `[modules.drive]` on in orion.toml, with its secrets' names (never their values),
    keeping the file's comments and layout. Creates the section if it is not there."""
    doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    modules = doc.setdefault("modules", tomlkit.table(is_super_table=True))
    section = modules.get("drive")
    if not isinstance(section, Table):
        section = tomlkit.table()
        modules["drive"] = section
    section["enabled"] = True
    section["client_id"] = client_id
    section["client_secret"] = tomlkit.inline_table()
    section["client_secret"]["env"] = "GOOGLE_CLIENT_SECRET"  # type: ignore[index]
    section["refresh_token"] = tomlkit.inline_table()
    section["refresh_token"]["env"] = "GOOGLE_REFRESH_TOKEN"  # type: ignore[index]
    section.setdefault("folder", "Orion")
    section["access"] = access
    write_private(path, tomlkit.dumps(doc))


def direct_client(settings: DriveSettings, timeout: float = 60.0) -> httpx.AsyncClient:
    """Straight to Google, with the credentials: Orion in one process, or the guardian."""
    return httpx.AsyncClient(base_url=GOOGLE_API, auth=GoogleAuth(settings), timeout=timeout)
