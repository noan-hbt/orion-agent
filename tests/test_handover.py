"""The owner takes Orion's browser in hand from Telegram, and the site stays signed in (D116)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
from playwright.async_api import Route

from orion.computer.browser import Session
from orion.computer.direct import BrowserDesk, browser_tools
from orion.computer.handover import handover_app
from orion.core.journal import SqliteJournal
from orion.tools.base import ToolContext
from orion.web.fetch import FetchError

PASSWORD = "Tr0ub4dor&3"
SIGN_IN = """<html><head><title>Connexion</title></head><body style="margin:0">
<form method="post" action="/login" style="margin:0">
<input name="pw" type="password" style="display:block;margin:0;width:100%;height:100px">
<button type="submit">Se connecter</button></form></body></html>"""
WELCOME = "<html><head><title>Emploi du temps</title></head><body>Lundi : maths 8 h</body></html>"


class School:
    """A site whose sign-in sets a session cookie (no expiry), like a school's."""

    def __init__(self) -> None:
        self.posted: list[str] = []

    async def handle(self, route: Route) -> None:
        request = route.request
        if request.method == "POST":
            self.posted.append(request.post_data or "")
            await route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                headers={"set-cookie": "sid=ok; Path=/"},
                body=WELCOME,
            )
            return
        signed_in = "sid=ok" in (await request.all_headers()).get("cookie", "")
        body = WELCOME if signed_in else SIGN_IN
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)


async def only_the_school(host: str, _port: int) -> None:
    if host != "ecole.test":
        raise FetchError(f"{host} is not a public address")


def desk_for(journal: SqliteJournal, school: School, profile: Path) -> BrowserDesk:
    def browser() -> Session:
        session = Session(False, only_the_school, {"ecole.test": school.handle})
        session.profile = profile
        session.phone = {"width": 412, "height": 860}
        return session

    return BrowserDesk(journal, browser)


async def test_he_signs_in_in_orions_browser_and_it_stays_signed_in(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    school = School()
    desk = desk_for(journal, school, tmp_path / "profile")
    buttons: list[tuple[str, str]] = []

    async def notify(text: str, url: str) -> None:
        buttons.append((text, url))

    desk.notify, desk.base_url = notify, "https://o.fr/console/browser"
    tools = {t.name: t for t in browser_tools(desk)}
    ctx = ToolContext("owner", "t1")

    async def use(name: str, **args: object) -> str:
        result = await tools[name].handler(tools[name].parse(json.dumps(args)), ctx)
        return ("ERROR " if result.is_error else "") + result.content

    async def is_owner(init: str) -> bool:
        return init == "owner"

    try:
        assert "Connexion" in await use("browser_open", url="https://ecole.test/edt")
        said = await use("browser_hand_over", why="Connecte-toi à Hyperplanning")
        assert "attends" in said
        [(text, url)] = buttons
        assert "Connecte-toi à Hyperplanning" in text
        assert url.startswith("https://o.fr/console/browser/?t=")
        token = url.split("t=")[1]

        page = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=handover_app(desk, is_owner)),
            base_url="https://o.fr",
        )

        async def act(who: str, **body: object) -> httpx.Response:
            return await page.post("/act", json={"init": who, "token": token, **body})

        assert "Connecte-toi à Hyperplanning" in (await page.get(f"/?t={token}")).text
        assert (await act("stranger", do="start")).status_code == 403
        started = (await act("owner", do="start")).json()
        assert started["url"] == "https://ecole.test/edt" and started["width"] == 412
        assert (await page.get(f"/shot?t={token}")).content[:4] == b"\x89PNG"
        await act("owner", do="tap", x=50, y=50)  # the password field
        await act("owner", do="type", text=PASSWORD)
        await act("owner", do="key", key="Enter")
        assert school.posted == ["pw=Tr0ub4dor%263"]
        assert (await act("owner", do="done")).json()["done"] == "Merci, je reprends."
        await page.aclose()

        [alert] = await desk.alerts(datetime.now(UTC))
        assert alert.spoken_to and "https://ecole.test/login" in alert.notice
        assert "Lundi : maths" in await use("browser_read", part=0)  # Orion resumes, signed in
        assert PASSWORD not in str([e.payload for e in await journal.read()])
    finally:
        await desk.aclose()

    # The next day, after a restart: still signed in, its session cookie included.
    again = desk_for(journal, school, tmp_path / "profile")
    tools = {t.name: t for t in browser_tools(again)}
    try:
        result = await tools["browser_open"].handler(
            tools["browser_open"].parse('{"url": "https://ecole.test/edt"}'),
            ToolContext("owner", "t2"),
        )
        assert "Lundi : maths" in result.content
        out = await tools["browser_forget"].handler(
            tools["browser_forget"].parse('{"site": "ecole.test"}'), ToolContext("owner", "t2")
        )
        assert "Déconnecté" in out.content
        result = await tools["browser_open"].handler(
            tools["browser_open"].parse('{"url": "https://ecole.test/edt"}'),
            ToolContext("owner", "t2"),
        )
        assert "Connexion" in result.content  # signed out
    finally:
        await again.aclose()
