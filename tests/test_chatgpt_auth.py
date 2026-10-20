from __future__ import annotations

import base64
import json
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from orion.providers.chatgpt_auth import (
    FIRST_CLIENT,
    ISSUER,
    PLAN_SCOPE,
    ChatGPTAuth,
    ChatGPTAuthError,
)


def _id_token(**claims: Any) -> str:
    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'RS256'})}.{part(claims)}.sig"


class _TokenServer:
    def __init__(self, nonce: str = "", scope: str = f"openid offline_access {PLAN_SCOPE}"):
        self.forms: list[dict[str, str]] = []
        self.nonce = nonce
        self.scope = scope
        self.counter = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.forms.append(form)
        self.counter += 1
        claims = {
            "iss": ISSUER,
            "aud": form["client_id"],
            "sub": "user-1",
            "email": "noan@example.com",
            "nonce": self.nonce,
            "exp": time.time() + 3600,
        }
        return httpx.Response(
            200,
            json={
                "id_token": _id_token(**claims),
                "access_token": f"access-{self.counter}",
                "refresh_token": f"refresh-{self.counter}",
                "expires_in": 3600,
                "scope": self.scope,
            },
        )


def _auth(tmp_path: Path, server: _TokenServer) -> ChatGPTAuth:
    http = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    return ChatGPTAuth(tmp_path / "chatgpt", http)


def _params(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(httpx.URL(url).query.decode()).items()}


async def test_sign_in_registers_then_stores_private_credentials(tmp_path: Path) -> None:
    server = _TokenServer()
    auth = _auth(tmp_path, server)
    params = _params(auth.start())
    assert params["client_id"] == FIRST_CLIENT
    assert params["agent_name_hint"] == "Orion"
    assert PLAN_SCOPE in params["scope"].split()
    assert params["code_challenge_method"] == "S256"
    assert params["ext_agent_host_id"].startswith("urn:uuid:")  # anything else is refused
    server.nonce = params["nonce"]

    redirected = (
        f"http://127.0.0.1:1455/auth/callback?code=abc&state={params['state']}&client_id=issued-1"
    )
    credentials = await auth.finish(redirected)

    assert credentials.client_id == "issued-1"
    assert credentials.can_use_plan
    assert server.forms[0]["grant_type"] == "authorization_code"
    assert server.forms[0]["code_verifier"]
    mode = stat.S_IMODE(auth.credentials_path.stat().st_mode)
    assert mode == 0o600
    assert await auth.access_token() == "access-1"

    # A second sign-in reuses the issued client, never the registration placeholder.
    again = _params(auth.start())
    assert again["client_id"] == "issued-1"
    assert "agent_name_hint" not in again


async def test_refresh_rotates_the_refresh_token(tmp_path: Path) -> None:
    server = _TokenServer()
    auth = _auth(tmp_path, server)
    params = _params(auth.start())
    server.nonce = params["nonce"]
    await auth.finish(
        f"http://127.0.0.1:1455/auth/callback?code=c&state={params['state']}&client_id=issued-1"
    )
    stale = auth.load()
    assert stale is not None
    auth._current = stale.model_copy(update={"expires_at": time.time()})  # pyright: ignore[reportPrivateUsage]

    assert await auth.access_token() == "access-2"
    assert server.forms[-1]["grant_type"] == "refresh_token"
    assert server.forms[-1]["refresh_token"] == "refresh-1"
    reloaded = ChatGPTAuth(tmp_path / "chatgpt").load()
    assert reloaded is not None and reloaded.refresh_token == "refresh-2"


async def test_foreign_or_partial_sign_ins_are_refused(tmp_path: Path) -> None:
    server = _TokenServer(scope="openid offline_access")
    auth = _auth(tmp_path, server)
    params = _params(auth.start())
    with pytest.raises(ChatGPTAuthError, match="this sign-in"):
        await auth.finish("http://127.0.0.1:1455/auth/callback?code=c&state=forged")
    with pytest.raises(ChatGPTAuthError, match="answer this sign-in"):
        await auth.finish(
            f"http://127.0.0.1:1455/auth/callback?code=c&state={params['state']}&client_id=i"
        )
    server.nonce = params["nonce"]
    credentials = await auth.finish(
        f"http://127.0.0.1:1455/auth/callback?code=c&state={params['state']}&client_id=i"
    )
    assert not credentials.can_use_plan
    with pytest.raises(ChatGPTAuthError, match="not granted"):
        await auth.access_token()
