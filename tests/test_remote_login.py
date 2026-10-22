"""Signing in from the owner's phone, in the guardian's own browser (D112): no second install,
SMS codes and captchas are his to do, and only the session is kept."""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from playwright.async_api import Route

from orion.computer.browser import Session
from orion.guardian.client import GuardianUnavailableError
from orion.guardian.ledger import SESSION_ADDED
from orion.guardian.remote import RemoteLogin
from orion.guardian.service import GuardianService
from orion.guardian.telegram import TelegramGate
from orion.guardian.vault import Vault
from orion.web.fetch import FetchError
from tests.fake_guardian import running_guardian
from tests.fake_telegram import OWNER, STRANGER, TOKEN, FakeTelegram
from tests.test_console import signed
from tests.test_telegram_guardian import split_config

PASSWORD = "Tr0ub4dor&3"
SIGN_IN = """<html><head><title>Connexion</title></head><body>
<form method="post" action="/login" style="margin:0">
<input name="pw" type="password" style="display:block;margin:0;width:100%;height:100px">
<button type="submit">Se connecter</button></form></body></html>"""


class Account:
    def __init__(self) -> None:
        self.posted: list[str] = []

    async def handle(self, route: Route) -> None:
        request = route.request
        if request.method == "POST":
            self.posted.append(request.post_data or "")
            await route.fulfill(
                status=200,
                content_type="text/html",
                headers={"set-cookie": "sid=s3ss10n; Path=/; Secure"},
                body="<html><title>Mon compte</title><body>Bienvenue</body></html>",
            )
            return
        await route.fulfill(status=200, content_type="text/html", body=SIGN_IN)


async def only_the_account(host: str, _port: int) -> None:
    if host != "www.compte.test":
        raise FetchError(f"{host} is not a public address")


def browser_for(account: Account) -> Session:
    return Session(False, only_the_account, {"www.compte.test": account.handle})


async def test_the_owner_signs_in_from_his_phone_and_the_guardian_keeps_the_session(
    tmp_path: Path,
) -> None:
    config = split_config(tmp_path)
    config = config.model_copy(
        update={"guardian": config.guardian.model_copy(update={"secrets_url": "https://o.fr"})}
    )
    tg = FakeTelegram()
    account = Account()
    parts: list[Vault | RemoteLogin] = []

    def prepare(service: GuardianService) -> None:
        gate = TelegramGate(service, tg.client())
        gate.install()
        vault = Vault(service, gate, set())
        remote = RemoteLogin(service, gate, vault.base, lambda: browser_for(account))
        remote.install(vault)
        vault.install()
        parts.extend([vault, remote])

    async with running_guardian(config, prepare=prepare) as g:
        with pytest.raises(GuardianUnavailableError, match="lowercase name"):
            await g.client.ask_session("Compte!", "https://www.compte.test/", "")
        await g.client.ask_session("compte", "https://www.compte.test/", "pour ta commande")
        [asked] = tg.sent()
        assert "compte.test" in asked["text"]
        url = str(asked["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"])
        assert url.startswith("https://o.fr/secrets/browse/?t=")
        token = parse_qs(urlparse(url).query)["t"][0]
        vault, remote = parts
        assert isinstance(vault, Vault) and isinstance(remote, RemoteLogin)
        public = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=vault.public_app()), base_url="https://o.fr"
        )
        try:
            assert (
                "Connexion à compte.test" in (await public.get(f"/secrets/browse/?t={token}")).text
            )
            assert (await public.get(f"/secrets/browse/shot?t={token}")).status_code == 404

            async def act(who: int, **body: object) -> httpx.Response:
                sent = {"token": token, "init": signed(TOKEN, who, time.time()), **body}
                return await public.post("/secrets/browse/act", json=sent)

            assert (await act(STRANGER, do="start")).status_code == 403
            started = await act(OWNER, do="start", width=390, height=640)
            assert started.json()["url"] == "https://www.compte.test/"
            shot = await public.get(f"/secrets/browse/shot?t={token}")
            assert shot.headers["content-type"] == "image/png" and shot.content[:4] == b"\x89PNG"
            assert (await act(OWNER, do="tap", x=50, y=50)).status_code == 200  # the field
            assert (await act(OWNER, do="type", text=PASSWORD)).status_code == 200
            assert (await act(OWNER, do="key", key="Enter")).status_code == 200
            assert account.posted == ["pw=Tr0ub4dor%263"]
            done = await act(OWNER, do="done")
            assert done.json()["done"].startswith("Session gardée")
            assert (await act(OWNER, do="tap", x=1, y=1)).status_code == 410  # over
        finally:
            await public.aclose()
            await remote.aclose()

        kept = json.loads((config.guardian.data_dir / "sessions" / "compte.json").read_text())
        assert [c["value"] for c in kept["cookies"]] == ["s3ss10n"]
        assert kept["orion"]["phone"] == {"width": 390, "height": 640}
        assert kept["orion"]["url"] == "https://www.compte.test/"  # the page Orion chose
        assert "Android" in kept["orion"]["user_agent"]
        assert g.service.ledger.state.sessions["compte"] == ["compte.test"]
        assert "session « compte »" in tg.sent()[-1]["text"]
        events = await g.journal.read()
        assert [e.kind for e in events].count(SESSION_ADDED) == 1
        assert PASSWORD not in repr([e.payload for e in events])

    # The core's browser uses it as it was made: the same phone, the same cookies.
    again = browser_for(account)
    again.storage_state = kept
    async with again as browser:
        await browser.goto("https://www.compte.test/")
        assert browser.page is not None
        assert await browser.page.evaluate("screen.width") == 390
        assert "Android" in await browser.page.evaluate("navigator.userAgent")
        cookies = await browser.page.context.cookies()
        assert [c.get("value") for c in cookies] == ["s3ss10n"]
