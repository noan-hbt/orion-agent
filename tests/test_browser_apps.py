"""Pages that build themselves (D119): seen for real on Hyperplanning, Orion read « Veuillez
patienter » while the page showed its sign-in form, and kept reading it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Route

from orion.computer import browser as browsing
from orion.computer.browser import Session
from orion.computer.direct import MOVING, BrowserDesk, browser_tools
from orion.core.journal import SqliteJournal
from orion.tools.base import Tool
from tests.test_browser_direct import number, use
from tests.test_computer import only_the_test_site

APP = """<html><head><title>Hyperplanning</title></head><body>
<div id="app">Veuillez patienter...</div>
<script>
const app = document.getElementById('app');
function signIn() {
  app.innerHTML = '<input placeholder="Identifiant" value="noan"><input type="password" ' +
    'placeholder="Mot de passe"><button id="go">Se connecter</button>';
  document.getElementById('go').onclick = () => {
    app.textContent = 'Veuillez patienter...';
    setTimeout(() => { app.innerHTML = '<p>Vos cours : Maths 8h, Droit 10h</p>'; }, 900);
  };
}
setTimeout(signIn, 700);
</script></body></html>"""

FRAMED = """<html><head><title>Portail</title></head><body><h1>Portail de l'école</h1>
<iframe src="https://annonces.test/cas" width="400" height="300"></iframe></body></html>"""
CAS = """<html><body><form onsubmit="event.preventDefault();
document.body.innerHTML = '<p>Connecté, ' + document.getElementById('u').value + '</p>'">
<input id="u" placeholder="Identifiant ENT"><button>Valider</button></form></body></html>"""

REDRAWN = """<html><body><div id="box"><button>Voir</button></div><script>
setTimeout(() => { document.getElementById('box').innerHTML = '<button>Voir</button>'; }, 1500);
</script></body></html>"""

LATER = """<html><body><p>Résultats en cours</p><script>
setTimeout(() => { document.body.insertAdjacentHTML('beforeend', '<p>Nouveau : 3 cours</p>'); },
           2500);
</script></body></html>"""

STUCK = """<html><body><p>Veuillez patienter</p></body></html>"""


class Apps:
    async def handle(self, route: Route) -> None:
        url = route.request.url
        body = (
            FRAMED
            if url.endswith("/portail")
            else CAS
            if url.endswith("/cas")
            else REDRAWN
            if url.endswith("/redrawn")
            else LATER
            if url.endswith("/later")
            else STUCK
            if url.endswith("/stuck")
            else APP
        )
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)


@pytest.fixture
async def tools(journal: SqliteJournal) -> AsyncIterator[dict[str, Tool]]:
    apps = Apps()
    desk = BrowserDesk(
        journal, lambda: Session(False, only_the_test_site, {"annonces.test": apps.handle})
    )
    yield {t.name: t for t in browser_tools(desk)}
    await desk.aclose()


async def test_an_app_is_read_once_it_has_built_itself(tools: dict[str, Tool]) -> None:
    page = await use(tools, "browser_open", url="https://annonces.test/hp")
    assert "Se connecter" in page and "patienter" not in page
    signed_in = await use(tools, "browser_click", element=number(page, "Se connecter"))
    # The click sends nothing over the network and loads no page: the app redraws itself.
    assert "Vos cours : Maths 8h" in signed_in and "patienter" not in signed_in


async def test_reading_again_shows_the_page_as_it_is_now(
    tools: dict[str, Tool], monkeypatch: pytest.MonkeyPatch
) -> None:
    page = await use(tools, "browser_open", url="https://annonces.test/later")
    assert "Résultats en cours" in page and "Nouveau" not in page
    # The page adds a line by itself, long after it settled: reading again must show it.
    monkeypatch.setattr(browsing, "STEADY_SECONDS", 0.5)
    now = ""
    for _ in range(20):
        now = await use(tools, "browser_read")
        if "Nouveau" in now:
            break
    assert "Nouveau : 3 cours" in now  # never the text kept from the last action


async def test_a_sign_in_form_in_a_frame_is_seen_and_used(tools: dict[str, Tool]) -> None:
    page = await use(tools, "browser_open", url="https://annonces.test/portail")
    assert "Portail de l'école" in page and "dans le cadre de annonces.test" in page
    await use(tools, "browser_type", element=number(page, "Identifiant ENT"), text="noan")
    done = await use(tools, "browser_click", element=number(page, "Valider"))
    assert "Connecté, noan" in done


async def test_an_element_redrawn_since_is_said_gone(tools: dict[str, Tool]) -> None:
    page = await use(tools, "browser_open", url="https://annonces.test/redrawn")
    await asyncio.sleep(2)  # the app redraws its button by itself, after Orion read the page
    gone = await use(tools, "browser_click", element=number(page, "Voir"))
    assert gone.startswith("ERROR") and "n'existe plus" in gone and "Relis-la" in gone
    again = await use(tools, "browser_read")
    assert "Voir" in await use(tools, "browser_click", element=number(again, "Voir"))


async def test_a_page_that_never_gets_ready_says_so(
    tools: dict[str, Tool], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browsing, "STEADY_SECONDS", 1.0)
    page = await use(tools, "browser_open", url="https://annonces.test/stuck")
    assert "Veuillez patienter" in page and MOVING in page
