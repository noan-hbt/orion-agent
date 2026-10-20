"""Secrets from the phone, straight to the guardian: the core never sees them (D94)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from orion.guardian.client import GuardianUnavailableError
from orion.guardian.service import GuardianService
from orion.guardian.telegram import TelegramGate
from orion.guardian.vault import SECRET_SET, Vault, load_vault, vault_path
from tests.fake_guardian import running_guardian
from tests.fake_telegram import OWNER, STRANGER, TOKEN, FakeTelegram
from tests.test_console import signed
from tests.test_telegram_guardian import split_config


async def test_the_owner_sets_a_secret_from_telegram_and_the_core_never_sees_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MAIL_PERSO_PASSWORD", raising=False)
    monkeypatch.setenv("SERVER_KEY", "x")
    config = split_config(tmp_path)
    config = config.model_copy(
        update={"guardian": config.guardian.model_copy(update={"secrets_url": "https://o.fr"})}
    )
    tg = FakeTelegram()
    vaults: list[Vault] = []

    def prepare(service: GuardianService) -> None:
        gate = TelegramGate(service, tg.client())
        gate.install()
        vaults.append(Vault(service, gate, set()))
        vaults[0].install()

    async with running_guardian(config, prepare=prepare) as g:
        with pytest.raises(GuardianUnavailableError, match="set on the server"):
            await g.client.ask_secret("SERVER_KEY", "x")
        await g.client.ask_secret("MAIL_PERSO_PASSWORD", "le mot de passe de ta boîte perso")
        [asked] = tg.sent()
        url = asked["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"]
        assert url.startswith("https://o.fr/secrets/?t=")
        token = str(parse_qs(urlparse(str(url)).query)["t"][0])
        public = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=vaults[0].public_app()), base_url="https://o.fr"
        )
        page = await public.get(f"/secrets/?t={token}")
        assert "MAIL_PERSO_PASSWORD" in page.text and "boîte perso" in page.text
        now = time.time()
        body: dict[str, str] = {"token": token, "value": "s3cret"}
        stranger = await public.post(
            "/secrets/set", json={**body, "init": signed(TOKEN, STRANGER, now)}
        )
        assert stranger.status_code == 403
        done = await public.post("/secrets/set", json={**body, "init": signed(TOKEN, OWNER, now)})
        assert done.status_code == 200
        again = await public.post("/secrets/set", json={**body, "init": signed(TOKEN, OWNER, now)})
        assert again.status_code == 410  # one time
        await public.aclose()
        assert vault_path(config).stat().st_mode & 0o777 == 0o600
        assert "MAIL_PERSO_PASSWORD" in tg.sent()[-1]["text"]  # he is told
        ledger = [e for e in await g.journal.read() if e.kind == SECRET_SET]
        assert ledger[0].payload == {"name": "MAIL_PERSO_PASSWORD"}  # the name, never the value
        assert "s3cret" not in repr([e.payload for e in await g.journal.read()])
    monkeypatch.delenv("MAIL_PERSO_PASSWORD")
    assert load_vault(config) == {"MAIL_PERSO_PASSWORD"}
    assert os.environ["MAIL_PERSO_PASSWORD"] == "s3cret"
    monkeypatch.delenv("MAIL_PERSO_PASSWORD")
