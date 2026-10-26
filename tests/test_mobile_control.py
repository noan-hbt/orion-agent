"""The app's controls (D125): what Orion knows and does, and the few things he changes there."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from orion.awaiting.module import Awaiting, AwaitingSettings
from orion.channels.inbox import OwnerInbox
from orion.config import load_config
from orion.core.events import Provenance, Trust
from orion.core.journal import SqliteJournal
from orion.guardian.autonomy import Autonomy
from orion.initiative.engine import InitiativeSettings
from orion.memory.model import CommitmentStatus, SettingKey
from orion.memory.service import MemoryService, Origin
from orion.memory.store import MemoryStore
from orion.mobile.channel import MobileChannel
from orion.mobile.control import Control
from orion.mobile.devices import Devices, session_key
from orion.mobile.server import mobile_app
from orion.modules.base import Planned
from orion.tasks.service import Tasks
from tests.fake_passkey import FakePasskey
from tests.fakes import ScriptedProvider
from tests.test_imessage import make_orion
from tests.test_mobile_channel import URL, FakePusher, World, pair

NOW = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)
TOLD = Origin(actor="orion:core", source="app:owner", trust=Trust.OWNER)
READ = Origin(actor="orion:core", source="web:annonce", trust=Trust.WEB)


@dataclass
class Controlled:
    world: World
    memory: MemoryService
    autonomy: Autonomy
    journal: SqliteJournal
    tasks: Tasks
    awaiting: Awaiting
    control: Control


@pytest.fixture
async def controlled(journal: SqliteJournal, tmp_path: Path) -> AsyncIterator[Controlled]:
    store = MemoryStore(":memory:")
    await store.open()
    memory = MemoryService(journal, store)
    autonomy = Autonomy(journal, ["calendar_add", "mail_send"])

    async def planned(_now: datetime, _until: datetime) -> list[Planned]:
        return [Planned(NOW + timedelta(hours=3), "te demander comment s'est passé l'entretien")]

    tasks = Tasks(journal)
    awaiting = Awaiting(journal, AwaitingSettings(), "UTC", clock=lambda: NOW)
    control = Control(
        journal,
        memory,
        InitiativeSettings(timezone="UTC"),
        autonomy=autonomy,
        planned=planned,
        clock=lambda: NOW,
        tasks=lambda: tasks,
        awaiting=lambda: awaiting,
    )
    orion = make_orion(journal, ScriptedProvider())
    devices = Devices(journal, URL, session_key(tmp_path / "m"))
    channel = MobileChannel(journal, OwnerInbox(orion), devices, tmp_path / "m")
    await channel.start()
    app = mobile_app(channel, None, secure_cookie=True, session_days=30, control=control)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=URL, headers={"origin": URL}
    ) as http:
        phone = FakePasskey(URL, "orion.example")
        world = World(channel, http, phone, ScriptedProvider(), FakePusher(), devices)
        await pair(world)
        yield Controlled(world, memory, autonomy, journal, tasks, awaiting, control)
    await store.close()


async def test_only_signed_in(controlled: Controlled) -> None:
    http = controlled.world.http
    http.cookies.clear()
    assert (await http.get("/app/api/control")).status_code == 401
    assert (await http.post("/app/api/control/pause", json={"minutes": 60})).status_code == 401


async def test_he_pauses_orion_and_resumes_it(controlled: Controlled) -> None:
    http = controlled.world.http
    paused = (await http.post("/app/api/control/pause", json={"minutes": 120})).json()
    assert paused["paused_until"] == (NOW + timedelta(hours=2)).isoformat()
    overview = (await http.get("/app/api/control")).json()
    assert overview["paused_until"] == paused["paused_until"]
    change = (await controlled.memory.store.settings())[SettingKey.PAUSED_UNTIL]
    assert change.reason == "pause depuis l'app"

    await http.post("/app/api/control/pause", json={"minutes": None})
    assert (await http.get("/app/api/control")).json()["paused_until"] is None
    huge = (await http.post("/app/api/control/pause", json={"minutes": 10**7})).json()
    assert huge["paused_until"] == (NOW + timedelta(days=30)).isoformat()


async def test_he_sets_quiet_hours_and_the_budget(controlled: Controlled) -> None:
    http = controlled.world.http
    shown = (
        await http.post(
            "/app/api/control/settings",
            json={"quiet_start": "23:00", "quiet_end": "07:30", "daily_budget": 5},
        )
    ).json()
    assert (shown["quiet_start"], shown["quiet_end"], shown["daily_budget"]) == (
        "23:00",
        "07:30",
        5,
    )
    bad = await http.post("/app/api/control/settings", json={"daily_budget": 500})
    assert bad.status_code == 404
    assert (
        await http.post("/app/api/control/settings", json={"quiet_end": "tard"})
    ).status_code == 404


async def test_he_reads_and_corrects_what_orion_knows(controlled: Controlled) -> None:
    http = controlled.world.http
    memory = controlled.memory
    loyer = await memory.remember("Son loyer est de 520 € par mois", TOLD)
    await memory.remember("L'annonce dit 42 m²", READ)
    await memory.add_commitment("Rappeler la banque", TOLD, debtor="owner", creditor="Banque")
    await memory.learn_rule("Réponses courtes, sans liste", TOLD)

    facts = (await http.get("/app/api/memory")).json()["facts"]
    assert [f["statement"] for f in facts] == [
        "L'annonce dit 42 m²",
        "Son loyer est de 520 € par mois",
    ]
    assert [f["from_owner"] for f in facts] == [False, True]
    found = (await http.get("/app/api/memory?q=loyer")).json()["facts"]
    assert [f["id"] for f in found] == [loyer]

    assert (await http.post(f"/app/api/memory/{loyer}/forget")).status_code == 200
    assert (await http.post(f"/app/api/memory/{loyer}/forget")).status_code == 404
    assert len((await http.get("/app/api/memory")).json()["facts"]) == 1
    assert len((await http.get("/app/api/memory?past=1")).json()["facts"]) == 2

    [commitment] = (await http.get("/app/api/commitments")).json()["commitments"]
    assert (await http.post(f"/app/api/commitments/{commitment['id']}/done")).status_code == 200
    closed = await memory.store.commitment(commitment["id"])
    assert closed is not None and closed.status is CommitmentStatus.DONE

    [rule] = (await http.get("/app/api/rules")).json()["rules"]
    assert (await http.post(f"/app/api/rules/{rule['id']}/retire")).status_code == 200
    assert (await http.get("/app/api/rules")).json()["rules"] == []


async def test_he_takes_autonomy_back_and_sees_what_happened(controlled: Controlled) -> None:
    http = controlled.world.http
    await controlled.autonomy.grant("calendar_add", None, None)
    actions = (await http.get("/app/api/autonomy")).json()["actions"]
    assert [(a["action"], a["alone"]) for a in actions] == [
        ("calendar_add", True),
        ("mail_send", False),
    ]
    overview = (await http.get("/app/api/control")).json()
    assert overview["alone"] == ["ajouter des événements à ton agenda"]
    assert overview["next"][0]["what"] == "te demander comment s'est passé l'entretien"

    assert (await http.post("/app/api/autonomy/calendar_add/revoke")).status_code == 200
    assert (await http.post("/app/api/autonomy/call_someone/revoke")).status_code == 404
    activity = (await http.get("/app/api/activity")).json()["activity"]
    assert [a["what"] for a in activity] == ["autonomie retirée", "autonomie accordée"]
    assert (await http.get("/app/api/planned")).json()["planned"][0]["undoable"] is False


async def test_for_you_gathers_what_waits_on_him(controlled: Controlled) -> None:
    http = controlled.world.http
    orion = Provenance(source="orion", actor="orion:core")
    later = NOW + timedelta(hours=4)
    asked = await controlled.awaiting.wait(
        "owner", "", "le jour de la visite", later, "", orion, Trust.SYSTEM, None
    )
    paul = await controlled.awaiting.wait(
        "contact", "Paul", "le dîner de samedi", later, "", orion, Trust.SYSTEM, None
    )
    task = await controlled.tasks.start("Trouver un T2 à Dreux", "brief", 0.5, 60, NOW, orion, None)

    shown = (await http.get("/app/api/for-you")).json()
    assert [(q["about"], q["who"]) for q in shown["questions"]] == [("le jour de la visite", "lui")]
    assert [(w["about"], w["who"]) for w in shown["waiting_on"]] == [("le dîner de samedi", "Paul")]
    assert [t["goal"] for t in shown["tasks"]] == ["Trouver un T2 à Dreux"]

    assert (await http.post(f"/app/api/waits/{paul.id}/drop")).status_code == 200
    assert (await http.post(f"/app/api/waits/{paul.id}/drop")).status_code == 404
    assert (await http.post(f"/app/api/tasks/{task}/stop")).status_code == 200
    assert (await http.post(f"/app/api/tasks/{task}/stop")).status_code == 404
    stopped = await controlled.tasks.get(task)
    assert stopped is not None and stopped.finished == "stopped"
    after = (await http.get("/app/api/for-you")).json()
    assert after["waiting_on"] == [] and after["tasks"] == []
    assert [q["id"] for q in after["questions"]] == [asked.id]


async def test_the_server_part_shows_settings_with_whose_yes_and_the_modules(
    controlled: Controlled, tmp_path: Path
) -> None:
    http = controlled.world.http
    assert (await http.get("/app/api/server")).status_code == 404  # no config: not here

    source = tmp_path / "orion.toml"
    source.write_text(
        'instance = "orion-main"\ndata_dir = "data"\n\n[initiative]\nquiet_start = "22:30:00"\n'
        "\n[modules.web]\nenabled = false\n",
        "utf-8",
    )
    controlled.control.config = load_config(source)
    shown = (await http.get("/app/api/server")).json()
    by_path = {s["path"]: s for s in shown["settings"]}
    assert by_path["initiative.quiet_start"] == {
        "path": "initiative.quiet_start",
        "value": "22:30:00",
        "tier": "orion",
    }
    assert by_path["modules.web.enabled"]["tier"] == "orion"
    assert all(s["tier"] == "owner" for s in shown["settings"] if s["path"].startswith("budget."))
    assert not any(s["path"].endswith(("api_key", "base_url", "_dir")) for s in shown["settings"])
    web = next(m for m in shown["modules"] if m["name"] == "web")
    assert web == {"name": "web", "on": False, "added": False}
    assert shown["version"] and shown["split"] is False
    assert all("description" in entry for entry in shown["catalog"])
