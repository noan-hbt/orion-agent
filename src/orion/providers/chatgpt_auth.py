"""Sign in with ChatGPT (D62): the owner's ChatGPT plan pays for Orion's model calls.

OpenAI's official flow for open-source and personal apps: Orion registers itself once
(`client_id=dynamic_agent_client`), the owner signs in in a browser (OAuth code + PKCE) and
grants `chatgpt.tokens.use.direct`; the access token then calls the public Responses API, and
each call draws on the plan's allowance. The browser is sent back to a 127.0.0.1 address that
does not load on a server: the owner pastes that address, the code in it is enough.

The credentials file is a secret that rotates (each refresh replaces the refresh token), so it
cannot live in an environment variable: it is written with owner-only permissions next to the
journal, never logged, never copied by `orion backup`.

The ID token comes straight from the token endpoint over TLS: per OpenID Connect (core 3.1.3.7)
the TLS server check stands in for the signature, and issuer, audience, expiry and nonce are
checked here.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

log = structlog.get_logger(__name__)

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
REDIRECT_URI = "http://127.0.0.1:1455/auth/callback"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = f"openid profile email offline_access resource.invoke {PLAN_SCOPE}"
FIRST_CLIENT = "dynamic_agent_client"
APP_NAME = "Orion"
REFRESH_MARGIN_SECONDS = 300


class ChatGPTAuthError(RuntimeError):
    pass


class Pending(BaseModel):
    """One sign-in attempt, kept on disk between printing the address and pasting it back."""

    model_config = ConfigDict(frozen=True)

    client_id: str
    host_id: str
    state: str
    nonce: str
    verifier: str
    redirect_uri: str = REDIRECT_URI


class Credentials(BaseModel):
    model_config = ConfigDict(frozen=True)

    client_id: str
    host_id: str
    subject: str
    email: str = ""
    id_token: str
    access_token: str
    refresh_token: str
    expires_at: float
    scopes: list[str]

    @property
    def can_use_plan(self) -> bool:
        return PLAN_SCOPE in self.scopes


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode()).digest())


def _claims(id_token: str) -> dict[str, Any]:
    try:
        payload = id_token.split(".")[1]
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        return cast(dict[str, Any], json.loads(decoded))
    except (IndexError, ValueError) as exc:
        raise ChatGPTAuthError("unreadable ID token") from exc


def _write_private(path: Path, text: str) -> None:
    """Atomic, owner-only: a crash mid-write never leaves half a refresh token."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    tmp.replace(path)


class ChatGPTAuth:
    """Sign-in and the rotating credentials, in `folder` (mode 0700)."""

    def __init__(self, folder: Path, http: httpx.AsyncClient | None = None) -> None:
        self._folder = folder
        self._http = http
        self._lock = asyncio.Lock()
        self._current: Credentials | None = None
        self._stamp: int | None = None

    @property
    def credentials_path(self) -> Path:
        return self._folder / "credentials.json"

    @property
    def _pending_path(self) -> Path:
        return self._folder / "pending.json"

    def load(self) -> Credentials | None:
        """Read again when the file changed: he signs in from the app while Orion runs (D154)."""
        try:
            stamp = self.credentials_path.stat().st_mtime_ns
        except FileNotFoundError:
            self._current, self._stamp = None, None
            return None
        if self._current is None or stamp != self._stamp:
            try:
                self._current = Credentials.model_validate_json(
                    self.credentials_path.read_text("utf-8")
                )
            except ValidationError as exc:
                raise ChatGPTAuthError(f"unreadable {self.credentials_path}: {exc}") from exc
            self._stamp = stamp
        return self._current

    def start(self) -> str:
        """A fresh attempt: the address to open. Reuses the client Orion was issued, if any."""
        known = self.load()
        pending = Pending(
            client_id=known.client_id if known else FIRST_CLIENT,
            host_id=known.host_id if known else f"urn:uuid:{uuid.uuid4()}",
            state=secrets.token_urlsafe(24),
            nonce=secrets.token_urlsafe(24),
            verifier=secrets.token_urlsafe(64),
        )
        _write_private(self._pending_path, pending.model_dump_json())
        params = {
            "client_id": pending.client_id,
            "ext_agent_host_id": pending.host_id,
            "response_type": "code",
            "redirect_uri": pending.redirect_uri,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": pending.state,
            "nonce": pending.nonce,
            "code_challenge_method": "S256",
            "code_challenge": _challenge(pending.verifier),
        }
        if known is None:
            params["agent_name_hint"] = APP_NAME
        else:
            params["id_token_hint"] = known.id_token
            if known.email:
                params["login_hint"] = known.email
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def finish(self, redirected: str) -> Credentials:
        """From the address the browser was sent back to."""
        if not self._pending_path.exists():
            raise ChatGPTAuthError(
                "no sign-in in progress: start it from the app (Réglages > ChatGPT)"
            )
        pending = Pending.model_validate_json(self._pending_path.read_text("utf-8"))
        params = httpx.URL(redirected.strip()).params
        if params.get("state") != pending.state:
            raise ChatGPTAuthError("that address does not come from this sign-in")
        if params.get("error"):
            raise ChatGPTAuthError(f"sign-in refused ({params.get('error')})")
        code = params.get("code")
        if not code:
            raise ChatGPTAuthError("no code in that address")
        issued = params.get("client_id")
        if pending.client_id != FIRST_CLIENT and issued and issued != pending.client_id:
            raise ChatGPTAuthError("the sign-in came back for another client")
        client_id = issued or pending.client_id
        if client_id == FIRST_CLIENT:
            raise ChatGPTAuthError("OpenAI did not issue a client id")
        token = await self._token(
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": pending.verifier,
                "redirect_uri": pending.redirect_uri,
                "resource": RESOURCE,
            }
        )
        claims = self._checked(str(token.get("id_token", "")), client_id, pending.nonce)
        known = self.load()
        if known is not None and known.subject != claims["sub"]:
            log.warning("chatgpt.account_changed")
        credentials = self._credentials(token, client_id, pending.host_id, claims)
        self._pending_path.unlink(missing_ok=True)
        self._save(credentials)
        return credentials

    async def access_token(self) -> str:
        """A valid access token, refreshed (and the refresh token rotated) when near expiry."""
        async with self._lock:
            current = self.load()
            if current is None:
                raise ChatGPTAuthError("not signed in: sign in from the app (Réglages > ChatGPT)")
            if not current.can_use_plan:
                raise ChatGPTAuthError(
                    "ChatGPT plan use was not granted: sign in again from the app and accept it"
                )
            if current.expires_at - time.time() > REFRESH_MARGIN_SECONDS:
                return current.access_token
            token = await self._token(
                {
                    "grant_type": "refresh_token",
                    "client_id": current.client_id,
                    "refresh_token": current.refresh_token,
                    "resource": RESOURCE,
                }
            )
            if token.get("id_token"):  # a new one is checked like the first
                id_token = str(token["id_token"])
                claims = self._checked(id_token, current.client_id, None)
            else:
                id_token = current.id_token
                claims = _claims(id_token)
            if claims.get("sub") != current.subject:
                raise ChatGPTAuthError("refresh came back for another account")
            kept = {"refresh_token": current.refresh_token, "scope": " ".join(current.scopes)}
            refreshed = self._credentials(
                {**kept, **token, "id_token": id_token},
                current.client_id,
                current.host_id,
                claims,
            )
            self._save(refreshed)
            log.info("chatgpt.refreshed", expires_in=round(refreshed.expires_at - time.time()))
            return refreshed.access_token

    def expire(self) -> None:
        """The server refused the access token: the next call refreshes it."""
        if self._current is not None:
            self._current = self._current.model_copy(update={"expires_at": 0.0})

    def sign_out(self) -> bool:
        """Forgets the tokens; the plan stays linked until removed in ChatGPT's settings."""
        self._current = None
        gone = self.credentials_path.exists()
        self.credentials_path.unlink(missing_ok=True)
        self._pending_path.unlink(missing_ok=True)
        return gone

    async def _token(self, form: dict[str, str]) -> dict[str, Any]:
        http = self._http or httpx.AsyncClient(timeout=30.0)
        try:
            response = await http.post(TOKEN_URL, data=form)
        except httpx.HTTPError as exc:
            raise ChatGPTAuthError(f"token endpoint unreachable: {exc!r}") from exc
        finally:
            if self._http is None:
                await http.aclose()
        if response.status_code != 200:
            # The body names the OAuth error (invalid_grant…); it carries no token.
            raise ChatGPTAuthError(
                f"token endpoint refused ({response.status_code}): {response.text[:300]}"
            )
        try:
            return cast(dict[str, Any], response.json())
        except ValueError as exc:
            raise ChatGPTAuthError("token endpoint answered something that is not JSON") from exc

    @staticmethod
    def _checked(id_token: str, client_id: str, nonce: str | None) -> dict[str, Any]:
        """Issuer, audience, expiry and account; and the nonce of a sign-in (a refresh has
        none)."""
        claims = _claims(id_token)
        audience = claims.get("aud")
        audiences: list[object] = (
            cast(list[object], audience) if isinstance(audience, list) else [audience]
        )
        if claims.get("iss") not in (ISSUER, f"{ISSUER}/"):
            raise ChatGPTAuthError(f"ID token from an unexpected issuer: {claims.get('iss')}")
        if client_id not in audiences:
            raise ChatGPTAuthError("ID token issued for another client")
        if nonce is not None and claims.get("nonce") != nonce:
            raise ChatGPTAuthError("ID token does not answer this sign-in")
        if float(claims.get("exp", 0)) < time.time() - 60:
            raise ChatGPTAuthError("ID token already expired")
        if not claims.get("sub"):
            raise ChatGPTAuthError("ID token names no account")
        return claims

    @staticmethod
    def _credentials(
        token: dict[str, Any], client_id: str, host_id: str, claims: dict[str, Any]
    ) -> Credentials:
        try:
            return Credentials(
                client_id=client_id,
                host_id=host_id,
                subject=str(claims["sub"]),
                email=str(claims.get("email", "")),
                id_token=str(token["id_token"]),
                access_token=str(token["access_token"]),
                refresh_token=str(token["refresh_token"]),
                expires_at=time.time() + float(token.get("expires_in", 3600)),
                scopes=str(token.get("scope", "")).split(),
            )
        except KeyError as exc:
            raise ChatGPTAuthError(f"token answer lacks {exc}") from exc

    def _save(self, credentials: Credentials) -> None:
        _write_private(self.credentials_path, credentials.model_dump_json())
        self._current = credentials
        self._stamp = self.credentials_path.stat().st_mtime_ns
