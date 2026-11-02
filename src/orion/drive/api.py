"""What reaches Google Drive, and how: settings, scopes, the calls allowed (D206).

Orion reads the whole Drive (`drive.readonly`) and writes only what it created itself
(`drive.file`): it can neither change, delete nor share the owner's own files, and the scopes
say so to Google, not only to Orion. The guardian, which holds the refresh token when Orion
runs split (D28), relays the calls listed in `allowed` and nothing else: no sharing
(`permissions`), no deletion, no change to an existing file.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncGenerator

import httpx
from pydantic import Field

from orion.config import SecretRef
from orion.modules.base import ModuleSettings

GOOGLE_API = "https://www.googleapis.com/"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
)

_READS = re.compile(r"drive/v3/(about|files(/[A-Za-z0-9_-]+(/export)?)?)")
_WRITES = re.compile(r"(upload/)?drive/v3/files")


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


class DriveError(RuntimeError):
    """Drive refused or could not be reached."""


def allowed(method: str, path: str) -> bool:
    """Reading anything; creating a file or a folder. Never sharing, changing or deleting."""
    path = path.lstrip("/")
    if method == "GET":
        return _READS.fullmatch(path) is not None
    if method == "POST":
        return _WRITES.fullmatch(path) is not None
    return False


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


def direct_client(settings: DriveSettings, timeout: float = 60.0) -> httpx.AsyncClient:
    """Straight to Google, with the credentials: Orion in one process, or the guardian."""
    return httpx.AsyncClient(base_url=GOOGLE_API, auth=GoogleAuth(settings), timeout=timeout)
