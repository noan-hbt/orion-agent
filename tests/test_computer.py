"""Orion's computer: a real headless browser, guarded at the network layer."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import Route

from orion.computer.browser import BrowserError, Session
from orion.computer.module import ComputerModule
from orion.config import OrionConfig
from orion.modules.registry import load
from orion.tools.base import Effect
from orion.web.fetch import FetchError

HOME = """<html><head><title>Annonces</title></head><body>
<h1>Locations à Lyon</h1>
<a href="/t2">T2 Lyon 7e, 650 €</a>
<form method="post" action="/contact"><input name="msg" placeholder="Votre message">
<button type="submit">Envoyer</button></form>
<form method="get" action="/search"><input name="q" type="search" placeholder="Chercher">
</form></body></html>"""
T2 = """<html><head><title>T2 Lyon 7e</title></head><body><p>Loyer 650 € CC, 46 m².</p>
</body></html>"""


class Site:
    def __init__(self) -> None:
        self.requests: list[str] = []

    async def handle(self, route: Route) -> None:
        request = route.request
        self.requests.append(f"{request.method} {request.url}")
        if request.url.endswith("/t2"):
            body = T2
        elif "/search" in request.url:
            body = "<html><title>Résultats</title><body>2 résultats</body></html>"
        else:
            body = HOME
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)


async def only_the_test_site(host: str, _port: int) -> None:
    if host != "annonces.test":
        raise FetchError(f"{host} is not a public address")


def session(site: Site, read_only: bool = True) -> Session:
    return Session(read_only, only_the_test_site, {"annonces.test": site.handle})


async def element(browser: Session, label: str) -> int:
    snapshot = await browser.snapshot()
    line = next(e for e in snapshot.elements if label in e)
    return int(line[1 : line.index("]")])


async def test_a_read_only_browser_reads_and_searches_but_sends_nothing() -> None:
    site = Site()
    async with session(site) as browser:
        await browser.goto("https://annonces.test/")
        page = await browser.snapshot()
        assert page.title == "Annonces" and "Locations à Lyon" in page.text
        await browser.click(await element(browser, "T2 Lyon 7e"))
        assert "650 € CC" in (await browser.snapshot()).text
        await browser.back()
        await browser.type(await element(browser, "Chercher"), "T3", enter=True)
        assert "2 résultats" in (await browser.snapshot()).text  # a GET search works
        await browser.goto("https://annonces.test/")
        await browser.type(await element(browser, "Votre message"), "Bonjour", enter=False)
        await browser.click(await element(browser, "Envoyer"))
    assert not any(r.startswith("POST") for r in site.requests)
    assert any(b.startswith("POST https://annonces.test/contact") for b in browser.blocked)


async def test_private_addresses_are_out_of_reach() -> None:
    async with session(Site()) as browser:
        with pytest.raises(BrowserError):
            await browser.goto("http://127.0.0.1:8787/inkbox/webhook")
    assert browser.blocked == ["GET http://127.0.0.1:8787/inkbox/webhook"]


async def test_an_approved_task_may_send() -> None:
    site = Site()
    async with session(site, read_only=False) as browser:
        await browser.goto("https://annonces.test/")
        await browser.click(await element(browser, "Envoyer"))
    assert "POST https://annonces.test/contact" in site.requests


def test_the_browser_signs_in_and_sends_only_with_his_yes() -> None:
    """No separate web agent (D113): Orion's own browser reads alone; signing in and sending
    are the owner's to approve."""
    from orion.computer.direct import BrowserDesk, browser_tools

    tools = {t.name: t for t in browser_tools(BrowserDesk(None, lambda: session(Site())))}  # type: ignore[arg-type]
    assert tools["browser_open"].effect is Effect.NONE
    assert tools["browser_login"].effect is Effect.EXTERNAL
    assert tools["browser_send"].effect is Effect.EXTERNAL
    assert not tools["browser_send"].accepts_untrusted_context  # put to him after a page


def test_the_module_has_no_web_agent(tmp_path: Path) -> None:
    config = OrionConfig.model_validate(
        {"data_dir": tmp_path, "modules": {"computer": {"enabled": True}}}
    )
    [module] = [m for m in load(config) if isinstance(m, ComputerModule)]
    assert "no separate web agent" in module.doctor()[0]
