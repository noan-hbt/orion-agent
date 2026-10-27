"""Nothing left to do on the server (D154): what needed `orion …` there is done in the app,
the changes that matter signed with Face ID for that very change."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx
import pytest

from orion.core.journal import SqliteJournal
from orion.guardian.app import AppEdge
from orion.guardian.client import GuardianUnavailableError
from orion.guardian.federation import ADMIN_HEADER
from orion.guardian.service import GuardianService
from orion.guardian.vault import CORE_VAULT, SECRET_SET, Vault, vault_path
from orion.hands.desk import HandsDesk
from orion.library.module import Library
from orion.library.store import LibraryStore
from orion.mobile.server import COOKIE
from orion.mobile.upkeep import GuardianSigner, Upkeep, UpkeepError
from orion.places.module import Places
from orion.setup import load_core_vault, read_env
from tests.fake_guardian import guardian_config, running_guardian
from tests.fake_passkey import FakePasskey
from tests.test_mobile_split import URL, edge_of

CSV = "name,url,username,password\nImpots,https://impots.gouv.fr,noan,pw-1\n"


async def test_his_secrets_logins_and_machines_from_the_app(
    journal: SqliteJournal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("MAIL_PERSO_PASSWORD", "OPENROUTER_API_KEY"):
        monkeypatch.setenv(name, "")  # restored after: the vault sets them
    config = guardian_config(
        tmp_path, modules={"mobile": {"enabled": True, "public_url": URL, "notifications": False}}
    ).model_copy(update={"source": tmp_path / "config" / "orion.toml"})

    def prepare(service: GuardianService) -> None:
        edge = AppEdge(service)
        edge.install()
        edge.vault = Vault(service, None, set())  # no Telegram: the app only
        edge.vault.install()

    async with running_guardian(config, prepare=prepare) as guardian:
        edge = guardian.service.app_edge
        assert edge is not None
        made = await guardian.client.post(
            "/v1/app/pair-code", {"device": "iphone"}, {ADMIN_HEADER: edge.admin_token}
        )
        phone = FakePasskey(URL, "orion.example")
        async with edge_of(guardian.service) as app:
            started = (await app.post("/app/pair/options", json={"code": made["code"]})).json()
            await app.post(
                "/app/pair",
                json={
                    "ceremony": started["ceremony"],
                    "credential": phone.create(json.dumps(started["options"])),
                },
            )
            assert COOKIE in app.cookies

            async def keep(what: str, args: dict[str, str], sent: dict[str, str]) -> httpx.Response:
                opened = (await app.post(f"/app/keep/{what}/options", json={"args": args})).json()
                return await app.post(
                    f"/app/keep/{what}",
                    json={
                        "args": sent,
                        "ceremony": opened["ceremony"],
                        "credential": phone.sign(json.dumps(opened["options"])),
                    },
                )

            # A secret: signed for this value, never another one.
            secret = {"name": "MAIL_PERSO_PASSWORD", "value": "s3cret"}
            forged = await keep("secret", secret, {**secret, "value": "autre"})
            assert forged.status_code == 403
            done = await keep("secret", secret, secret)
            assert done.json() == {"set": "MAIL_PERSO_PASSWORD", "where": "guardian"}
            assert read_env(vault_path(config)) == {"MAIL_PERSO_PASSWORD": "s3cret"}
            ledger = await guardian.journal.read(0, 10_000)
            assert [e.payload["name"] for e in ledger if e.kind == SECRET_SET] == [
                "MAIL_PERSO_PASSWORD"
            ]
            assert "s3cret" not in repr([e.payload for e in ledger])
            # The core's own key goes beside orion.toml, for the core to read.
            core = {"name": "OPENROUTER_API_KEY", "value": "sk-new"}
            assert (await keep("secret", core, core)).json()["where"] == "core"
            path = tmp_path / "config" / CORE_VAULT
            assert stat.S_IMODE(path.stat().st_mode) == 0o640
            assert load_core_vault(path.parent) == ["OPENROUTER_API_KEY"]

            # Orion asks for one: no Telegram, it shows in the app.
            await guardian.client.ask_secret("BRAVE_SEARCH_API_KEY", "la recherche web")
            kept = (await app.get("/app/keep")).json()
            assert kept["asked"] == [{"name": "BRAVE_SEARCH_API_KEY", "why": "la recherche web"}]
            assert {"name": "MAIL_PERSO_PASSWORD", "where": "app"} in kept["secrets"]

            imported = (await app.post("/app/logins/import", json={"text": CSV})).json()
            assert imported["kept"] == [{"name": "impots", "sites": ["impots.gouv.fr"]}]

        # A machine: the core asks the guardian, which signs only the core's purposes.
        with pytest.raises(GuardianUnavailableError):
            await guardian.client.post("/v1/app/sign", {"purpose": "approval:a1:yes"})
        upkeep = Upkeep(
            journal, config, URL, hands=lambda: desk, signer=GuardianSigner(guardian.client)
        )
        desk = HandsDesk(journal)
        ceremony, options = await upkeep.machine_options("mac")
        with pytest.raises(UpkeepError):  # signed for « mac », not for another machine
            await upkeep.machine_pair("pc", ceremony, phone.sign(json.dumps(options)))
        ceremony, options = await upkeep.machine_options("mac")
        paired = await upkeep.machine_pair("mac", ceremony, phone.sign(json.dumps(options)))
        assert paired["command"] == f"orion hand pair {URL} {paired['code']} --root ~/Documents"


async def test_the_phone_token_and_his_notes_from_the_app(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    store = LibraryStore(tmp_path / "library.sqlite3")
    await store.open()
    places = Places(journal)
    library = Library(journal, store, None)
    upkeep = Upkeep(
        journal,
        guardian_config(tmp_path),
        URL,
        places=lambda: places,
        library=lambda: library,
    )
    try:
        token = await upkeep.phone_token()
        assert token["event_url"] == f"{URL}/places/event"
        assert places.state.token  # its digest: the phone's automations work with it
        notes = [("Voyage Lisbonne", "Hôtel près de l'Alfama, vol le 12 à 7 h."), ("vide", " ")]
        assert await upkeep.library_import(notes) == {
            "added": ["Voyage Lisbonne"],
            "skipped": ["vide (vide, ou trop long)"],
        }
        again = await upkeep.library_import(notes[:1])
        assert again["skipped"] == ["Voyage Lisbonne (déjà dans la bibliothèque)"]
    finally:
        await store.close()
