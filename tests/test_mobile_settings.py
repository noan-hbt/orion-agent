"""orion.toml changed from the app, signed with Face ID for the exact change (D135): written
without the model, so a conversation holding outside content does not stand in the way."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from orion.channels.inbox import OwnerInbox
from orion.config import load_config
from orion.configure.module import CHANGED, Configurator, SignedSettings
from orion.core.journal import SqliteJournal
from orion.mobile.channel import MobileChannel
from orion.mobile.devices import Devices, session_key
from orion.mobile.server import mobile_app
from tests.fake_passkey import FakePasskey
from tests.fakes import ScriptedProvider
from tests.test_imessage import make_orion
from tests.test_mobile_channel import URL, FakePusher, World, pair

TOML = """# Orion, chez Noan
instance = "orion-main"
data_dir = "data"

[initiative]
quiet_start = "22:30:00"  # pas après
"""


@pytest.fixture
async def signed(
    journal: SqliteJournal, tmp_path: Path
) -> AsyncIterator[tuple[World, Path, list[bool]]]:
    source = tmp_path / "orion.toml"
    source.write_text(TOML, "utf-8")
    configurator = Configurator(journal, load_config(source).source)
    restarted: list[bool] = []

    async def restart() -> None:
        restarted.append(True)

    configurator.restart = restart
    devices = Devices(journal, URL, session_key(tmp_path / "m"))
    channel = MobileChannel(
        journal, OwnerInbox(make_orion(journal, ScriptedProvider())), devices, tmp_path / "m"
    )
    await channel.start()
    app = mobile_app(channel, None, True, 30, settings=SignedSettings(configurator))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=URL, headers={"origin": URL}
    ) as http:
        world = World(
            channel,
            http,
            FakePasskey(URL, "orion.example"),
            ScriptedProvider(),
            FakePusher(),
            devices,
        )
        await pair(world)
        yield world, source, restarted


async def test_a_change_signed_in_the_app_is_written_as_his(
    signed: tuple[World, Path, list[bool]], journal: SqliteJournal
) -> None:
    world, source, restarted = signed
    http = world.http
    change = {"changes": [{"path": "initiative.quiet_start", "value": "23:00:00"}]}
    opened = (await http.post("/app/settings/options", json=change)).json()
    done = await http.post(
        "/app/settings",
        json={
            **change,
            "ceremony": opened["ceremony"],
            "credential": world.phone.sign(json.dumps(opened["options"])),
        },
    )
    assert done.status_code == 200 and done.json() == {"changed": True}
    text = source.read_text("utf-8")
    assert 'quiet_start = "23:00:00"' in text and "# Orion, chez Noan" in text
    events = [e for e in await journal.read(0, 500) if e.kind == CHANGED]
    assert events[-1].provenance.actor == "owner:iphone"
    await asyncio.sleep(0)
    assert restarted == [True]


async def test_a_signature_answers_one_change_only(signed: tuple[World, Path, list[bool]]) -> None:
    world, source, _restarted = signed
    http = world.http
    asked = {"changes": [{"path": "initiative.quiet_start", "value": "23:00:00"}]}
    opened = (await http.post("/app/settings/options", json=asked)).json()
    other = {"changes": [{"path": "budget.daily_usd", "value": 100}]}
    swapped = await http.post(
        "/app/settings",
        json={
            **other,
            "ceremony": opened["ceremony"],
            "credential": world.phone.sign(json.dumps(opened["options"])),
        },
    )
    assert swapped.status_code == 403
    assert source.read_text("utf-8") == TOML

    # What is never changed by message is not changed from the app either, nor asked to sign.
    secret = {"changes": [{"path": "providers.openrouter.api_key", "value": "sk"}]}
    assert (await http.post("/app/settings/options", json=secret)).status_code == 422
    broken = {"changes": [{"path": "initiative.quiet_start", "value": "tard"}]}
    assert (await http.post("/app/settings/options", json=broken)).status_code == 422


async def test_orion_restarts_when_the_file_changes_under_it(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    source = tmp_path / "orion.toml"
    source.write_text(TOML, "utf-8")
    configurator = Configurator(journal, source)
    restarted: list[bool] = []

    async def restart() -> None:
        restarted.append(True)

    configurator.restart = restart
    watching = asyncio.create_task(configurator.watch(every=0.01))
    await asyncio.sleep(0.05)
    assert not restarted
    source.write_text(TOML.replace("22:30", "23:30"), "utf-8")  # the guardian wrote it
    await asyncio.wait_for(watching, 1)
    await asyncio.sleep(0)
    assert restarted == [True]


async def test_split_the_guardian_writes_what_he_signed(tmp_path: Path) -> None:
    from orion.guardian.app import AppEdge
    from orion.guardian.federation import ADMIN_HEADER
    from orion.guardian.ledger import CONFIG_CHANGED
    from orion.guardian.service import GuardianService
    from tests.fake_guardian import running_guardian
    from tests.fake_inkbox import OWNER as OWNER_NUMBER
    from tests.test_configure import SPLIT_TOML
    from tests.test_mobile_split import edge_of

    source = tmp_path / "orion.toml"
    toml = SPLIT_TOML.format(owner=OWNER_NUMBER) + (
        '\n[modules.mobile]\nenabled = true\npublic_url = "https://orion.example"\n'
        "notifications = false\n"
    )
    source.write_text(toml, "utf-8")
    config = load_config(source)
    restarted: list[bool] = []

    async def restart() -> None:
        restarted.append(True)

    def prepare(service: GuardianService) -> None:
        AppEdge(service).install()
        service.restart = restart

    async with running_guardian(config, prepare=prepare) as g:
        edge = g.service.app_edge
        assert edge is not None
        made = await g.client.post(
            "/v1/app/pair-code", {"device": "iphone"}, {ADMIN_HEADER: edge.admin_token}
        )
        phone = FakePasskey("https://orion.example", "orion.example")
        async with edge_of(g.service) as app:
            started = (await app.post("/app/pair/options", json={"code": made["code"]})).json()
            await app.post(
                "/app/pair",
                json={
                    "ceremony": started["ceremony"],
                    "credential": phone.create(json.dumps(started["options"])),
                },
            )
            # What protects him (the budget) is his to change: his signature is the yes.
            change = {"changes": [{"path": "budget.daily_usd", "value": 4}]}
            opened = (await app.post("/app/settings/options", json=change)).json()
            done = await app.post(
                "/app/settings",
                json={
                    **change,
                    "ceremony": opened["ceremony"],
                    "credential": phone.sign(json.dumps(opened["options"])),
                },
            )
            assert done.json() == {"changed": True}, done.text
        assert load_config(source).budget.daily_usd == 4
        recorded = [e for e in await g.journal.read(0, 500) if e.kind == CONFIG_CHANGED]
        assert recorded[-1].payload["signed_by"] == "passkey:iphone"
        await asyncio.sleep(0)
        assert restarted == [True]
