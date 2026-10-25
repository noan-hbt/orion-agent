"""The owner's logins, kept by the guardian (D111): the browser signs in on any site, without
the model ever seeing a password."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from playwright.async_api import Route

from orion.computer.browser import BrowserError, Session
from orion.core.journal import SqliteJournal
from orion.guardian.client import GuardianClient, GuardianUnavailableError
from orion.guardian.ledger import LOGIN_CODE, LOGIN_LENT
from orion.guardian.logins import (
    LoginDesk,
    LoginError,
    add_login,
    make_login,
    read_export,
    site_of,
    totp,
    totp_secret,
)
from orion.web.fetch import FetchError
from tests.fake_guardian import running_guardian
from tests.test_computer import element
from tests.test_writes import writable_config

PASSWORD = "Tr0ub4dor&3"
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890", RFC 6238's key


def test_codes_are_those_of_an_authenticator_app() -> None:
    assert totp(RFC_SECRET, 59, digits=8) == "94287082"  # RFC 6238, appendix B
    assert totp(RFC_SECRET, 1111111109, digits=8) == "07081804"
    uri = f"otpauth://totp/Doctolib:noan?secret={RFC_SECRET.lower()}&issuer=Doctolib"
    assert totp_secret(uri) == RFC_SECRET
    with pytest.raises(LoginError, match="base32"):
        totp_secret("pas une clé !")


def test_a_password_managers_export_is_read() -> None:
    assert site_of("https://www.doctolib.fr/sessions/new") == "doctolib.fr"
    assert site_of("connect.urssaf.fr") == "urssaf.fr"
    assert site_of("https://www.amazon.co.uk/ap/signin") == "amazon.co.uk"
    keepassxc = (
        '"Group","Title","Username","Password","URL","Notes","TOTP"\n'
        '"Root","Doctolib","noan@x.fr","p1","https://www.doctolib.fr/","",'
        f'"otpauth://totp/x?secret={RFC_SECRET}"\n'
        '"Root","Wifi","","p2","","",""\n'
    )
    logins, skipped = read_export(keepassxc)
    [doctolib] = logins
    assert (doctolib.name, doctolib.sites, doctolib.username) == (
        "doctolib",
        ["doctolib.fr"],
        "noan@x.fr",
    )
    assert doctolib.totp == RFC_SECRET and "Wifi" in skipped[0]
    bitwarden = (
        "folder,favorite,type,name,notes,fields,reprompt,login_uri,login_username,login_password,"
        "login_totp\n,,login,Leboncoin,,,,https://auth.leboncoin.fr/login,n@x.fr,p3,\n"
        ",,login,Leboncoin,,,,https://www.leboncoin.fr,autre@x.fr,p4,\n"
    )
    first, second = read_export(bitwarden)[0]
    assert (first.name, second.name) == ("leboncoin", "leboncoin-2")
    assert first.sites == ["leboncoin.fr"] and first.url.startswith("https://auth.")
    chrome = "name,url,username,password,note\nurssaf.fr,https://www.urssaf.fr/,n,p5,\n"
    assert read_export(chrome)[0][0].name == "urssaf-fr"
    assert read_export("a,b\n1,2\n")[0] == []


async def test_the_guardian_lends_a_login_once_and_keeps_the_2fa_key(tmp_path: Path) -> None:
    config = writable_config(tmp_path)
    lend = {"action": "login", "login": "doctolib", "session": "", "url": ""}
    desk: list[LoginDesk] = []

    def install(service: Any) -> None:
        desk.append(LoginDesk(service))
        desk[0].install()

    async with running_guardian(config, prepare=install) as g:
        g.fake.inbound("salut")
        login = make_login("doctolib", ["doctolib.fr"], "noan@x.fr", PASSWORD, RFC_SECRET)
        await add_login(config, g.service.ledger, login)
        assert (config.guardian.data_dir / "logins" / "doctolib.json").stat().st_mode & 0o077 == 0
        listed = await g.client.logins()
        assert listed == [{"name": "doctolib", "sites": ["doctolib.fr"], "totp": True}]
        assert PASSWORD not in json.dumps(listed)

        with pytest.raises(GuardianUnavailableError, match="no login"):
            await g.client.lend_login("banque", {**lend, "login": "banque"}, None)
        lent = await g.client.lend_login("doctolib", lend, None)  # when Orion asks (D115)
        assert (lent["username"], lent["password"], lent["totp"]) == ("noan@x.fr", PASSWORD, True)
        assert RFC_SECRET not in json.dumps(lent)  # the 2FA key stays with the guardian

        desk[0].clock = lambda: 59.0
        assert await g.client.login_code("doctolib", str(lent["loan"])) == "287082"
        with pytest.raises(GuardianUnavailableError, match="no loan"):
            await g.client.login_code("doctolib", "made-up")
        kinds = [e.kind for e in await g.journal.read()]
        assert kinds.count(LOGIN_LENT) == 1 and kinds.count(LOGIN_CODE) == 1
        assert PASSWORD not in str([e.payload for e in await g.journal.read()])


SIGN_IN = """<html><head><title>Connexion</title></head><body>
<form method="post" action="/login"><input name="email" type="email" placeholder="E-mail">
<input name="pw" type="password" placeholder="Mot de passe">
<input name="otp" placeholder="Code">
<button type="submit">Se connecter</button></form>
<button onclick="document.querySelector('[name=pw]').type='text'">Afficher</button>
</body></html>"""


class Account:
    """A site with a sign-in form; it shows back what was posted, like a page may."""

    def __init__(self) -> None:
        self.posted: list[str] = []

    async def handle(self, route: Route) -> None:
        request = route.request
        if request.method == "POST":
            self.posted.append(request.post_data or "")
            body = (
                "<html><title>Mon compte</title><body>Bienvenue. Reçu : "
                f"{request.post_data}</body></html>"
            )
        else:
            body = SIGN_IN
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)


async def account_or_elsewhere(host: str, _port: int) -> None:
    if host not in ("compte.test", "ailleurs.test"):
        raise FetchError(f"{host} is not a public address")


def browser(account: Account, read_only: bool = False) -> Session:
    return Session(
        read_only,
        account_or_elsewhere,
        {"compte.test": account.handle, "ailleurs.test": account.handle},
    )


async def test_secrets_are_typed_only_on_the_logins_sites_and_fields() -> None:
    account = Account()
    async with browser(account) as page:
        await page.goto("https://ailleurs.test/")
        with pytest.raises(BrowserError, match=r"not on compte\.test"):
            await page.fill_secret(
                await element(page, "Mot de passe"), PASSWORD, ["compte.test"], "password"
            )
        await page.goto("https://compte.test/")
        with pytest.raises(BrowserError, match="password field only"):
            await page.fill_secret(
                await element(page, "E-mail"), PASSWORD, ["compte.test"], "password"
            )
        with pytest.raises(BrowserError, match="password field only"):
            await page.fill_secret(
                await element(page, "Mot de passe"), "n@x.fr", ["compte.test"], "username"
            )
        await page.fill_secret(
            await element(page, "Mot de passe"), PASSWORD, ["compte.test"], "password"
        )
        assert PASSWORD not in json.dumps((await page.snapshot()).elements)


class Lender(GuardianClient):
    """The guardian's side of a sign-in, as the core sees it."""

    grants: list[str | None]

    async def lend_login(
        self, name: str, params: dict[str, Any], grant: str | None
    ) -> dict[str, Any]:
        assert params == {"action": "login", "login": name}
        self.grants.append(grant)
        return {
            "username": "noan@x.fr",
            "password": PASSWORD,
            "sites": ["compte.test"],
            "url": "https://compte.test/",
            "totp": True,
            "loan": "l1",
        }

    async def login_code(self, name: str, loan: str) -> str:
        assert (name, loan) == ("compte", "l1")
        return "482913"


async def test_orion_signs_in_himself_without_seeing_the_password(
    journal: SqliteJournal,
) -> None:
    """D113: no web agent. Orion drives the sign-in page; the guardian's login is typed by
    code, and masked on every page he reads."""
    from orion.computer.direct import BrowserDesk, browser_tools
    from orion.tools.base import ToolContext

    account = Account()
    lender = cast(Lender, Lender.over_socket(Path("/nonexistent.sock")))
    lender.grants = []
    desk = BrowserDesk(journal, lambda: browser(account), guardian=lender)
    tools = {t.name: t for t in browser_tools(desk)}

    async def use(name: str, **args: object) -> str:
        tool = tools[name]
        ctx = ToolContext("owner", "t1", grant="g1")
        result = await tool.handler(tool.parse(json.dumps(args)), ctx)
        return ("ERROR " if result.is_error else "") + result.content

    try:
        page = await use("browser_login", login="compte")
        assert lender.grants == ["g1"] and "Identifiant prêt" in page and "Connexion" in page
        assert "username" not in page and PASSWORD not in page
        await use("browser_fill", element=1, field="username")
        await use("browser_fill", element=2, field="password")
        await use("browser_fill", element=3, field="code")
        shown = await use("browser_click", element=5)  # « Afficher » : the password in clear
        assert PASSWORD not in shown and "••••••" in shown
        welcome = await use("browser_click", element=4)
        assert "Bienvenue" in welcome
        assert "Tr0ub4dor%263" in account.posted[0] and "otp=482913" in account.posted[0]
        assert PASSWORD not in welcome and "Tr0ub4dor%263" not in welcome
        assert "482913" not in welcome
        # The password goes into a password field of the account's site, nowhere else.
        wrong = await use("browser_fill", element=1, field="password")
        assert wrong.startswith("ERROR")
    finally:
        await desk.aclose()
    assert PASSWORD not in str([e.payload for e in await journal.read()])
    assert "482913" not in str([e.payload for e in await journal.read()])


async def test_the_owner_gives_a_login_from_telegram_and_the_core_never_sees_it(
    tmp_path: Path,
) -> None:
    import time
    from urllib.parse import parse_qs, urlparse

    import httpx

    from orion.guardian.logins import LoginForm, read_login
    from orion.guardian.service import GuardianService
    from orion.guardian.telegram import TelegramGate
    from orion.guardian.vault import Vault
    from tests.fake_telegram import OWNER, STRANGER, TOKEN, FakeTelegram
    from tests.test_console import signed
    from tests.test_telegram_guardian import split_config

    config = split_config(tmp_path)
    config = config.model_copy(
        update={"guardian": config.guardian.model_copy(update={"secrets_url": "https://o.fr"})}
    )
    tg = FakeTelegram()
    vaults: list[Vault] = []

    def prepare(service: GuardianService) -> None:
        gate = TelegramGate(service, tg.client())
        gate.install()
        LoginDesk(service).install()
        vault = Vault(service, gate, set())
        LoginForm(service, gate, vault.base).install(vault)
        vault.install()
        vaults.append(vault)

    async with running_guardian(config, prepare=prepare) as g:
        with pytest.raises(GuardianUnavailableError, match="lowercase name"):
            await g.client.ask_login("Doctolib!", "doctolib.fr", "")
        await g.client.ask_login("doctolib", "https://www.doctolib.fr/", "pour tes rendez-vous")
        [asked] = tg.sent()
        assert "doctolib.fr" in asked["text"]
        url = asked["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"]
        assert url.startswith("https://o.fr/secrets/login/?t=")
        token = str(parse_qs(urlparse(str(url)).query)["t"][0])
        public = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=vaults[0].public_app()), base_url="https://o.fr"
        )
        page = await public.get(f"/secrets/login/?t={token}")
        assert "doctolib.fr" in page.text and 'type="password"' in page.text
        now = time.time()
        body = {"token": token, "username": "noan@x.fr", "password": PASSWORD, "totp": RFC_SECRET}
        stranger = await public.post(
            "/secrets/login/set", json={**body, "init": signed(TOKEN, STRANGER, now)}
        )
        assert stranger.status_code == 403
        done = await public.post(
            "/secrets/login/set", json={**body, "init": signed(TOKEN, OWNER, now)}
        )
        assert done.status_code == 200
        again = await public.post(
            "/secrets/login/set", json={**body, "init": signed(TOKEN, OWNER, now)}
        )
        assert again.status_code == 410  # one time
        await public.aclose()
        kept = read_login(config, "doctolib")
        assert kept is not None and kept.password == PASSWORD and kept.sites == ["doctolib.fr"]
        assert "double authentification" in tg.sent()[-1]["text"]
        assert await g.client.logins() == [
            {"name": "doctolib", "sites": ["doctolib.fr"], "totp": True}
        ]
        assert PASSWORD not in repr([e.payload for e in await g.journal.read()])


async def test_an_export_is_imported_from_the_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from orion.cli import _logins  # pyright: ignore[reportPrivateUsage]
    from orion.guardian.logins import read_login

    config = writable_config(tmp_path)
    export = tmp_path / "export.csv"
    export.write_text(
        "name,url,username,password,note\nDoctolib,https://www.doctolib.fr/,n@x.fr,p1,\n"
        "Sans site,,n,p2,\n",
        encoding="utf-8",
    )
    command = argparse.Namespace(logins_command="import", file=export)
    assert await _logins(config, command) == 0
    out = capsys.readouterr().out
    assert "doctolib : doctolib.fr" in out and "laissé de côté" in out and "Efface" in out
    kept = read_login(config, "doctolib")
    assert kept is not None and kept.password == "p1"
    assert await _logins(config, argparse.Namespace(logins_command="list")) == 0
    assert "- doctolib : doctolib.fr" in capsys.readouterr().out
    # Through sudo, the export comes on stdin: the guardian's user cannot read his home.
    import io
    import sys

    stdin = sys.stdin
    sys.stdin = io.StringIO("name,url,username,password\nAmeli,https://www.ameli.fr/,n,p3\n")
    try:
        piped = argparse.Namespace(logins_command="import", file=Path("-"))
        assert await _logins(config, piped) == 0
    finally:
        sys.stdin = stdin
    assert read_login(config, "ameli") is not None
    # Imported again with another password: the one kept is not silently replaced.
    export.write_text(
        "name,url,username,password\nDoctolib,https://www.doctolib.fr/,n@x.fr,autre\n", "utf-8"
    )
    assert await _logins(config, command) == 0
    assert "déjà gardé" in capsys.readouterr().out
    kept = read_login(config, "doctolib")
    assert kept is not None and kept.password == "p1"
    # Created for its user alone, and removed only by its own name.
    from orion.guardian.logins import logins_dir

    path = logins_dir(config) / "doctolib.json"
    assert path.stat().st_mode & 0o777 == 0o600
    outside = path.parent.parent / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    removing = argparse.Namespace(logins_command="remove", name="../outside")
    assert await _logins(config, removing) == 2
    assert outside.exists()
