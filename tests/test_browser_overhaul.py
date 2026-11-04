"""What the owner saw go wrong in his browser for real (D245): a menu Orion could not see, a link
that « did nothing », a payslip it could not fetch, sign-ins lost at every restart."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from playwright.async_api import Route

from orion.computer.browser import Session
from orion.computer.direct import BrowserDesk, browser_tools
from orion.core.journal import SqliteJournal
from orion.tools.base import Tool, ToolContext
from tests.test_browser_direct import number, use
from tests.test_computer import only_the_test_site

PAGES = {
    "/menu": """<html><title>Menu</title><body>
      <div style="cursor:pointer"
           onclick="document.getElementById('out').textContent='Bulletin ouvert'">
        <span>Bulletin</span></div>
      <button aria-label="Grille" id="g"></button>
      <p id="out">rien</p><p>Une ligne qui ne bouge pas</p></body></html>""",
    "/popup": """<html><title>Lanceur</title><body>
      <a href="/opened" target="_blank">Créer</a></body></html>""",
    "/opened": "<html><title>Ouvert</title><body><p>Le formulaire de création</p></body></html>",
    "/covered": """<html><title>Couvert</title><body>
      <button onclick="document.getElementById('out').textContent='Pris'">Valider</button>
      <p id="out">rien</p>
      <div style="position:fixed;inset:0;background:#fff">Accepter les cookies</div>
      </body></html>""",
    "/confirm": """<html><title>Compte</title><body>
      <button onclick="document.getElementById('out').textContent =
        confirm('Supprimer ?') ? 'Supprimé' : 'Annulé'">Supprimer</button>
      <p id="out">rien</p></body></html>""",
    "/file": """<html><title>Bulletins</title><body>
      <a href="/bulletin.pdf">Octobre</a></body></html>""",
    "/signin": """<html><title>Connexion</title><body>
      <input type="password"><iframe src="https://www.google.com/recaptcha/api2/anchor"
        width="256" height="60"></iframe></body></html>""",
    "/agent": """<html><title>Agent</title>
      <meta name="viewport" content="width=device-width,initial-scale=1"><body><p id="w"></p>
      <script>document.getElementById('w').textContent =
        (matchMedia('(pointer:coarse)').matches ? 'tel ' : 'pc ') + innerWidth;</script>
      </body></html>""",
}


async def handle(route: Route) -> None:
    path = "/" + route.request.url.split("/", 3)[3]
    if path.endswith(".pdf"):
        await route.fulfill(
            status=200,
            content_type="application/pdf",
            headers={"content-disposition": 'attachment; filename="bulletin.pdf"'},
            body=b"%PDF-1.4 octobre",
        )
        return
    await route.fulfill(
        status=200, content_type="text/html; charset=utf-8", body=PAGES.get(path, "<p>ok</p>")
    )


def own_browser(tmp: Path, phone: bool = False) -> Session:
    session = Session(False, only_the_test_site, {"annonces.test": handle})
    session.keep_downloads = True
    session.profile = tmp / "browser"
    session.phone = {"width": 412, "height": 860} if phone else None
    return session


@pytest.fixture
async def desk(journal: SqliteJournal, tmp_path: Path) -> AsyncIterator[BrowserDesk]:
    desk = BrowserDesk(journal, lambda: own_browser(tmp_path))
    desk.profile = tmp_path / "browser"
    yield desk
    await desk.aclose()


def tools_of(desk: BrowserDesk) -> dict[str, Tool]:
    return {t.name: t for t in browser_tools(desk)}


async def test_what_only_a_script_makes_clickable_is_seen_and_named(desk: BrowserDesk) -> None:
    tools = tools_of(desk)
    page = await use(tools, "browser_open", url="https://annonces.test/menu")
    assert "Grille" in page  # an icon button, named by its aria-label
    done = await use(tools, "browser_click", element=number(page, "Bulletin"))
    assert "Bulletin ouvert" in done
    assert "Une ligne qui ne bouge pas" not in done  # only what changed comes back


async def test_a_click_that_changes_nothing_says_so_and_numbers_stay(desk: BrowserDesk) -> None:
    tools = tools_of(desk)
    page = await use(tools, "browser_open", url="https://annonces.test/menu")
    grid = number(page, "Grille")
    done = await use(tools, "browser_click", element=grid)
    assert "Rien n'a changé" in done
    again = await use(tools, "browser_read", part=0)
    assert number(again, "Grille") == grid


async def test_a_link_that_opens_a_tab_takes_the_conversation_there_and_back(
    desk: BrowserDesk,
) -> None:
    tools = tools_of(desk)
    page = await use(tools, "browser_open", url="https://annonces.test/popup")
    opened = await use(tools, "browser_click", element=number(page, "Créer"))
    assert "Le formulaire de création" in opened
    live = desk.tab("owner")
    assert live is not None
    await use(tools, "browser_back")
    assert "Lanceur" in await use(tools, "browser_read", part=0)
    assert live.page.url.endswith("/popup")


async def test_a_click_nothing_can_reach_goes_through_and_tells_why(desk: BrowserDesk) -> None:
    tools = tools_of(desk)
    page = await use(tools, "browser_open", url="https://annonces.test/covered")
    done = await use(tools, "browser_click", element=number(page, "Valider"))
    assert "Pris" in done
    assert "Accepter les cookies" in done  # what stood in the way


async def test_a_confirmation_is_accepted_not_silently_dismissed(desk: BrowserDesk) -> None:
    tools = tools_of(desk)
    page = await use(tools, "browser_open", url="https://annonces.test/confirm")
    done = await use(tools, "browser_click", element=number(page, "Supprimer"))
    assert "Supprimé" in done
    assert "Supprimer ?" in done


async def test_a_file_a_signed_in_site_gives_is_kept(desk: BrowserDesk) -> None:
    kept: dict[str, bytes] = {}

    async def keep(name: str, data: bytes, _kind: str, _ctx: ToolContext, _how: str) -> str:
        kept[name] = data
        return "fi_1"

    tools = {t.name: t for t in browser_tools(desk, keep)}
    page = await use(tools, "browser_open", url="https://annonces.test/file")
    done = await use(tools, "browser_click", element=number(page, "Octobre"))
    assert kept["bulletin.pdf"].startswith(b"%PDF")
    assert "fi_1" in done


async def test_a_tab_can_be_shown_as_a_computer_and_be_handled_there(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    desk = BrowserDesk(journal, lambda: own_browser(tmp_path, phone=True))
    try:
        tools = tools_of(desk)
        assert "tel 412" in await use(tools, "browser_open", url="https://annonces.test/agent")
        assert "pc 1280" in await use(tools, "browser_view", screen="computer")
        assert desk._session is not None  # pyright: ignore[reportPrivateUsage]
        assert (await desk._session.size())["width"] == 1280  # pyright: ignore[reportPrivateUsage]
        assert await desk._session.screenshot(False)  # pyright: ignore[reportPrivateUsage]
        assert "tel 412" in await use(tools, "browser_view", screen="phone")
    finally:
        await desk.aclose()


async def test_on_a_phone_a_click_is_a_finger(journal: SqliteJournal, tmp_path: Path) -> None:
    desk = BrowserDesk(journal, lambda: own_browser(tmp_path, phone=True))
    try:
        tools = tools_of(desk)
        page = await use(tools, "browser_open", url="https://annonces.test/menu")
        assert "Bulletin ouvert" in await use(
            tools, "browser_click", element=number(page, "Bulletin")
        )
    finally:
        await desk.aclose()


async def test_his_sign_ins_survive_the_browser_dying_under_it(
    desk: BrowserDesk, tmp_path: Path
) -> None:
    tools = tools_of(desk)
    await use(tools, "browser_open", url="https://annonces.test/menu")
    assert desk._session is not None  # pyright: ignore[reportPrivateUsage]
    context = desk._session.current().context  # pyright: ignore[reportPrivateUsage]
    await context.add_cookies(
        [{"name": "sid", "value": "x", "domain": "annonces.test", "path": "/"}]
    )
    desk._session._cookies_at = 0.0  # pyright: ignore[reportPrivateUsage]
    await use(tools, "browser_read", part=0)
    kept = json.loads((tmp_path / "browser" / "cookies.json").read_text(encoding="utf-8"))
    assert any(c["name"] == "sid" for c in kept)
    assert await desk.sites() == ["annonces.test"]


async def test_after_a_lost_tab_it_knows_where_it_was(desk: BrowserDesk) -> None:
    tools = tools_of(desk)
    await use(tools, "browser_open", url="https://annonces.test/opened")
    await desk.close("owner")
    refused = await use(tools, "browser_click", element=1)
    assert "ERROR" in refused
    assert "annonces.test/opened" in refused
    assert "Le formulaire de création" in await use(tools, "browser_read", part=0)


async def test_he_opens_the_browser_himself_and_orion_is_not_woken(desk: BrowserDesk) -> None:
    woken: list[int] = []

    async def resume() -> None:
        woken.append(1)

    desk.resume = resume
    tools_of(desk)
    taken = await desk.take("https://annonces.test/menu")
    assert await desk.on_tab("hands", lambda s: s.screenshot(False))
    await desk.give_back(taken["token"], done=True)
    await asyncio.sleep(0)
    assert not woken
    assert desk.tab("hands") is None
    assert not desk.finished


async def test_a_sign_in_form_with_a_recaptcha_behind_it_is_no_wall(desk: BrowserDesk) -> None:
    page = await use(tools_of(desk), "browser_open", url="https://annonces.test/signin")
    assert "hand_over" not in page
