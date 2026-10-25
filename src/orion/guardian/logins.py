"""The owner's logins, kept by the guardian (D111): a password manager for Orion's browser.

A lent session (D49) expires, and the owner then has to sign in again by hand. So the guardian
also keeps, like KeePass, a login per site (address, username, password, and the 2FA secret
if the owner wants), and the browser signs in again by itself, on any site:

- Orion drives the login page in its own browser as any page (cookie banner, « Se connecter »,
  two steps…), and asks for the username, the password or a code to be typed into a field it
  names (`browser` fill, D113): it never sees them, nor does its journal (each page it reads
  has them masked);
- they are typed only on a page of the login's sites, the password only into a password field,
  and for a few minutes the browser may send only to those sites;
- the 2FA secret never leaves the guardian: the browser asks it for the current code, with the
  loan's id, while the loan lasts;
- lent like a session, when Orion's browser asks for it (D115). Each loan and code is
  recorded.

Logins come from the owner's phone (the guardian's own form, D94), from `orion logins add` on
the server, or by importing a password manager's CSV export (KeePass, KeePassXC, Bitwarden,
Chrome, Firefox, 1Password).

    guardian.login.added  guardian.login.removed  guardian.login.lent  guardian.login.code
"""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import hmac
import html
import io
import json
import re
import secrets as tokens
import struct
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlsplit

import structlog
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from orion.channels.telegram import TelegramError, check_init_data
from orion.core.private import write_private
from orion.guardian.ledger import LOGIN_ADDED, LOGIN_CODE, LOGIN_LENT, LOGIN_REMOVED, Ledger

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.guardian.service import GuardianService
    from orion.guardian.telegram import TelegramGate
    from orion.guardian.vault import Vault

log = structlog.get_logger(__name__)

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
LEND_SECONDS = 3600.0
"""How long a loan's 2FA codes may be asked: longer than any task runs."""
ASK_SECONDS = 15 * 60.0
_SECOND_LEVEL = {"co", "com", "org", "net", "gouv", "ac", "edu", "gov", "asso", "nom"}


class LoginError(ValueError):
    """Not a usable login."""


@dataclass
class Login:
    name: str
    sites: list[str]
    username: str
    password: str
    totp: str = ""
    """The 2FA secret (base32), if the owner gave it."""
    url: str = ""
    """The login page, if known."""


def logins_dir(config: OrionConfig) -> Path:
    return config.guardian.data_dir / "logins"


def site_of(address: str) -> str:
    """The site an address belongs to: `https://www.doctolib.fr/sessions/new` → doctolib.fr."""
    address = address.strip()
    host = (urlsplit(address if "://" in address else f"https://{address}").hostname or "").lower()
    labels = [part for part in host.split(".") if part]
    if len(labels) < 2:
        return ""
    keep = 3 if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL else 2
    return ".".join(labels[-keep:])


def totp_secret(value: str) -> str:
    """The base32 secret of a 2FA setup: given bare, or as an `otpauth://` address."""
    value = value.strip()
    if value.lower().startswith("otpauth://"):
        value = (parse_qs(urlsplit(value).query).get("secret") or [""])[0]
    secret = value.replace(" ", "").upper()
    try:
        base64.b32decode(secret + "=" * (-len(secret) % 8))
    except (binascii.Error, ValueError) as exc:
        raise LoginError("the 2FA secret is not a base32 key nor an otpauth:// address") from exc
    if not secret:
        raise LoginError("the 2FA secret is empty")
    return secret


def totp(secret: str, at: float, step: int = 30, digits: int = 6) -> str:
    """The code an authenticator app shows at `at` (RFC 6238)."""
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    mac = hmac.new(key, struct.pack(">Q", int(at // step)), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    number = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10**digits).zfill(digits)


def make_login(
    name: str,
    sites: list[str],
    username: str,
    password: str,
    totp_value: str = "",
    url: str = "",
) -> Login:
    if not NAME.match(name):
        raise LoginError("a login's name is lowercase letters, digits, - or _")
    found = sorted({s for s in (site_of(s) for s in [*sites, url]) if s})
    if not found:
        raise LoginError("a login needs its site (doctolib.fr) or its address")
    if not password:
        raise LoginError("a login needs its password")
    return Login(
        name,
        found,
        username.strip(),
        password,
        totp_secret(totp_value) if totp_value.strip() else "",
        url.strip(),
    )


async def add_login(config: OrionConfig, ledger: Ledger, login: Login) -> None:
    folder = logins_dir(config)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    path = folder / f"{login.name}.json"
    data = json.dumps(asdict(login))
    write_private(path, data)
    await ledger.record(
        LOGIN_ADDED,
        {
            "name": login.name,
            "domains": login.sites,
            "totp": bool(login.totp),
            "digest": hashlib.sha256(data.encode()).hexdigest(),
        },
    )


async def keep_login(
    config: OrionConfig, ledger: Ledger, site: str, username: str, password: str, url: str = ""
) -> Login:
    """A login the owner gave (his form, or what he typed in a hand-over), kept under a name of
    its own: the site's (`doctolib`), or `doctolib-2` if that one is taken by another account.
    The same account again has its password updated."""
    found = site_of(site or url)
    if not found:
        raise LoginError("no site for this login")
    base = re.sub(r"[^a-z0-9]+", "-", found.split(".")[0]).strip("-")[:30] or "login"
    name, n = base, 2
    while (existing := read_login(config, name)) is not None:
        if existing.username == username.strip() and found in existing.sites:
            break  # the same account: its password is updated
        name, n = f"{base}-{n}", n + 1
    login = make_login(name, [found], username, password, "", url)
    await add_login(config, ledger, login)
    return login


async def remove_login(config: OrionConfig, ledger: Ledger, name: str) -> bool:
    if not NAME.match(name):
        raise LoginError(f"not a login name: {name!r}")
    path = logins_dir(config) / f"{name}.json"
    existed = path.exists()
    path.unlink(missing_ok=True)
    await ledger.record(LOGIN_REMOVED, {"name": name})
    return existed


def read_login(config: OrionConfig, name: str) -> Login | None:
    path = logins_dir(config) / f"{name}.json"
    if not NAME.match(name) or not path.is_file():
        return None
    return Login(**json.loads(path.read_text(encoding="utf-8")))


_COLUMNS = {
    "title": ("title", "name", "account", "nom"),
    "url": ("url", "login_uri", "website", "web site", "login url", "uri", "adresse"),
    "username": ("username", "login_username", "user name", "login name", "email", "login"),
    "password": ("password", "login_password", "mot de passe"),
    "totp": ("totp", "login_totp", "otpauth", "one-time password", "otp"),
}


def read_export(text: str) -> tuple[list[Login], list[str]]:
    """The logins of a password manager's CSV export, and why the other rows were left out."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        return [], ["the file is empty"]
    header = [h.strip().lower() for h in rows[0]]
    where = {
        key: next((header.index(n) for n in names if n in header), -1)
        for key, names in _COLUMNS.items()
    }
    if where["password"] < 0 or where["url"] < 0:
        return [], ["no url or password column: is this a CSV export of a password manager?"]
    logins: list[Login] = []
    skipped: list[str] = []
    taken: set[str] = set()
    for number, row in enumerate(rows[1:], start=2):

        def cell(key: str, row: list[str] = row) -> str:
            at = where[key]
            return row[at].strip() if 0 <= at < len(row) else ""

        url, password = cell("url"), cell("password")
        site = site_of(url)
        if not site or not password:
            skipped.append(f"line {number} ({cell('title') or url or '?'}): no site or password")
            continue
        base = _slug(cell("title")) or _slug(site.split(".")[0]) or "login"
        name, n = base, 2
        while name in taken:
            name, n = f"{base}-{n}", n + 1
        try:
            login = make_login(name, [site], cell("username"), password, cell("totp"), url)
        except LoginError as exc:
            skipped.append(f"line {number} ({name}): {exc}")
            continue
        taken.add(name)
        logins.append(login)
    return logins, skipped


def _slug(text: str) -> str:
    plain = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return plain[:40].strip("-")


# -- the guardian's side ----------------------------------------------------------------------


@dataclass
class Loan:
    name: str
    until: float


@dataclass
class LoginDesk:
    """The routes the core uses: which logins exist, a loan for a task, its 2FA codes."""

    service: GuardianService
    loans: dict[str, Loan] = field(default_factory=dict[str, Loan])
    """Loan id → which login, until when; in memory: a restart ends them, like the task."""
    monotonic: Any = time.monotonic
    clock: Any = time.time

    def install(self) -> None:
        self.service.routes += [
            Route("/v1/logins", self._list, methods=["GET"]),
            Route("/v1/accounts", self._accounts, methods=["GET"]),
            Route("/v1/logins/{name}/lend", self._lend, methods=["POST"]),
            Route("/v1/logins/{name}/code", self._code, methods=["POST"]),
            Route("/v1/logins/keep", self._keep, methods=["POST"]),
        ]

    async def _list(self, _request: Request) -> Response:
        await self.service.ledger.catch_up()
        state = self.service.ledger.state
        return JSONResponse(
            [
                {"name": name, "sites": sites, "totp": name in state.logins_totp}
                for name, sites in sorted(state.logins.items())
            ]
        )

    async def _accounts(self, request: Request) -> Response:
        logins = json.loads(bytes((await self._list(request)).body))
        return JSONResponse({"logins": logins})

    async def _lend(self, request: Request) -> Response:
        """The username and password for Orion's browser, when it asks (D115)."""
        name = str(request.path_params["name"])
        ledger = self.service.ledger
        await ledger.catch_up()
        login = read_login(self.service.config, name)
        if name not in ledger.state.logins or login is None:
            return JSONResponse({"error": f"guardian: no login {name!r}"}, 404)
        how = "asked"  # lent when Orion asks (D115); each loan is recorded
        loan = tokens.token_urlsafe(18)
        self.loans = {k: v for k, v in self.loans.items() if v.until > self.monotonic()}
        self.loans[loan] = Loan(name, self.monotonic() + LEND_SECONDS)
        await ledger.record(LOGIN_LENT, {"name": name, "how": how})
        log.info("guardian.login_lent", name=name, how=how)
        return JSONResponse(
            {
                "username": login.username,
                "password": login.password,
                "sites": login.sites,
                "url": login.url,
                "totp": bool(login.totp),
                "loan": loan,
            }
        )

    async def _keep(self, request: Request) -> Response:
        """A login the owner gave in the console (his form, or what he typed in a hand-over),
        kept here: the core passes it on only after he said yes (D120)."""
        body = await _body(request)
        await self.service.ledger.catch_up()
        try:
            login = await keep_login(
                self.service.config,
                self.service.ledger,
                str(body.get("site", "")),
                str(body.get("username", "")),
                str(body.get("password", "")),
                str(body.get("url", "")),
            )
        except LoginError as exc:
            return JSONResponse({"error": str(exc)}, 400)
        log.info("guardian.login_kept", name=login.name)
        return JSONResponse({"name": login.name, "sites": login.sites})

    async def _code(self, request: Request) -> Response:
        """The 2FA code of a login lent for a task under way: the secret stays here."""
        name = str(request.path_params["name"])
        loan = self.loans.get(str((await _body(request)).get("loan", "")))
        if loan is None or loan.name != name or loan.until <= self.monotonic():
            return await self.service.deny(f"login {name} code", "no loan of this login under way")
        login = read_login(self.service.config, name)
        if login is None or not login.totp:
            return JSONResponse({"error": f"guardian: no 2FA secret for {name!r}"}, 404)
        await self.service.ledger.record(LOGIN_CODE, {"name": name})
        return JSONResponse({"code": totp(login.totp, self.clock())})


async def _body(request: Request) -> dict[str, Any]:
    try:
        loaded: Any = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError:
        return {}
    return cast(dict[str, Any], loaded) if isinstance(loaded, dict) else {}


@dataclass
class Asked:
    name: str
    site: str
    why: str
    until: float


class LoginForm:
    """« Ajoute mon compte Doctolib » from the owner's phone: the guardian's own form (D94)."""

    def __init__(self, service: GuardianService, gate: TelegramGate, base: str) -> None:
        self.service = service
        self.gate = gate
        self.base = base.rstrip("/")
        self.asked: dict[str, Asked] = {}
        self.now = time.time

    def install(self, vault: Vault) -> None:
        self.service.routes.append(Route("/v1/logins/ask", self._ask, methods=["POST"]))
        vault.public_routes += [
            Route("/secrets/login/", self._page, methods=["GET"]),
            Route("/secrets/login/set", self._set, methods=["POST"]),
        ]

    async def _ask(self, request: Request) -> Response:
        body = await _body(request)
        name = str(body.get("name", "")).strip()
        site = site_of(str(body.get("site", "")))
        why = str(body.get("why", "")).strip()[:300]
        if not NAME.match(name) or not site:
            return await self.service.deny(
                f"login {name}", "a lowercase name (doctolib) and its site (doctolib.fr)"
            )
        token = tokens.token_urlsafe(24)
        self.asked = {t: a for t, a in self.asked.items() if a.until > self.now()}
        self.asked[token] = Asked(name, site, why, self.now() + ASK_SECONDS)
        markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "Saisir l'identifiant",
                        "web_app": {"url": f"{self.base}/secrets/login/?t={token}"},
                    }
                ]
            ]
        }
        text = (
            f"Pour me connecter à ton compte {site}" + (f" ({why})" if why else "") + ", touche "
            "le bouton : le formulaire est celui de mon gardien, et ton mot de passe ne passe "
            "jamais par notre conversation. Valable 15 minutes."
        )
        try:
            await self.gate.client.call(
                "sendMessage", chat_id=self.gate.owner_id, text=text, reply_markup=markup
            )
        except TelegramError as exc:
            return JSONResponse({"error": f"guardian: Telegram unreachable: {exc}"}, 502)
        return JSONResponse({"asked": name})

    async def _page(self, request: Request) -> Response:
        from orion.guardian.vault import EXPIRED, PAGE

        asked = self.asked.get(request.query_params.get("t", ""))
        if asked is None or asked.until <= self.now():
            return HTMLResponse(PAGE.format(body=EXPIRED), status_code=410)
        form = _FORM.format(
            site=html.escape(asked.site),
            why=html.escape(asked.why),
            token=html.escape(request.query_params["t"], quote=True),
        )
        return HTMLResponse(PAGE.format(body=form))

    async def _set(self, request: Request) -> Response:
        body = await _body(request)
        user = check_init_data(str(body.get("init", "")), self.gate.client.token, self.now())
        if not self.gate.owner_id or user != self.gate.owner_id:
            await self.service.deny("login", "not opened by the owner, from Telegram")
            return JSONResponse({"error": "Ouvre ce formulaire depuis Telegram."}, 403)
        token = str(body.get("token", ""))
        asked = self.asked.get(token)
        if asked is None or asked.until <= self.now():
            return JSONResponse({"error": "Ce lien a expiré : redemande-le à Orion."}, 410)
        try:
            login = make_login(
                asked.name,
                [asked.site],
                str(body.get("username", "")),
                str(body.get("password", "")),
                str(body.get("totp", "")),
                str(body.get("url", "")),
            )
        except LoginError as exc:
            return JSONResponse({"error": str(exc)}, 400)
        del self.asked[token]
        await add_login(self.service.config, self.service.ledger, login)
        log.info("guardian.login_set", name=login.name)
        await self.gate.tell(
            f"C'est gardé : ton identifiant « {login.name} » ({', '.join(login.sites)})"
            + (", avec la double authentification." if login.totp else ".")
        )
        return JSONResponse({"set": login.name})


_FORM = """<h3>Ton compte {site}</h3><p class="note">{why}</p>
<form id="f">
<input id="u" autocomplete="off" placeholder="Identifiant ou e-mail">
<input id="p" type="password" autocomplete="off" required placeholder="Mot de passe">
<input id="o" autocomplete="off" placeholder="Clé de double authentification (facultatif)">
<input id="a" autocomplete="off" placeholder="Adresse de la page de connexion (facultatif)">
<button>Garder dans le gardien</button></form>
<p id="r" class="note">Ces valeurs vont directement au gardien d'Orion : ni la conversation ni le
modèle ne les voient. Le navigateur ne les tape que sur {site}.</p>
<script>
const tg = window.Telegram && Telegram.WebApp; if (tg) tg.ready();
const v = (id) => document.getElementById(id).value;
document.getElementById("f").onsubmit = async (e) => {{
  e.preventDefault();
  const r = document.getElementById("r");
  const res = await fetch("set", {{method: "POST", headers: {{"content-type": "application/json"}},
    body: JSON.stringify({{init: tg ? tg.initData : "", token: "{token}", username: v("u"),
      password: v("p"), totp: v("o"), url: v("a")}})}});
  const data = await res.json();
  if (res.ok) {{ r.textContent = "Gardé."; setTimeout(() => tg && tg.close(), 800); }}
  else r.textContent = data.error || "Refusé.";
}};
</script>"""
