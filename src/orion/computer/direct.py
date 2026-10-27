"""Orion's own browser, driven step by step from its loop.

Orion itself opens a page, reads it, clicks, types, signs in to the owner's accounts and
sends forms, and decides what to do next with everything it knows: there is no separate web
agent (D113). One live browser per conversation, closed after a few idle minutes.

It browses like a person (D115): any address, any text typed, forms sent, on the owner's
accounts with the sessions and logins the guardian keeps, lent when Orion asks. The protections
there were stopping it more than they helped. What remains costs it nothing: the server's own
addresses (the guardian, the local network) are never reached (`browser.Session`), and a lent
password or 2FA code is masked on every page Orion reads.

Every page shown is journaled with `Trust.WEB` and a `web:<address>` source, so what it led to
can be traced and contained (`memory.provenance`).

    browser.page
"""

from __future__ import annotations

import asyncio
import re
import secrets as tokens
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote, quote_plus

import structlog
from playwright.async_api import Page
from pydantic import BaseModel, ConfigDict, Field

from orion.computer.browser import BrowserError, Session, Snapshot
from orion.computer.vault import LocalVault, LoginKeeper, VaultError
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.guardian.client import GuardianClient, GuardianUnavailableError
from orion.guardian.logins import site_of
from orion.initiative.engine import Alert
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult
from orion.web.fetch import normalize
from orion.web.service import WebState

log = structlog.get_logger(__name__)

PAGE = "browser.page"
IDLE_SECONDS = 600.0
SHOWN_CHARS = 4000
"""Page text shown after each action; `browser` read gives the rest, part by part."""
READ_CHARS = 60_000
_HREF = re.compile(r" -> (https?://\S+)$")


@dataclass
class BrowsingState(WebState):
    """`WebState` plus the pages the browser showed and their links."""

    name: str = "browser"

    async def apply(self, event: Event) -> None:
        await super().apply(event)
        if event.redacted:
            return
        if event.kind == PAGE:
            payload: dict[str, Any] = event.payload
            self.allowed.add(str(payload["url"]))
            self.allowed.update(str(link) for link in payload.get("links", []))


class Refused(ValueError):
    """Not allowed from Orion's own browser."""


SessionFactory = Callable[[], Session]


@dataclass
class _Live:
    """A conversation's tab in Orion's browser."""

    page: Page
    closer: asyncio.Task[None] | None = None
    text: str = ""
    login: dict[str, Any] | None = None
    """A login the vault lent for this browser: username, password, sites, code (D111, D120)."""
    hidden: set[str] = field(default_factory=set[str])
    """What Orion must never read: the password, and the codes typed."""


MASK = "••••••"
NO_VAULT = "No vault: the browser module has none (he fills it in the app: Mots de passe)."
HANDOVER_SECONDS = 30 * 60.0
BROWSING = frozenset(
    {
        "browser_open",
        "browser_click",
        "browser_type",
        "browser_select",
        "browser_scroll",
        "browser_back",
        "browser_read",
        "browser_login",
        "browser_fill",
    }
)
"""The tools that drive the page: a long turn says « je navigue sur le site » for them."""


@dataclass
class HandOver:
    thread: str
    why: str
    until: float
    typed: dict[str, str] = field(default_factory=dict[str, str])
    """What he typed into a sign-in form (site, username, password), to offer to keep it in
    the vault once he is done (D120). In memory only, never journaled."""
    offered: str = ""
    """The token of the offer to keep that login, once he is done: the app asks him at once."""


@dataclass
class KeepOffer:
    """A login to keep in the vault, from his phone: the one he just typed in a hand-over, or
    an empty form Orion asked him to fill (D120)."""

    site: str
    why: str
    until: float
    username: str = ""
    password: str = ""
    url: str = ""


KEEP_SECONDS = 15 * 60.0


def masked(text: str, hidden: set[str]) -> str:
    """The text with the login's secrets blanked out: a page may show what was typed, as typed
    or as it was posted (URL-encoded)."""
    forms = {f for s in hidden if len(s) >= 4 for f in (s, quote(s, safe=""), quote_plus(s))}
    for secret in sorted(forms, key=len, reverse=True):
        text = text.replace(secret, MASK)
    return text


class BrowserDesk:
    def __init__(
        self,
        journal: EventStore,
        sessions: SessionFactory,
        idle_seconds: float = IDLE_SECONDS,
        guardian: GuardianClient | None = None,
        vault: LocalVault | None = None,
    ) -> None:
        self._journal = journal
        self._sessions = sessions
        self.guardian = guardian
        self.vault: LoginKeeper | None = guardian if guardian is not None else vault
        """The logins: the guardian's when it runs apart (D111), else Orion's own (D120)."""
        self.offers: dict[str, KeepOffer] = {}
        """One-time token → a login he may keep, from his phone (D120)."""
        self.kept: list[tuple[str, str, datetime]] = []
        """Logins he kept from his phone (name, site, when), for Orion to use them."""
        self._idle = idle_seconds
        self.state = BrowsingState()
        self._runner = ProjectionRunner(journal, [self.state])
        self._live: dict[str, _Live] = {}
        """Conversation → its tab. One browser, one profile on disk (D116): its cookies, so its
        sign-ins, are the same in every tab and stay after a restart."""
        self._session: Session | None = None
        self._lock = asyncio.Lock()
        self.held: set[str] = set()
        """Tabs the owner has in hand (a hand-over): never closed for being idle."""
        self.handovers: dict[str, HandOver] = {}
        """One-time token → the tab the owner was handed (D116)."""
        self.finished: list[tuple[str, HandOver, str, datetime]] = []
        """Hand-overs he finished: (token, which, the page he left, when), for Orion to resume."""
        self.notify: Callable[[str, str], Awaitable[None]] | None = None
        """(text, address) → the owner gets a Telegram button opening that address."""
        self.base_url = ""
        """Where the hand-over page is served (the console's public address)."""
        self.in_app = False
        """The app shows a hand-over under way in Maintenant, with the live tab (D135)."""
        self.on_telegram: Callable[[], bool] = lambda: False
        """Whether he last wrote from Telegram: with the app there, the button goes only then."""

    @property
    def session(self) -> Session:
        if self._session is None:  # pragma: no cover - only after `_tab`
            raise Refused("No page is open: browser open first.")
        return self._session

    async def _tab(self, thread: str) -> _Live:
        """The conversation's tab, the browser opened first if need be; the session then acts
        on it. Called under the lock."""
        if self._session is None:
            session = self._sessions()
            await session.__aenter__()
            self._session = session
            first: Page | None = session.page
        else:
            first = None
        live = self._live.get(thread)
        if live is None:
            page = first or await self._session.new_page()
            live = self._live[thread] = _Live(page)
        self._session.page = live.page
        self._arm(thread, live)
        return live

    def _arm(self, thread: str, live: _Live) -> None:
        if live.closer is not None:
            live.closer.cancel()
        live.closer = asyncio.create_task(self._close_later(thread))

    async def _close_later(self, thread: str) -> None:
        await asyncio.sleep(self._idle)
        async with self._lock:  # never under an action running on that tab
            if thread not in self.held:
                await self.close(thread)

    def tab(self, thread: str) -> _Live | None:
        return self._live.get(thread)

    async def close(self, thread: str) -> bool:
        live = self._live.pop(thread, None)
        self.held.discard(thread)
        if live is None:
            return False
        if live.closer is not None and live.closer is not asyncio.current_task():
            live.closer.cancel()
        if not self._live and self._session is not None:
            # Before the last tab goes: the full Chromium ends its session with it, and its
            # session cookies (a school's sign-in) with them.
            await self._session.keep_cookies()
        await live.page.close()
        log.info("browser.closed", thread=thread)
        if not self._live and self._session is not None:  # the last tab: the browser goes
            session, self._session = self._session, None
            await session.__aexit__(None, None, None)
        return True

    async def aclose(self) -> None:
        for thread in list(self._live):
            await self.close(thread)

    async def on_tab(self, thread: str, action: Callable[[Session], Awaitable[Any]]) -> Any:
        """Something done on a conversation's tab from outside a turn (the owner's hand-over)."""
        async with self._lock:
            if thread not in self._live:
                raise Refused("That tab is closed.")
            await self._tab(thread)
            return await action(self.session)

    # -- actions ------------------------------------------------------------------------------

    async def open(self, url: str, ctx: ToolContext) -> str:
        address = normalize(url)
        if not address:
            raise Refused(f"{url} is not a web address (http or https).")
        return await self._act(ctx, lambda s: s.goto(address))

    async def click(self, element: int, ctx: ToolContext) -> str:
        return await self._act(ctx, lambda s: s.click(element))

    async def type(self, element: int, text: str, enter: bool, ctx: ToolContext) -> str:
        return await self._act(ctx, lambda s: s.type(element, text, enter))

    async def select(self, element: int, option: str, ctx: ToolContext) -> str:
        return await self._act(ctx, lambda s: s.select(element, option))

    async def scroll(self, down: bool, ctx: ToolContext) -> str:
        return await self._act(ctx, lambda s: s.scroll(down))

    async def back(self, ctx: ToolContext) -> str:
        return await self._act(ctx, lambda s: s.back())

    async def read(self, part: int, find: str, ctx: ToolContext) -> str:
        live = self._live.get(ctx.thread_id)
        if live is None:
            raise Refused("No page is open: browser open first.")

        async def look(_session: Session) -> None:
            return None

        # Always the page as it is now: what was read after the last action may have been a
        # « veuillez patienter » long gone (seen for real on Hyperplanning, D119).
        page = await self._act(ctx, look)
        text = live.text
        if not find and (part == 0 or (part == 1 and len(text) <= SHOWN_CHARS)):
            return page  # it fits: the whole page, as it is now
        if find:
            spots = [m.start() for m in re.finditer(re.escape(find), text, re.IGNORECASE)][:5]
            if not spots:
                return f"« {find} » n'apparaît pas sur la page."
            return "\n\n".join(f"…{text[max(0, i - 300) : i + 700]}…" for i in spots)
        start = part * SHOWN_CHARS
        if start >= len(text):
            return f"La page s'arrête avant la partie {part} ({len(text)} caractères)."
        chunk = text[start : start + SHOWN_CHARS]
        more = len(text) > start + SHOWN_CHARS
        return chunk + (f"\n[suite : browser read, part={part + 1}]" if more else "\n[fin]")

    async def login(self, args: Any, ctx: ToolContext) -> str:
        """A login from the vault (D111, D120), for a sign-in form: `fill` types it, Orion
        never sees it. Opens `url`, or the login's own page."""
        vault = self.vault
        if vault is None:
            raise Refused(NO_VAULT)
        try:
            lent = await vault.lend_login(
                args.login, {"action": "login", "login": args.login}, ctx.grant
            )
        except (GuardianUnavailableError, VaultError) as exc:
            raise Refused(f"The vault did not lend it: {exc}") from exc
        sites = list(lent["sites"])
        start = normalize(str(args.url)) if args.url else ""
        start = start or str(lent.get("url") or "") or (f"https://{sites[0]}/" if sites else "")
        async with self._lock:
            live = await self._tab(ctx.thread_id)
            name, loan = args.login, str(lent["loan"])

            async def code() -> str:
                return await vault.login_code(name, loan)

            live.login = {**lent, "code": code}
            live.hidden.add(str(lent["password"]))
        said = await self._act(ctx, lambda s: s.goto(start)) if start else ""
        return (
            "Identifiant prêt : `browser` fill tape l'identifiant, le mot de passe ou le code "
            "(tu ne les vois jamais), puis clique pour te connecter.\n\n" + said
        )

    async def fill(self, element: int, what: str, ctx: ToolContext) -> str:
        live = self._live.get(ctx.thread_id)
        if live is None or live.login is None:
            raise Refused("No login is lent to this browser: `browser` login first.")
        login = live.login
        if what == "code":
            if not login.get("totp"):
                raise Refused(
                    "No 2FA key is kept for this login: ask the owner for the code the site sent "
                    "him (SMS, e-mail), and type it with his words."
                )
            try:
                value = str(await login["code"]())
            except (GuardianUnavailableError, VaultError) as exc:
                raise Refused(f"No code from the vault: {exc}") from exc
            live.hidden.add(value)
        else:
            value = str(login[what])
        sites = list(login["sites"])
        return await self._act(ctx, lambda s: s.fill_secret(element, value, sites, what))

    async def hand_over(self, why: str, ctx: ToolContext) -> str:
        """The owner takes the tab in hand, from Telegram or the app (D116, D135): he signs in,
        types a code, solves a captcha; Orion picks up where he left it when he says he is
        done."""
        if ctx.thread_id not in self._live:
            raise Refused("Open the page first (browser open): he gets this very tab.")
        telegram = self.notify is not None and bool(self.base_url)
        if not telegram and not self.in_app:
            raise Refused(
                "Handing over needs the app, or Telegram and the console's public address "
                "([modules.console] public_url)."
            )
        token = tokens.token_urlsafe(24)
        now = time.monotonic()
        self.handovers = {t: h for t, h in self.handovers.items() if h.until > now}
        self.handovers[token] = HandOver(ctx.thread_id, why, now + HANDOVER_SECONDS)
        self.held.add(ctx.thread_id)
        # One place to take the tab, where he is: the app, unless he is on Telegram.
        telegram = telegram and (not self.in_app or self.on_telegram())
        if telegram and self.notify is not None:
            await self.notify(
                f"{why}\nTouche le bouton : c'est mon navigateur, sur la page où j'en suis. Fais "
                "ce qu'il faut (te connecter, un code…), puis « J'ai fini » : je reprends. Ce que "
                "tu tapes va dans la page, pas dans notre conversation.",
                f"{self.base_url}/?t={token}",
            )
        log.info("browser.handed_over", thread=ctx.thread_id)
        where = (
            "Il a reçu un bouton sur Telegram, et « Prendre la main » dans l'onglet Maintenant "
            "de l'app : les deux lui ouvrent"
            if telegram and self.in_app
            else "Il a reçu un bouton qui lui ouvre"
            if telegram
            else "Il a « Prendre la main » dans l'onglet Maintenant de l'app, qui lui ouvre"
        )
        return (
            f"{where} cet onglet de ton navigateur. Dis-lui en une "
            "phrase ce qu'il doit y faire, puis attends : quand il aura fini, tu le sauras et tu "
            "reprendras sur la page où il l'aura laissée. Ne touche pas à cet onglet d'ici là."
        )

    def handover(self, token: str) -> HandOver | None:
        found = self.handovers.get(token)
        return found if found is not None and found.until > time.monotonic() else None

    async def give_back(self, token: str, done: bool) -> str:
        """He is done (or gave up): the tab is Orion's again."""
        found = self.handovers.pop(token, None)
        if found is None:
            return ""
        self.held.discard(found.thread)
        live = self._live.get(found.thread)
        url = live.page.url if live is not None else ""
        if live is not None:
            live.text = ""  # what Orion last read is not the page any more
            self._arm(found.thread, live)
        if done:
            self.finished.append((token, found, url, datetime.now(UTC)))
            if self._session is not None:
                await self._session.keep_cookies()  # signed in: kept at once
            await self._offer_typed(found, live)
        return url

    async def _offer_typed(self, found: HandOver, live: _Live | None) -> None:
        """He signed in by hand: offer to keep that login, like a browser does. Only once the
        sign-in went through (no password field left on the page)."""
        typed = found.typed
        if self.vault is None or not typed.get("password") or live is None:
            return
        try:
            async with self._lock:
                await self._tab(found.thread)
                still = await self.session.shows_password_field()
        except BrowserError as exc:
            log.info("browser.keep_not_offered", error=str(exc))
            return
        if still:
            return
        try:
            found.offered = await self._offer(
                KeepOffer(
                    typed.get("site", ""),
                    "",
                    time.monotonic() + KEEP_SECONDS,
                    typed.get("username", ""),
                    typed["password"],
                    typed.get("url", ""),
                )
            )
        except Refused as exc:
            log.info("browser.keep_not_offered", error=str(exc))

    async def alerts(self, now: datetime) -> list[Alert]:
        """A finished hand-over, for Orion to resume where the owner left the page."""
        self.finished = [f for f in self.finished if now - f[3] < timedelta(hours=1)]
        self.kept = [k for k in self.kept if now - k[2] < timedelta(hours=1)]
        return [
            Alert(
                key=f"handover:{token}",
                notice=f"Il a fini dans ton navigateur ({found.why}) : la page est maintenant "
                f"{url}. Reprends ce que tu faisais pour lui, avec `browser` (read pour la "
                "voir).",
                why="il a fini ce que tu lui avais passé",
                fallback="",
                expires_at=at + timedelta(hours=1),
                importance=0.9,
                spoken_to=True,
            )
            for token, found, url, at in self.finished
        ] + [
            Alert(
                key=f"kept:{name}:{at.isoformat()}",
                notice=f"Il a gardé son identifiant de {site} dans ton coffre, sous le nom "
                f"« {name} » : `browser` login avec ce nom, puis fill, et tu te connectes sans "
                "jamais voir le mot de passe. Dis-le-lui en une phrase si c'est utile, sinon "
                "SILENCE.",
                why="il a gardé un identifiant",
                fallback="",
                expires_at=at + timedelta(hours=1),
                importance=0.6,
                spoken_to=True,
            )
            for name, site, at in self.kept
        ]

    async def accounts(self) -> str:
        if self.vault is None:
            return "Pas de coffre."
        try:
            kept = await self.vault.accounts()
        except (GuardianUnavailableError, VaultError) as exc:
            raise Refused(f"The vault did not answer: {exc}") from exc
        lines = [
            f"- « {k['name']} » : {', '.join(k['sites'])}"
            + (" (avec la double authentification)" if k["totp"] else "")
            for k in kept["logins"]
        ]
        if not lines:
            return (
                "Aucun mot de passe dans le coffre (les sites où ton navigateur est connecté le "
                "restent)."
            )
        return "Mots de passe gardés (`browser` login) :\n" + "\n".join(lines)

    async def save_password(self, args: Any) -> str:
        if self.vault is not None and self._in_app():
            # The app's form posts to the vault's keeper (the guardian, run apart): the core
            # never sees the password either way (D142).
            await self._offer(KeepOffer(args.site, args.why, time.monotonic() + KEEP_SECONDS))
            return (
                f"Il a, dans l'app (onglet Maintenant), un formulaire pour mettre son identifiant "
                f"de {args.site} dans ton coffre, rempli par les mots de passe de son téléphone. "
                "Dis-le-lui en une phrase. Tu ne verras jamais le mot de passe : quand on te dira "
                "qu'il est gardé, `browser` login avec le nom donné."
            )
        # With the guardian apart, its own form on Telegram takes it: the core never sees it.
        if self.guardian is None and self.vault is not None:
            await self._offer(KeepOffer(args.site, args.why, time.monotonic() + KEEP_SECONDS))
            return (
                f"Il a reçu un bouton pour saisir son identifiant de {args.site} dans ton coffre. "
                "Tu ne verras jamais le mot de passe : quand on te dira qu'il est gardé, "
                "`browser` login avec le nom donné."
            )
        if self.guardian is None:
            raise Refused(NO_VAULT)
        try:
            await self.guardian.ask_login(args.name, args.site, args.why)
        except GuardianUnavailableError as exc:
            raise Refused(f"Not asked: {exc}") from exc
        return (
            f"Il a reçu un bouton pour saisir son identifiant « {args.name} » dans le formulaire "
            "du gardien. Tu ne verras jamais le mot de passe : quand le gardien dit qu'il est "
            f'gardé, `browser` login avec login "{args.name}".'
        )

    # -- keeping a login from his phone (D120) -------------------------------------------------

    def _in_app(self) -> bool:
        """Whether he answers in the app: it is there, and he is not on Telegram (D141)."""
        telegram = self.notify is not None and bool(self.base_url)
        return self.in_app and not (telegram and self.on_telegram())

    async def _offer(self, offer: KeepOffer) -> str:
        """In the app, shown in Maintenant (D142); else a Telegram button. Its token."""
        in_app = self._in_app()
        if not in_app and (self.notify is None or not self.base_url):
            raise Refused(
                "Keeping a login from his phone needs the app, or Telegram and the console's "
                "public address ([modules.console] public_url)."
            )
        token = tokens.token_urlsafe(24)
        now = time.monotonic()
        self.offers = {t: o for t, o in self.offers.items() if o.until > now}
        self.offers[token] = offer
        if in_app or self.notify is None:
            return token
        what = (
            f"Garder ton identifiant {offer.site} ({offer.username}) dans mon coffre ?"
            if offer.password
            else f"{offer.why}\nSaisis ton identifiant {offer.site} : il va dans mon coffre."
        )
        await self.notify(
            f"{what} Je ne verrai jamais le mot de passe : je le taperai pour toi.",
            f"{self.base_url}/keep?t={token}",
        )
        return token

    def asked(self) -> list[dict[str, str]]:
        """What the app shows him (D142): a login to keep, or one Orion asks him for."""
        now = time.monotonic()
        return [
            {
                "token": token,
                "site": site_of(offer.site) or offer.site,
                "url": offer.url or (offer.site if "://" in offer.site else ""),
                "why": offer.why,
                "username": offer.username,
                "kind": "keep" if offer.password else "ask",
            }
            for token, offer in list(self.offers.items())
            if offer.until > now
        ]

    def decline(self, token: str) -> None:
        self.offers.pop(token, None)

    async def saved(self, token: str, name: str) -> None:
        """He filled Orion's request in the app, which put the login `name` straight into the
        vault: the core checks it is there, for that site (it never sees the password)."""
        found = self.offers.get(token)
        if found is None or found.until <= time.monotonic() or self.vault is None:
            raise Refused("C'est terminé, ou le lien a expiré.")
        try:
            kept = await self.vault.accounts()
        except (GuardianUnavailableError, VaultError) as exc:
            raise Refused(str(exc)) from exc
        site = site_of(found.site)
        if not any(k["name"] == name and site in k["sites"] for k in kept["logins"]):
            raise Refused(f"Rien n'est gardé sous « {name} » pour {found.site}.")
        del self.offers[token]
        self.kept.append((name, site, datetime.now(UTC)))
        log.info("browser.login_kept", name=name)

    def offer(self, token: str) -> KeepOffer | None:
        found = self.offers.get(token)
        return found if found is not None and found.until > time.monotonic() else None

    async def keep(self, token: str, username: str = "", password: str = "") -> str:
        """He said yes (or filled the form): the login goes into the vault. Its name."""
        found = self.offers.pop(token, None)
        if found is None or found.until <= time.monotonic() or self.vault is None:
            raise Refused("C'est terminé, ou le lien a expiré.")
        try:
            kept = await self.vault.keep(
                found.site,
                found.username or username,
                found.password or password,
                found.url,
            )
        except (GuardianUnavailableError, VaultError) as exc:
            self.offers[token] = found  # he may correct the form
            raise Refused(str(exc)) from exc
        self.kept.append((kept["name"], ", ".join(kept["sites"]), datetime.now(UTC)))
        log.info("browser.login_kept", name=kept["name"])
        return str(kept["name"])

    async def sign_in(self, token: str, username: str, password: str) -> dict[str, Any]:
        """His username and password from the app's sign-in form, typed into the page and
        sent (D142); masked for Orion at once, and offered to keep once he is done."""
        found = self.handover(token)
        if found is None:
            raise Refused("That hand-over is over.")
        live = self._live.get(found.thread)
        if live is not None and password:
            live.hidden.add(password)
        where: dict[str, str] = await self.on_tab(
            found.thread, lambda s: s.sign_in(username, password)
        )
        if password:
            self.note_typed(
                token, {"type": "password", "value": password, "username": username, **where}
            )
        elif username:
            self.note_typed(token, {"type": "text", "value": username})
        return where

    def note_typed(self, token: str, typed: dict[str, str] | None) -> None:
        """What he just typed in a hand-over, when it went into a sign-in form."""
        found = self.handovers.get(token)
        if found is None or not typed:
            return
        if typed.get("type") == "password" and typed.get("value"):
            live = self._live.get(found.thread)
            if live is not None:  # what he typed is masked on every page Orion reads, too
                live.hidden.add(typed["value"])
            found.typed.update(
                password=typed["value"],
                username=typed.get("username", "") or found.typed.get("username", ""),
                site=typed.get("host", ""),
                url=typed.get("url", ""),
            )
        elif typed.get("value"):
            found.typed["username"] = typed["value"]

    async def forget(self, site: str, ctx: ToolContext) -> str:
        """Signed out of a site (its cookies go), or of every site."""
        async with self._lock:
            await self._tab(ctx.thread_id)
            await self.session.forget(site)
            await self.session.keep_cookies()
        return f"Déconnecté de {site}." if site else "Déconnecté de tous les sites."

    async def screenshot(self, full: bool, ctx: ToolContext) -> bytes:
        async with self._lock:
            if ctx.thread_id not in self._live:
                raise Refused("No page is open: browser open first.")
            await self._tab(ctx.thread_id)
            return await self.session.screenshot(full)

    async def _act(self, ctx: ToolContext, action: Callable[[Session], Any]) -> str:
        async with self._lock:
            live = await self._tab(ctx.thread_id)
            session = self.session
            blocked_before = len(session.blocked)
            live.text = ""
            await action(session)
            snapshot = await session.snapshot(READ_CHARS)
            if live.hidden:
                snapshot.url = masked(snapshot.url, live.hidden)
                snapshot.text = masked(snapshot.text, live.hidden)
                snapshot.elements = [masked(e, live.hidden) for e in snapshot.elements]
            live.text = snapshot.text
            blocked = session.blocked[blocked_before:]
        await self._record(snapshot, ctx)
        return _render(snapshot, blocked)

    async def _record(self, snapshot: Snapshot, ctx: ToolContext) -> None:
        links = [m.group(1) for e in snapshot.elements if (m := _HREF.search(e))]
        await self._journal.append(
            [
                NewEvent(
                    kind=PAGE,
                    payload={
                        "thread_id": ctx.thread_id,
                        "url": snapshot.url,
                        "title": snapshot.title,
                        "links": [normalize(link) or link for link in links],
                        "images": [normalize(url) for url, _ in snapshot.images if normalize(url)],
                    },
                    provenance=Provenance(source=f"web:{snapshot.url}", actor="orion:browser"),
                    trust=Trust.WEB,
                    correlation_id=ctx.correlation_id,
                )
            ]
        )
        await self._runner.catch_up()


MOVING = (
    "[La page bougeait encore, ou disait encore d'attendre, au bout de quelques secondes : "
    "relis-la (browser read) avant de conclure qu'elle est bloquée.]"
)


def _render(snapshot: Snapshot, blocked: list[str]) -> str:
    text = snapshot.text[:SHOWN_CHARS]
    more = (
        f"\n[{len(snapshot.text) - SHOWN_CHARS} caractères de plus : browser read]"
        if len(snapshot.text) > SHOWN_CHARS
        else ""
    )
    lines = [
        f"URL : {snapshot.url}",
        f"Titre : {snapshot.title}",
        "",
        text + more,
        "",
        "Éléments (utilise leur numéro) :",
        *snapshot.elements,
    ]
    if snapshot.images:
        lines += ["", "Images de la page (à envoyer avec `file`, action download) :"]
        lines += [f"- {text or 'image'} — {url}" for url, text in snapshot.images]
    if not snapshot.steady:
        lines += ["", MOVING]
    if blocked:
        lines += [
            "",
            "Bloqué (une adresse du serveur lui-même ou de son réseau, jamais atteinte) : "
            + "; ".join(blocked[:3]),
        ]
    return snapshot.wall() + "\n".join(lines)


# -- tools --------------------------------------------------------------------------------------


class OpenInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(description="Any web address")


class ElementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    element: int = Field(ge=1, description="The element's number on the last page shown")


class TypeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    element: int = Field(ge=1)
    text: str = Field(max_length=200)
    enter: bool = Field(default=False, description="Press Enter after typing")


class SelectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    element: int = Field(ge=1)
    option: str = Field(max_length=120, description="The option's label, as shown")


class ScrollInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    down: bool = True


class ReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part: int = Field(default=1, ge=0, description="Which part of the page text (0 is the top)")
    find: str = Field(default="", max_length=80, description="Or: show where this text appears")


class NoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScreenshotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full: bool = Field(default=False, description="The whole page, not only what is visible")


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login: str = Field(max_length=60, description="A login in your vault (accounts)")
    url: str = Field(default="", max_length=500, description="The sign-in page to open")


class FillInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    element: int = Field(ge=1)
    field: Literal["username", "password", "code"] = Field(
        description="What to type there: his username, password, or the current 2FA code"
    )


class PasswordInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,40}$", description="e.g. doctolib")
    site: str = Field(max_length=300, description="The sign-in page's full address")
    why: str = Field(default="", max_length=300)


class HandOverInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    why: str = Field(max_length=200, description="For him, e.g. « Connecte-toi à Hyperplanning »")


class ForgetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site: str = Field(
        default="", max_length=200, description="e.g. index-education.net; empty: all"
    )


Keep = Callable[[str, bytes, str, ToolContext], Awaitable[str]]
"""(name, data, content type, ctx) → the id of the file kept (`orion.files`)."""


def browser_tools(desk: BrowserDesk, keep: Keep | None = None, looks: bool = False) -> list[Tool]:
    """`looks`: `attachment_look` can look at a screenshot (the photos module is on)."""

    def run(act: Callable[[Any, ToolContext], Any]) -> Any:
        async def handler(args: Any, ctx: ToolContext) -> ToolResult:
            try:
                return ToolResult(await act(args, ctx), trust=Trust.WEB)
            except (Refused, BrowserError) as exc:
                return ToolResult(str(exc), is_error=True)

        return handler

    async def screenshot(args: Any, ctx: ToolContext) -> ToolResult:
        if keep is None:  # pragma: no cover - the tool exists only with a store
            return ToolResult("Screenshots are not available.", is_error=True)
        try:
            data = await desk.screenshot(args.full, ctx)
        except (Refused, BrowserError) as exc:
            return ToolResult(str(exc), is_error=True)
        kind = "image/jpeg" if args.full else "image/png"
        file_id = await keep("capture", data, kind, ctx)
        look = (
            f" Tu ne la vois pas : `attachment_look` ({file_id}) te dit ce qu'elle montre."
            if looks
            else ""
        )
        return ToolResult(f"Capture prête ({file_id}). Envoie-la avec `file` (send).{look}")

    async def close(_args: Any, ctx: ToolContext) -> ToolResult:
        closed = await desk.close(ctx.thread_id)
        return ToolResult("Navigateur fermé." if closed else "Aucun navigateur ouvert.")

    def tool(
        name: str,
        description: str,
        model: type[BaseModel],
        handler: Any,
        effect: Effect = Effect.NONE,
    ) -> Tool:
        return Tool(
            name=name,
            description=description,
            input_model=model,
            handler=handler,
            effect=effect,
            reversibility=Reversibility.REVERSIBLE
            if effect is not Effect.EXTERNAL
            else Reversibility.IRREVERSIBLE,
            accepts_untrusted_context=effect is not Effect.EXTERNAL,
        )

    async def accounts(_args: Any, _ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult(await desk.accounts())
        except Refused as exc:
            return ToolResult(str(exc), is_error=True)

    async def save_password(args: Any, _ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult(await desk.save_password(args))
        except Refused as exc:
            return ToolResult(str(exc), is_error=True)

    async def hand_over(args: Any, ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult(await desk.hand_over(args.why, ctx))
        except Refused as exc:
            return ToolResult(str(exc), is_error=True)

    return [
        tool(
            "browser_open",
            "Open a page in your own browser and see it: its text and numbered elements. For "
            "browsing a site yourself, step by step (listings, schedules, a site's search, a "
            "form to send).",
            OpenInput,
            run(lambda a, c: desk.open(a.url, c)),
        ),
        tool(
            "browser_click",
            "Click an element of the page shown (link, button, tab). Then see the new page.",
            ElementInput,
            run(lambda a, c: desk.click(a.element, c)),
        ),
        tool(
            "browser_type",
            "Type into a field of the page shown, then optionally press Enter (to search, or "
            "to send a form).",
            TypeInput,
            run(lambda a, c: desk.type(a.element, a.text, a.enter, c)),
        ),
        tool(
            "browser_select",
            "Choose an option in a list of the page shown.",
            SelectInput,
            run(lambda a, c: desk.select(a.element, a.option, c)),
        ),
        tool(
            "browser_scroll",
            "Scroll the page shown (to load more results).",
            ScrollInput,
            run(lambda a, c: desk.scroll(a.down, c)),
        ),
        tool(
            "browser_back", "Go back to the previous page.", NoInput, run(lambda a, c: desk.back(c))
        ),
        tool(
            "browser_read",
            "More of the page shown: the next part of its text, or where a word appears.",
            ReadInput,
            run(lambda a, c: desk.read(a.part, a.find, c)),
        ),
        tool("browser_close", "Close your browser when you are done.", NoInput, close),
        tool(
            "browser_login",
            "A password from your vault, for a sign-in form: then fill types it "
            "without you seeing it.",
            LoginInput,
            run(lambda a, c: desk.login(a, c)),
            Effect.INTERNAL,
        ),
        tool(
            "browser_fill",
            "After login with a login: type his username, password or 2FA code into a field.",
            FillInput,
            run(lambda a, c: desk.fill(a.element, a.field, c)),
        ),
        tool(
            "browser_accounts",
            "The logins in your vault, by site.",
            NoInput,
            accounts,
        ),
        tool(
            "browser_hand_over",
            "A page needs him (a code sent to him, a captcha, a sign-in his vault cannot do): "
            "he gets this tab on his phone, does it (a sign-in form is filled from his phone's "
            "passwords), says he is done, and you resume. Your browser stays signed in "
            "afterwards.",
            HandOverInput,
            hand_over,
            Effect.INTERNAL,
        ),
        tool(
            "browser_forget",
            "Sign out of a site (its cookies go), or of all.",
            ForgetInput,
            run(lambda a, c: desk.forget(a.site, c)),
            Effect.INTERNAL,
        ),
        tool(
            "browser_save_password",
            "Ask him to put a site's login in your vault: a form in his app, filled from his "
            "phone's passwords. Then `login` and fill sign you in, now and later: for a plain "
            "sign-in, better than hand_over. Never ask for a password in a message.",
            PasswordInput,
            save_password,
            Effect.INTERNAL,
        ),
        *(
            [
                tool(
                    "browser_screenshot",
                    "A picture of the page shown, to send to the owner (`file` send).",
                    ScreenshotInput,
                    screenshot,
                )
            ]
            if keep is not None
            else []
        ),
    ]
