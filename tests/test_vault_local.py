"""The vault without the guardian (D120): Orion keeps the logins itself, still never sees them,
and offers to keep the one the owner types in a hand-over."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from playwright.async_api import Route

from orion.computer.direct import BrowserDesk, browser_tools
from orion.computer.handover import handover_app
from orion.computer.vault import LocalVault, VaultError
from orion.config import OrionConfig
from orion.core.hlc import HybridClock
from orion.core.journal import SqliteJournal
from orion.guardian.ledger import LOGIN_ADDED, LOGIN_LENT
from orion.guardian.logins import logins_dir, totp
from orion.tools.base import ToolContext
from tests.test_handover import PASSWORD, SIGN_IN, School, desk_for
from tests.test_logins import Account, browser


class WrongPassword(School):
    """Every password is wrong: the sign-in form comes back."""

    async def handle(self, route: Route) -> None:
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=SIGN_IN)


def config_for(tmp_path: Path) -> OrionConfig:
    return OrionConfig.model_validate(
        {"data_dir": tmp_path, "guardian": {"data_dir": tmp_path / "guardian"}}
    )


async def ledger_events(config: OrionConfig) -> list[tuple[str, dict[str, object]]]:
    async with SqliteJournal(config.guardian_ledger_path, HybridClock("t")) as journal:
        return [(e.kind, e.payload) for e in await journal.read()]


async def test_orion_keeps_the_vault_itself(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    vault = LocalVault(config, clock=lambda: 59.0)
    kept = await vault.keep("https://www.doctolib.fr/sessions/new", "noan@x.fr", PASSWORD)
    assert kept.name == "doctolib" and kept.sites == ["doctolib.fr"]
    path = logins_dir(config) / "doctolib.json"
    assert path.stat().st_mode & 0o777 == 0o600
    # The same account again: its password is updated; another account: a name of its own.
    assert (await vault.keep("doctolib.fr", "noan@x.fr", "n0uveau")).name == "doctolib"
    assert (await vault.keep("doctolib.fr", "maman@x.fr", "autre")).name == "doctolib-2"
    accounts = await vault.accounts()
    assert [k["name"] for k in accounts["logins"]] == ["doctolib", "doctolib-2"]

    lent = await vault.lend_login("doctolib", {"action": "login", "login": "doctolib"}, None)
    assert lent["password"] == "n0uveau" and lent["sites"] == ["doctolib.fr"]
    with pytest.raises(VaultError):
        await vault.login_code("doctolib", lent["loan"])  # no 2FA key kept for it
    with pytest.raises(VaultError):
        await vault.lend_login("banque", {}, None)

    events = await ledger_events(config)
    assert [k for k, _ in events].count(LOGIN_ADDED) == 3 and (
        LOGIN_LENT,
        {
            "name": "doctolib",
            "how": "asked",
        },
    ) in events
    assert PASSWORD not in json.dumps([p for _, p in events])  # the ledger never holds one


async def test_a_2fa_code_comes_only_with_a_loan(tmp_path: Path) -> None:
    from orion.guardian.ledger import Ledger
    from orion.guardian.logins import add_login, make_login

    config = config_for(tmp_path)
    config.guardian_ledger_path.parent.mkdir(parents=True)
    async with SqliteJournal(config.guardian_ledger_path, HybridClock("t")) as journal:
        login = make_login("compte", ["compte.test"], "noan", PASSWORD, "JBSWY3DPEHPK3PXP")
        await add_login(config, Ledger(journal), login)
    vault = LocalVault(config, clock=lambda: 59.0)
    with pytest.raises(VaultError):
        await vault.login_code("compte", "no-loan")
    lent = await vault.lend_login("compte", {}, None)
    assert await vault.login_code("compte", lent["loan"]) == totp("JBSWY3DPEHPK3PXP", 59.0)


async def test_orion_signs_in_from_his_own_vault_without_seeing_the_password(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    vault = LocalVault(config_for(tmp_path))
    await vault.keep("compte.test", "noan@x.fr", PASSWORD, "https://compte.test/")
    account = Account()
    desk = BrowserDesk(journal, lambda: browser(account), vault=vault)
    tools = {t.name: t for t in browser_tools(desk)}

    async def use(name: str, **args: object) -> str:
        result = await tools[name].handler(
            tools[name].parse(json.dumps(args)), ToolContext("owner", "t1")
        )
        return ("ERROR " if result.is_error else "") + result.content

    try:
        assert "« compte » : compte.test" in await use("browser_accounts")
        page = await use("browser_login", login="compte")
        assert "Identifiant prêt" in page and PASSWORD not in page
        await use("browser_fill", element=1, field="username")
        await use("browser_fill", element=2, field="password")
        welcome = await use("browser_click", element=4)
        assert "Bienvenue" in welcome and "Tr0ub4dor%263" in account.posted[0]
        assert PASSWORD not in welcome and "Tr0ub4dor%263" not in welcome  # masked
    finally:
        await desk.aclose()
    assert PASSWORD not in str([e.payload for e in await journal.read()])


async def test_he_signs_in_by_hand_and_is_offered_to_keep_it(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    config = config_for(tmp_path)
    school = School()
    desk = desk_for(journal, school, tmp_path / "profile")
    desk.keeper = desk.vault = LocalVault(config)
    buttons: list[tuple[str, str]] = []

    async def notify(text: str, url: str) -> None:
        buttons.append((text, url))

    async def is_owner(init: str) -> bool:
        return init == "owner"

    desk.notify, desk.base_url = notify, "https://o.fr/console/browser"
    tools = {t.name: t for t in browser_tools(desk)}
    ctx = ToolContext("owner", "t1")
    page = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=handover_app(desk, is_owner)), base_url="https://o.fr"
    )
    try:
        await tools["browser_open"].handler(
            tools["browser_open"].parse('{"url": "https://ecole.test/edt"}'), ctx
        )
        await tools["browser_hand_over"].handler(
            tools["browser_hand_over"].parse('{"why": "Connecte-toi"}'), ctx
        )
        token = buttons[0][1].split("t=")[1]

        async def act(**body: object) -> httpx.Response:
            return await page.post("/act", json={"init": "owner", "token": token, **body})

        await act(do="start")
        await act(do="tap", x=50, y=50)  # the password field
        await act(do="type", text=PASSWORD)
        await act(do="key", key="Enter")
        await act(do="done")
        text, url = buttons[1]
        assert "Garder ton identifiant ecole.test" in text and "jamais" in text
        assert PASSWORD not in text and PASSWORD not in url
        keep = url.split("t=")[1]
        shown = await page.get(f"/keep?t={keep}")
        assert "ecole.test" in shown.text and PASSWORD not in shown.text
        refused = await page.post("/keep", json={"init": "stranger", "token": keep, "yes": True})
        assert refused.status_code == 403
        done = await page.post("/keep", json={"init": "owner", "token": keep, "yes": True})
        assert "Gardé" in done.json()["done"] and "« ecole »" in done.json()["done"]
        lent = await LocalVault(config).lend_login("ecole", {}, None)
        assert lent["password"] == PASSWORD

        notices = [a.notice for a in await desk.alerts(datetime.now(UTC))]
        assert any("« ecole »" in n and "`browser` login" in n for n in notices)
        assert (await page.post("/keep", json={"init": "owner", "token": keep})).status_code == 200
    finally:
        await page.aclose()
        await desk.aclose()
    assert PASSWORD not in str([e.payload for e in await journal.read()])


async def test_a_sign_in_that_failed_is_not_offered(journal: SqliteJournal, tmp_path: Path) -> None:
    school = WrongPassword()
    desk = desk_for(journal, school, tmp_path / "profile")
    desk.keeper = desk.vault = LocalVault(config_for(tmp_path))
    buttons: list[tuple[str, str]] = []

    async def notify(text: str, url: str) -> None:
        buttons.append((text, url))

    async def is_owner(init: str) -> bool:
        return init == "owner"

    desk.notify, desk.base_url = notify, "https://o.fr/console/browser"
    tools = {t.name: t for t in browser_tools(desk)}
    ctx = ToolContext("owner", "t1")
    page = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=handover_app(desk, is_owner)), base_url="https://o.fr"
    )
    try:
        await tools["browser_open"].handler(
            tools["browser_open"].parse('{"url": "https://ecole.test/edt"}'), ctx
        )
        await tools["browser_hand_over"].handler(
            tools["browser_hand_over"].parse('{"why": "Connecte-toi"}'), ctx
        )
        token = buttons[0][1].split("t=")[1]
        for body in ({"do": "start"}, {"do": "tap", "x": 50, "y": 50}):
            await page.post("/act", json={"init": "owner", "token": token, **body})
        await page.post("/act", json={"init": "owner", "token": token, "do": "type", "text": "x"})
        await page.post("/act", json={"init": "owner", "token": token, "do": "done"})
        assert len(buttons) == 1  # still on the sign-in form: nothing to keep
    finally:
        await page.aclose()
        await desk.aclose()


async def test_orion_asks_for_a_login_and_he_gives_it_from_his_phone(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    config = config_for(tmp_path)
    desk = BrowserDesk(journal, lambda: browser(Account()), vault=LocalVault(config))
    buttons: list[tuple[str, str]] = []

    async def notify(text: str, url: str) -> None:
        buttons.append((text, url))

    async def is_owner(init: str) -> bool:
        return init == "owner"

    desk.notify, desk.base_url = notify, "https://o.fr/console/browser"
    tool = next(t for t in browser_tools(desk) if t.name == "browser_save_password")
    said = await tool.handler(
        tool.parse(json.dumps({"name": "urssaf", "site": "urssaf.fr", "why": "Pour tes décl."})),
        ToolContext("owner", "t1"),
    )
    assert "coffre" in said.content and not said.is_error
    [(text, url)] = buttons
    assert "urssaf.fr" in text
    keep = url.split("t=")[1]
    page = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=handover_app(desk, is_owner)), base_url="https://o.fr"
    )
    try:
        assert 'type="password"' in (await page.get(f"/keep?t={keep}")).text
        done = await page.post(
            "/keep",
            json={"init": "owner", "token": keep, "yes": True, "username": "n", "password": "pw"},
        )
        assert "« urssaf »" in done.json()["done"]
    finally:
        await page.aclose()
        await desk.aclose()
    assert (await LocalVault(config).lend_login("urssaf", {}, None))["password"] == "pw"
