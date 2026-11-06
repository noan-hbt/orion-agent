"""Orion's own browser, driven from its loop: a real Chromium on a test site."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from orion.computer.browser import CONFIG_HOME, Session
from orion.computer.direct import PAGE, BrowserDesk, browser_tools
from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import SqliteJournal
from orion.memory.provenance import trace
from orion.tools.base import Tool, ToolContext
from tests.test_computer import Site, session


async def owner_says(journal: SqliteJournal, text: str, turn: str) -> None:
    await journal.append(
        [
            NewEvent(
                kind="message.received",
                payload={"thread_id": "owner", "channel": "imessage", "text": text},
                provenance=Provenance(source="imessage:owner", actor="owner:main"),
                trust=Trust.OWNER,
                correlation_id=turn,
            )
        ]
    )


@pytest.fixture
async def browser(journal: SqliteJournal) -> AsyncIterator[tuple[dict[str, Tool], Site]]:
    site = Site()
    desk = BrowserDesk(journal, lambda: session(site))
    yield {t.name: t for t in browser_tools(desk)}, site
    await desk.aclose()


async def use(tools: dict[str, Tool], name: str, turn: str = "t1", **args: object) -> str:
    import json

    tool = tools[name]
    result = await tool.handler(tool.parse(json.dumps(args)), ToolContext("owner", turn))
    if not result.is_error and name != "browser_close":
        assert result.trust is Trust.WEB  # a page is web content, whoever opened it
    return ("ERROR " if result.is_error else "") + result.content


def number(page: str, label: str) -> int:
    line = next(line for line in page.splitlines() if line.startswith("[") and label in line)
    return int(line[1 : line.index("]")])


async def test_orion_browses_a_site_itself(
    journal: SqliteJournal, browser: tuple[dict[str, Tool], Site]
) -> None:
    tools, site = browser
    await owner_says(journal, "Regarde les T3 sur https://annonces.test/ stp", "t1")

    home = await use(tools, "browser_open", url="https://annonces.test/")
    assert "Locations à Lyon" in home and "Éléments" in home
    t2 = await use(tools, "browser_click", element=number(home, "T2 Lyon 7e"))
    assert "650 € CC" in t2
    home = await use(tools, "browser_back")
    found = await use(
        tools, "browser_type", element=number(home, "Chercher"), text="T3", enter=True
    )
    assert "2 résultats" in found  # a site's own search (GET) works
    # A link from a page opened is open to Orion; its address was never typed by anyone.
    assert "650 € CC" in await use(tools, "browser_open", url="https://annonces.test/t2")
    assert "fermé" in await use(tools, "browser_close")
    assert not any(r.startswith("POST") for r in site.requests)


async def test_orion_browses_like_a_person(journal: SqliteJournal) -> None:
    """D115: any address, any text, forms sent; only the server's own addresses stay out."""
    site = Site()
    desk = BrowserDesk(journal, lambda: session(site, read_only=False))
    tools = {t.name: t for t in browser_tools(desk)}
    try:
        home = await use(tools, "browser_open", url="https://annonces.test/?ref=orion")
        assert "Locations à Lyon" in home  # an address nobody gave: fine
        message = number(home, "Votre message")
        await use(tools, "browser_type", element=message, text="Bonjour, le T2 est libre ?")
        await use(tools, "browser_click", element=number(home, "Envoyer"))
        assert any(r.startswith("POST https://annonces.test/contact") for r in site.requests)
        inside = await use(tools, "browser_open", url="http://127.0.0.1:8787/inkbox/webhook")
        assert inside.startswith("ERROR")  # the server itself, never
    finally:
        await desk.aclose()


async def test_pages_seen_are_traceable(
    journal: SqliteJournal, browser: tuple[dict[str, Tool], Site]
) -> None:
    tools, _ = browser
    await owner_says(journal, "va sur https://annonces.test/", "t1")
    await use(tools, "browser_open", url="https://annonces.test/")
    pages = [e for e in await journal.read() if e.kind == PAGE]
    assert [e.trust for e in pages] == [Trust.WEB]
    assert pages[0].provenance.source == "web:https://annonces.test/"
    assert trace(await journal.read(), "web:https://annonces.test/").origins == pages


async def test_a_verified_demarche_is_followed_at_the_next_visit(journal: SqliteJournal) -> None:
    """ORION-5, D255: kept only once this turn's browser shows it done on the site, by the path
    it names; given back on the site."""
    from orion.cognition.verifier import Verifier
    from orion.context.threads import MESSAGE_KIND
    from tests.fakes import ScriptedProvider, reply

    site = Site()
    desk = BrowserDesk(journal, lambda: session(site))
    desk.verifier = Verifier(ScriptedProvider(reply('{"ok": true}')), "verifier")
    tools = {t.name: t for t in browser_tools(desk)}
    how = {
        "site": "annonces.test",
        "task": "voir le T2",
        "steps": "Accueil → « T2 Lyon 7e »",
        "proof": "650 € CC",
    }
    try:
        home = await use(tools, "browser_open", url="https://annonces.test/")
        # The site was visited, the démarche not done: its proof is on no page of the site,
        # even though another tool said it this turn.
        await journal.append(
            [
                NewEvent(
                    kind=MESSAGE_KIND,
                    payload={"message": {"role": "tool", "content": "T2 : 650 € CC"}},
                    provenance=Provenance(source="web:https://ailleurs.test/", actor="orion"),
                    trust=Trust.WEB,
                    correlation_id="t1",
                )
            ]
        )
        assert (await use(tools, "browser_save_procedure", **how)).startswith("ERROR")
        await use(tools, "browser_click", element=number(home, "T2 Lyon 7e"))
        # A path the browser never took is not kept.
        wrong = {**how, "steps": "Accueil → « Mes annonces » → https://annonces.test/compte"}
        assert (await use(tools, "browser_save_procedure", **wrong)).startswith("ERROR")
        assert not (await use(tools, "browser_save_procedure", **how)).startswith("ERROR")
    finally:
        await desk.aclose()

    later = BrowserDesk(journal, lambda: session(site))  # another day: rebuilt from the journal
    try:
        tools = {t.name: t for t in browser_tools(later)}
        page = await use(tools, "browser_open", turn="t2", url="https://annonces.test/")
        assert "T2 Lyon 7e »" in page and "voir le T2" in page
    finally:
        await later.aclose()


async def test_a_demarche_is_not_proven_by_another_site(journal: SqliteJournal) -> None:
    """D255: the confirmation must come from the site itself, not another one seen that turn."""
    from orion.cognition.verifier import Verifier
    from orion.computer.procedures import ProcedureInput, Seen, keep_procedure
    from tests.fakes import ScriptedProvider, reply

    verifier = Verifier(ScriptedProvider(reply('{"ok": true}')), "verifier")
    seen = [
        Seen("open https://www.impots.gouv.fr/", "https://www.impots.gouv.fr/", "Accueil"),
        Seen("open https://mail.test/", "https://mail.test/", "Votre déclaration est envoyée"),
    ]
    args = ProcedureInput(
        site="impots.gouv.fr",
        task="déclarer les revenus",
        steps="Accueil",
        proof="Votre déclaration est envoyée",
    )
    kept, _ = await keep_procedure(journal, verifier, args, ToolContext("owner", "t1"), seen, {})
    assert not kept


async def test_reading_the_rest_of_a_long_page(
    journal: SqliteJournal, browser: tuple[dict[str, Tool], Site]
) -> None:
    tools, _ = browser
    assert (await use(tools, "browser_read")).startswith("ERROR")  # nothing open yet
    await owner_says(journal, "https://annonces.test/", "t1")
    await use(tools, "browser_open", url="https://annonces.test/")
    assert "Locations" in await use(tools, "browser_read", part=0)
    assert "Locations à Lyon" in await use(tools, "browser_read", find="lyon")
    assert "n'apparaît pas" in await use(tools, "browser_read", find="piscine")


async def test_a_browser_that_died_is_opened_again(journal: SqliteJournal) -> None:
    site = Site()
    desk = BrowserDesk(journal, lambda: session(site))
    tools = {t.name: t for t in browser_tools(desk)}
    assert "Locations à Lyon" in await use(tools, "browser_open", url="https://annonces.test/")
    dead = desk.session
    assert dead.page is not None
    await dead.page.context.close()  # Chromium goes away under the session (a crash)
    assert not dead.alive
    assert "Locations à Lyon" in await use(tools, "browser_open", url="https://annonces.test/")
    assert desk.session is not dead and desk.session.alive
    await desk.aclose()


async def test_a_browser_that_cannot_start_says_so(journal: SqliteJournal) -> None:
    started = Session()._launch()  # pyright: ignore[reportPrivateUsage]
    assert started["env"]["XDG_CONFIG_HOME"] == str(CONFIG_HOME)  # a home it cannot write
    desk = BrowserDesk(journal, lambda: Session(executable="/nowhere/chromium"))
    tools = {t.name: t for t in browser_tools(desk)}
    said = await use(tools, "browser_open", url="https://annonces.test/")
    assert said.startswith("ERROR the browser did not start")
    await desk.aclose()
