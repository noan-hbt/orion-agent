"""The rest of his notebook in the app (D135): papers, people, the pages of his weeks, and
Orion's browser handed to him."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import pytest

from orion.channels.inbox import OwnerInbox
from orion.chronicle.module import WRITTEN, Chronicle, ChronicleSettings
from orion.computer.direct import BrowserDesk, HandOver
from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import SqliteJournal
from orion.files.module import FileStore
from orion.memory.model import Category
from orion.memory.service import MemoryService, Origin
from orion.memory.store import MemoryStore
from orion.mobile.channel import MobileChannel
from orion.mobile.devices import Devices, session_key
from orion.mobile.notebook import Notebook
from orion.mobile.server import mobile_app
from orion.papers.module import Vault
from orion.relations.module import BIRTHDAY, RHYTHM, Relations, RelationSettings
from orion.tools.base import ToolContext
from tests.fake_passkey import FakePasskey
from tests.fakes import ScriptedProvider
from tests.test_imessage import make_orion
from tests.test_mobile_channel import URL, FakePusher, World, pair
from tests.test_papers import Kept, render

PARIS = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 10, 26, 10, 0, tzinfo=PARIS)
TOLD = Origin(actor="owner:main", source="app:owner", trust=Trust.OWNER)
CTX = ToolContext("owner", "c1")


@pytest.fixture
async def shelf(journal: SqliteJournal, tmp_path: Path) -> AsyncIterator[tuple[World, Notebook]]:
    store = MemoryStore(":memory:")
    await store.open()
    memory = MemoryService(journal, store)
    notebook = Notebook(journal, lambda: NOW)
    devices = Devices(journal, URL, session_key(tmp_path / "m"))
    channel = MobileChannel(
        journal, OwnerInbox(make_orion(journal, ScriptedProvider())), devices, tmp_path / "m"
    )
    await channel.start()
    app = mobile_app(channel, None, secure_cookie=True, session_days=30, notebook=notebook)
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
        relations = Relations(
            journal, memory, RelationSettings(), "Europe/Paris", ["Noan"], None, lambda: NOW
        )
        notebook.relations = lambda: relations
        await memory.remember(
            "Paul Martin cherche du travail.", TOLD, about=["Paul Martin"], category=Category.PERSON
        )
        yield world, notebook
    await store.close()


async def test_nothing_is_shown_when_its_module_is_off(shelf: tuple[World, Notebook]) -> None:
    world, notebook = shelf
    notebook.relations = lambda: None
    for path in ("papers", "people", "chronicle"):
        assert (await world.http.get(f"/app/api/{path}")).status_code == 404
    assert (await world.http.get("/app/api/browser")).json() == {"handovers": []}
    world.http.cookies.clear()
    assert (await world.http.get("/app/api/papers")).status_code == 401


async def test_his_papers_are_found_and_opened(
    shelf: tuple[World, Notebook], journal: SqliteJournal, tmp_path: Path
) -> None:
    world, notebook = shelf
    vault = Vault(
        journal, FileStore(journal, tmp_path / "files"), cast(Any, Kept()), "Europe/Paris", render
    )
    notebook.papers = lambda: vault
    passport = await vault.file(
        "at_id", "Passeport", "passeport français", "Noan", None, date(2026, 12, 1), CTX
    )
    await vault.file("at_pay0", "Paie 09", "fiche de paie", "", date(2026, 9, 30), None, CTX)

    shown = (await world.http.get("/app/api/papers")).json()["papers"]
    assert {p["label"] for p in shown} == {"Passeport", "Paie 09"}
    found = (await world.http.get("/app/api/papers?q=passeport")).json()["papers"]
    assert [p["label"] for p in found] == ["Passeport"]
    assert found[0]["expiring"] and not found[0]["expired"] and found[0]["type"] == "image/png"

    opened = await world.http.get(f"/app/api/papers/{passport.id}/file")
    assert opened.status_code == 200 and opened.headers["content-type"] == "image/png"
    assert opened.content == Kept().files["at_id"][0]
    assert (await world.http.get("/app/api/papers/pa_nope/file")).status_code == 404


async def test_the_people_in_his_life_and_seeing_them(
    shelf: tuple[World, Notebook], journal: SqliteJournal
) -> None:
    world, notebook = shelf
    origin = Provenance(source="thread:owner", actor="orion:core")
    await journal.append(
        [
            NewEvent(
                kind=BIRTHDAY,
                payload={"person": "Paul Martin", "month": 11, "day": 2, "year": 1990},
                provenance=origin,
                trust=Trust.OWNER,
            ),
            NewEvent(
                kind=RHYTHM,
                payload={"person": "Paul Martin", "every_days": 14},
                provenance=origin,
                trust=Trust.OWNER,
            ),
        ]
    )
    (paul,) = (await world.http.get("/app/api/people")).json()["people"]
    assert paul["name"] == "Paul Martin" and paul["facts"] == ["Paul Martin cherche du travail."]
    assert paul["birthday"] == {"next": "2026-11-02", "in_days": 7, "age": 36}
    assert paul["every_days"] == 14 and paul["last_seen"] is None and not paul["overdue"]

    assert (
        await world.http.post("/app/api/people/saw", json={"name": "Paul Martin"})
    ).status_code == 200
    (paul,) = (await world.http.get("/app/api/people")).json()["people"]
    assert paul["last_seen"] == "2026-10-26" and paul["due"] == "2026-11-09"
    assert (
        await world.http.post("/app/api/people/saw", json={"name": "Inconnu"})
    ).status_code == 404
    relations = notebook.relations()
    assert relations is not None
    assert relations.state.contacts  # journaled as his, so Orion's rhythm counts from today


async def test_the_pages_of_his_weeks(
    shelf: tuple[World, Notebook], journal: SqliteJournal
) -> None:
    world, notebook = shelf
    chronicle = Chronicle(
        journal, ScriptedProvider(), None, ChronicleSettings(model="m"), "Europe/Paris"
    )  # type: ignore[arg-type]
    notebook.chronicle = lambda: chronicle
    for weeks, text in ((1, "Tu as fini le dossier."), (2, "Une semaine calme.")):
        await journal.append(
            [
                NewEvent(
                    kind=WRITTEN,
                    payload={
                        "week": (date(2026, 10, 26) - timedelta(weeks=weeks)).isoformat(),
                        "text": text,
                        "upto": 0,
                        "at": "",
                        "model": "m",
                    },
                    provenance=Provenance(source="orion:chronicle", actor="orion:chronicle"),
                    trust=Trust.SYSTEM,
                )
            ]
        )
    pages = (await world.http.get("/app/api/chronicle")).json()["pages"]
    assert [p["title"] for p in pages] == [
        "Ma semaine du 19 octobre 2026",
        "Ma semaine du 12 octobre 2026",
    ]
    assert pages[0]["text"] == "Tu as fini le dossier." and pages[0]["trusted"]


async def test_he_takes_orions_browser_in_hand_from_the_app(
    shelf: tuple[World, Notebook], journal: SqliteJournal
) -> None:
    world, notebook = shelf
    desk = BrowserDesk(journal, cast(Any, None))
    notebook.browser = lambda: desk
    done: list[tuple[str, dict[str, Any]]] = []

    async def on_tab(thread: str, action: Any) -> Any:
        class Tab:
            page = type(
                "P",
                (),
                {"viewport_size": {"width": 412, "height": 860}, "url": "https://ecole.test"},
            )()

            async def screenshot(self, full: bool) -> bytes:
                return b"\x89PNG"

            async def tap(self, x: float, y: float) -> None:
                done.append(("tap", {"x": x, "y": y}))

            async def settle(self) -> None:
                return None

            async def focused_field(self) -> None:
                return None

        return await action(Tab())

    desk.on_tab = on_tab  # type: ignore[method-assign]
    desk.tab = lambda thread: None  # type: ignore[method-assign]

    # Without Telegram, a hand-over goes to the app once it is there.
    desk._live["owner"] = cast(Any, None)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(Exception, match="needs the app"):
        await desk.hand_over("Connecte-toi à l'école", ToolContext("owner", "c1"))
    desk.in_app = True
    said = await desk.hand_over("Connecte-toi à l'école", ToolContext("owner", "c1"))
    assert "Prendre la main" in said
    (shown,) = (await world.http.get("/app/api/browser")).json()["handovers"]
    assert shown["why"] == "Connecte-toi à l'école"
    token = shown["token"]

    picture = await world.http.get(f"/app/api/browser/{token}/shot")
    assert picture.content == b"\x89PNG" and picture.headers["content-type"] == "image/png"
    tapped = (
        await world.http.post(f"/app/api/browser/{token}/act", json={"do": "tap", "x": 10, "y": 20})
    ).json()
    assert tapped == {"url": "", "width": 412, "height": 860} and done == [
        ("tap", {"x": 10.0, "y": 20.0})
    ]
    finished = (await world.http.post(f"/app/api/browser/{token}/act", json={"do": "done"})).json()
    assert finished == {"done": "Merci, je reprends."}
    assert (await world.http.get("/app/api/browser")).json() == {"handovers": []}
    assert (await world.http.get(f"/app/api/browser/{token}/shot")).status_code == 404

    desk.handovers["old"] = HandOver("owner", "trop tard", time.monotonic() - 1)
    assert (
        await world.http.post("/app/api/browser/old/act", json={"do": "tap"})
    ).status_code == 404
    assert json.dumps((await world.http.get("/app/api/browser")).json()) == '{"handovers": []}'
