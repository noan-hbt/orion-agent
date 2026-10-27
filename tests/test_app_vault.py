"""His vault in the app (D142): the logins listed, kept and let go there; Orion's requests
answered there, filled by the phone's own passwords; a sign-in form in a hand-over filled the
same way, and offered to keep at once."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from orion.channels.inbox import OwnerInbox
from orion.computer.direct import BrowserDesk, browser_tools
from orion.computer.vault import LocalVault
from orion.config import OrionConfig
from orion.core.journal import SqliteJournal
from orion.guardian.app import AppEdge
from orion.guardian.ledger import LOGIN_ADDED, LOGIN_REMOVED
from orion.guardian.logins import LoginDesk, read_login
from orion.guardian.service import GuardianService
from orion.mobile.channel import MobileChannel
from orion.mobile.devices import Devices, session_key
from orion.mobile.notebook import Notebook
from orion.mobile.server import mobile_app
from orion.tools.base import ToolContext
from tests.fake_guardian import guardian_config, running_guardian
from tests.fake_passkey import FakePasskey
from tests.fakes import ScriptedProvider
from tests.test_imessage import make_orion
from tests.test_logins import Account, browser
from tests.test_mobile_channel import URL, FakePusher, World, pair

PASSWORD = "S3cret!pw"
CTX = ToolContext("owner", "t1")


def config_for(tmp_path: Path) -> OrionConfig:
    return OrionConfig.model_validate(
        {"data_dir": tmp_path, "guardian": {"data_dir": tmp_path / "guardian"}}
    )


@pytest.fixture
async def app(
    journal: SqliteJournal, tmp_path: Path
) -> AsyncIterator[tuple[World, BrowserDesk, list[str]]]:
    """The app in one process, with Orion's browser and his own vault; Telegram there too."""
    account = Account()
    desk = BrowserDesk(journal, lambda: browser(account), vault=LocalVault(config_for(tmp_path)))
    desk.in_app = True
    buttons: list[str] = []

    async def notify(text: str, url: str) -> None:
        buttons.append(text)

    desk.notify, desk.base_url = notify, "https://o.fr/console/browser"
    devices = Devices(journal, URL, session_key(tmp_path / "m"))
    channel = MobileChannel(
        journal, OwnerInbox(make_orion(journal, ScriptedProvider())), devices, tmp_path / "m"
    )
    await channel.start()
    notebook = Notebook(journal, lambda: datetime.now(UTC), browser=lambda: desk)
    served = mobile_app(
        channel,
        None,
        True,
        30,
        notebook=notebook,
        logins=lambda: desk.vault,  # pyright: ignore[reportArgumentType]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=served), base_url=URL, headers={"origin": URL}
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
        try:
            yield world, desk, buttons
        finally:
            await desk.aclose()


async def test_his_vault_is_kept_from_the_app_and_never_shows_a_password(
    app: tuple[World, BrowserDesk, list[str]], tmp_path: Path
) -> None:
    world, _desk, _buttons = app
    http = world.http
    assert (await http.get("/app/logins")).json() == {"logins": []}
    kept = await http.post(
        "/app/logins",
        json={"site": "https://www.doctolib.fr/sessions/new", "username": "noan@x.fr",
              "password": PASSWORD, "totp": "JBSWY3DPEHPK3PXP"},
    )  # fmt: skip
    assert kept.json() == {"name": "doctolib", "sites": ["doctolib.fr"]}
    # The same account again: its password is replaced, under the same name.
    again = await http.post(
        "/app/logins", json={"site": "doctolib.fr", "username": "noan@x.fr", "password": "n0uv"}
    )
    assert again.json()["name"] == "doctolib"
    listed = (await http.get("/app/logins")).json()["logins"]
    assert listed == [
        {
            "name": "doctolib",
            "sites": ["doctolib.fr"],
            "username": "noan@x.fr",
            "totp": False,
            "url": "",
        }
    ]
    assert PASSWORD not in json.dumps(listed) and "n0uv" not in json.dumps(listed)
    login = read_login(config_for(tmp_path), "doctolib")
    assert login is not None and login.password == "n0uv"

    wrong = await http.post("/app/logins", json={"site": "doctolib.fr", "username": "x"})
    assert wrong.status_code == 422 and "password" in wrong.json()["error"]
    assert (await http.post("/app/logins/nope/remove", json={})).status_code == 404
    assert (await http.post("/app/logins/doctolib/remove", json={})).status_code == 200
    assert (await http.get("/app/logins")).json() == {"logins": []}

    stranger = await http.post(
        "/app/logins",
        json={"site": "doctolib.fr", "password": "x"},
        headers={"origin": "https://evil.example"},
    )
    assert stranger.status_code == 403
    http.cookies.clear()
    assert (await http.get("/app/logins")).status_code == 401


async def test_no_vault_no_page(journal: SqliteJournal, tmp_path: Path) -> None:
    devices = Devices(journal, URL, session_key(tmp_path / "m"))
    channel = MobileChannel(
        journal, OwnerInbox(make_orion(journal, ScriptedProvider())), devices, tmp_path / "m"
    )
    served = mobile_app(channel, None, True, 30)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=served), base_url=URL, headers={"origin": URL}
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
        assert (await http.get("/app/logins")).status_code == 404


async def test_orion_asks_for_a_login_in_the_app_not_on_telegram(
    app: tuple[World, BrowserDesk, list[str]],
) -> None:
    world, desk, buttons = app
    http = world.http
    tool = next(t for t in browser_tools(desk) if t.name == "browser_save_password")
    said = await tool.handler(
        tool.parse(
            json.dumps(
                {"name": "urssaf", "site": "https://www.urssaf.fr/connexion", "why": "Tes décl."}
            )
        ),
        CTX,
    )
    assert "l'app" in said.content and not said.is_error
    assert buttons == []
    [asked] = (await http.get("/app/api/logins/asked")).json()["asked"]
    assert asked["kind"] == "ask" and asked["site"] == "urssaf.fr" and asked["why"] == "Tes décl."
    assert asked["url"] == "https://www.urssaf.fr/connexion"
    token = asked["token"]
    # Saying it is saved without saving it: refused, the request stays.
    unsaved = await http.post(
        f"/app/api/logins/asked/{token}", json={"answer": "saved", "name": "urssaf"}
    )
    assert unsaved.status_code == 404
    kept = await http.post(
        "/app/logins", json={"site": "urssaf.fr", "username": "n", "password": PASSWORD}
    )
    name = kept.json()["name"]
    done = await http.post(f"/app/api/logins/asked/{token}", json={"answer": "saved", "name": name})
    assert "« urssaf »" in done.json()["done"]
    assert (await http.get("/app/api/logins/asked")).json() == {"asked": []}
    notices = [a.notice for a in await desk.alerts(datetime.now(UTC))]
    assert any("« urssaf »" in n for n in notices)

    # On Telegram, he gets it there, as before.
    desk.on_telegram = lambda: True
    await tool.handler(tool.parse(json.dumps({"name": "caf", "site": "caf.fr", "why": ""})), CTX)
    assert len(buttons) == 1 and "caf.fr" in buttons[0]


async def test_a_sign_in_form_is_filled_from_the_app_and_offered_to_keep(
    app: tuple[World, BrowserDesk, list[str]], journal: SqliteJournal, tmp_path: Path
) -> None:
    world, desk, buttons = app
    http = world.http
    tools = {t.name: t for t in browser_tools(desk)}
    await tools["browser_open"].handler(
        tools["browser_open"].parse('{"url": "https://compte.test/"}'), CTX
    )
    said = await tools["browser_hand_over"].handler(
        tools["browser_hand_over"].parse('{"why": "Connecte-toi"}'), CTX
    )
    assert not said.is_error and buttons == []
    [handover] = (await http.get("/app/api/browser")).json()["handovers"]
    token = handover["token"]

    async def act(**body: object) -> dict[str, object]:
        response = await http.post(f"/app/api/browser/{token}/act", json=body)
        assert response.status_code == 200, response.text
        return response.json()

    started = await act(do="start")
    assert started["form"] == {"username": True, "password": True}
    signed = await act(do="signin", username="noan@x.fr", password=PASSWORD)
    account_posted = signed["url"]
    assert account_posted == "https://compte.test/login" and signed["form"] is None
    page = await tools["browser_read"].handler(tools["browser_read"].parse("{}"), CTX)
    assert "Bienvenue" in page.content and PASSWORD not in page.content  # masked for Orion

    done = await act(do="done")
    keep = done["keep"]
    assert isinstance(keep, dict) and keep["kind"] == "keep"
    assert keep["site"] == "compte.test" and keep["username"] == "noan@x.fr"
    assert buttons == []  # asked in the app, not on Telegram
    kept = await http.post(f"/app/api/logins/asked/{keep['token']}", json={"answer": "keep"})
    assert "« compte »" in kept.json()["done"]
    login = read_login(config_for(tmp_path), "compte")
    assert login is not None and (login.username, login.password) == ("noan@x.fr", PASSWORD)
    assert PASSWORD not in str([e.payload for e in await journal.read()])


def install(service: GuardianService) -> None:
    LoginDesk(service).install()
    AppEdge(service).install()


async def test_run_split_the_guardian_keeps_what_the_app_gives(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    """The app's edge is the guardian's: the password never passes through the core, and
    Orion's request goes to the app rather than to the guardian's Telegram form."""
    config = guardian_config(
        tmp_path, modules={"mobile": {"enabled": True, "public_url": URL, "notifications": False}}
    )
    prepare: Callable[[GuardianService], None] = install
    async with running_guardian(config, prepare=prepare) as g:
        edge = g.service.app_edge
        assert edge is not None
        kept = await edge.save("caf.fr", "noan", PASSWORD, "", "")
        assert kept == {"name": "caf", "sites": ["caf.fr"]}
        assert [entry["name"] for entry in await edge.listing()] == ["caf"]
        assert PASSWORD not in json.dumps(await edge.listing())

        desk = BrowserDesk(journal, lambda: browser(Account()), guardian=g.client)
        desk.in_app = True
        tool = next(t for t in browser_tools(desk) if t.name == "browser_save_password")
        await tool.handler(tool.parse(json.dumps({"name": "caf", "site": "caf.fr"})), CTX)
        [asked] = desk.asked()
        await desk.saved(asked["token"], "caf")  # checked with the guardian, by name
        assert desk.asked() == []

        assert await edge.remove("caf")
        kinds = [e.kind for e in await g.journal.read()]
        assert LOGIN_ADDED in kinds and LOGIN_REMOVED in kinds
