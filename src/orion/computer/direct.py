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
can be traced and contained (`memory.provenance`). Where a login went through is kept too:
the next sign-in, from any conversation, starts there rather than on the site's front page.

    browser.page
    browser.signed_in
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import mimetypes
import re
import secrets as tokens
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, quote_plus, urlsplit

import structlog
from playwright.async_api import Page
from pydantic import BaseModel, ConfigDict, Field

from orion.cognition.verifier import Verifier
from orion.computer.browser import KEPT_COOKIES, MAX_ELEMENTS, BrowserError, Session, Snapshot
from orion.computer.procedures import (
    ProcedureInput,
    Procedures,
    Seen,
    keep_procedure,
    settled_steps,
)
from orion.computer.vault import LocalVault, LoginKeeper, VaultError
from orion.core import busy
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.guardian.client import GuardianClient, GuardianUnavailableError
from orion.guardian.limits import host_in
from orion.guardian.logins import site_of
from orion.initiative.engine import Alert
from orion.runtime import OWNER_THREAD
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult
from orion.tools.outcome import Outcome
from orion.web.fetch import normalize
from orion.web.service import WebState

log = structlog.get_logger(__name__)

_ACTING: ContextVar[list[str] | None] = ContextVar("browser_acting", default=None)
"""Set by a tool call that acts on the page: the outcome ids of the pages it led to (D258)."""

PAGE = "browser.page"
SIGNED_IN = "browser.signed_in"
IDLE_SECONDS = 600.0
BROWSER_DEADLINE = 120.0
"""Seconds a browser call may take (ORION-25): Playwright's own timeouts are 3 to 30 s a step, so
this only cuts a browser that no longer answers, which held a turn 38 minutes on 06/11."""
DISCARD_SECONDS = 15.0
SHOWN_CHARS = 4000
"""Page text shown after each action; `browser` read gives the rest, part by part."""
READ_CHARS = 60_000
SEEN_TURNS = 8
"""Recent turns whose pages are held as the evidence a procedure rests on (D255)."""
SEEN_PAGES = 40
"""Pages held per turn, the last ones."""
SEEN_CHARS = 20_000
"""What is held of each of them."""
FOUND_ELEMENTS = 1000
"""How far `browser` read looks for an element by its words, past what a page shows."""
HANDS = "hands"
"""His own tab, when he opens the browser himself (D245): Orion's stays as it is."""
_HREF = re.compile(r" -> (https?://\S+)$")
_ELEMENT = re.compile(r"^\[\d+\] (\S+) ?(.*?)(?: -> \S+)?$")
PAYS = re.compile(
    r"\b(?:payer|pay|paypal|acheter maintenant|buy now|passer (?:la |ma |votre )?commande"
    r"|place (?:your |my )?order|submit order|complete (?:your )?(?:order|purchase)"
    r"|confirm(?:er)? (?:et payer|and pay|(?:la |ma |votre |your )?(?:commande|order)|l'achat"
    r"|le paiement|purchase|payment)|valider (?:et payer|(?:la |ma |votre )?commande"
    r"|le paiement|l'achat)|commande avec obligation de paiement)\b",
    re.IGNORECASE,
)
"""A button that pays, whatever the page (ORION-13, D265): a link does not, off a payment page."""
CHECKOUT_URL = re.compile(r"checkout|payment|paiement|/pay\b|billing|/buy/|/commande|/order", re.I)
CHECKOUT_TEXT = re.compile(
    r"num[ée]ro de (?:la )?carte|card number|\bcvv\b|\bcvc\b|cryptogramme|date d'expiration"
    r"|expir(?:y|ation) date|total à payer|montant à payer|order total|total de la commande",
    re.IGNORECASE,
)
"""A payment or purchase-confirmation page: by its address, or its card fields and total."""
SENDS = re.compile(
    r"^(?:button|submit|input\((?:submit|image|button)\))$"
    r"|\b(?:confirm|valid|command|order|achet|buy|purchase|submit|soumettre|finalis|terminer)",
    re.IGNORECASE,
)
"""On a payment page, what may send it: any button, or words that confirm (fail closed)."""


def pays(element: str | None, url: str, text: str) -> bool:
    """Whether acting on this element may pay or confirm a purchase (ORION-13, D265).
    `element`: its line as shown (« [n] tag label -> href »); None when it was not shown: on a
    payment page, the doubt is his."""
    checkout = bool(CHECKOUT_URL.search(urlsplit(url).path) or CHECKOUT_TEXT.search(text))
    if element is None:
        return checkout
    found = _ELEMENT.match(element)
    tag, label = (found.group(1), found.group(2)) if found else ("", element)
    link = " -> " in element
    if PAYS.search(label) and (checkout or not link):
        return True
    return checkout and bool(SENDS.search(tag) or SENDS.search(label))


@dataclass
class BrowsingState(WebState):
    """`WebState` plus the pages the browser showed and their links."""

    name: str = "browser"
    last: dict[str, str] = field(default_factory=dict[str, str])
    """Conversation → the last page it showed: a tab closed or lost is not a thread lost."""
    visited: set[str] = field(default_factory=set[str])
    """The sites Orion's browser went to: the rest of its cookies are their trackers."""
    signed_in: dict[str, str] = field(default_factory=dict[str, str])
    """Login → the address of the form it last went through: where its site signs in."""

    async def reset(self) -> None:
        await super().reset()
        self.last.clear()
        self.visited.clear()
        self.signed_in.clear()

    async def apply(self, event: Event) -> None:
        await super().apply(event)
        if event.redacted:
            return
        if event.kind == PAGE:
            payload: dict[str, Any] = event.payload
            self.allowed.add(str(payload["url"]))
            self.allowed.update(str(link) for link in payload.get("links", []))
            self.last[str(payload.get("thread_id", ""))] = str(payload["url"])
            site = site_of(str(payload["url"]))
            if site:
                self.visited.add(site)
        elif event.kind == SIGNED_IN:
            self.signed_in[str(event.payload["login"])] = str(event.payload["url"])


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
    others: list[Page] = field(default_factory=list[Page])
    """The tabs it came from, when a page opened another: closed with it, reached by `back`."""
    shown: Snapshot | None = None
    """The page as Orion last read it: the next action tells only what changed."""
    hinted: set[str] = field(default_factory=set[str])
    """Sites it was already told it has a login for, or not."""
    guided: set[str] = field(default_factory=set[str])
    """Sites it was already given the procedures of (ORION-5)."""


MASK = "••••••"
NO_VAULT = "No vault: the browser module has none (he fills it in the app: Mots de passe)."
HANDOVER_SECONDS = 30 * 60.0
BROWSING = frozenset(
    {
        "browser_open",
        "browser_click",
        "browser_pay",
        "browser_type",
        "browser_select",
        "browser_scroll",
        "browser_back",
        "browser_read",
        "browser_login",
        "browser_fill",
        "browser_press",
        "browser_hover",
        "browser_upload",
        "browser_view",
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
    asked: bool = True
    """Orion asked for his hands, and waits. False: he opened the browser himself (D245)."""


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
        self.procedures = Procedures()
        self._runner = ProjectionRunner(journal, [self.state, self.procedures])
        self.verifier: Verifier | None = None
        """Checks a démarche went through before its procedure is kept (ORION-5, D252)."""
        self._seen: dict[str, list[Seen]] = {}
        """Turn → the pages its browser showed, the evidence of a procedure (D255)."""
        self._live: dict[str, _Live] = {}
        """Conversation → its tab. One browser, one profile on disk (D116): its cookies, so its
        sign-ins, are the same in every tab and stay after a restart."""
        self._session: Session | None = None
        self._lock = asyncio.Lock()
        self._stuck = False
        """An action was cut midway: the next one starts on a new browser."""
        self.held: set[str] = set()
        """Tabs the owner has in hand (a hand-over): never closed for being idle."""
        self.handovers: dict[str, HandOver] = {}
        """One-time token → the tab the owner was handed (D116)."""
        self.finished: list[tuple[str, HandOver, str, datetime]] = []
        """Hand-overs he finished: (token, which, the page he left, when), for Orion to resume."""
        self.resume: Callable[[], Awaitable[object]] | None = None
        """« J'ai fini » as a message of his in the app: answered at once, like anything he
        says, where an alert could wait, or end in silence."""
        self.notify: Callable[[str, str], Awaitable[None]] | None = None
        """(text, address) → the owner gets a Telegram button opening that address."""
        self.base_url = ""
        """Where the hand-over page is served (the console's public address)."""
        self.in_app = False
        """The app shows a hand-over under way in Maintenant, with the live tab (D135)."""
        self.on_telegram: Callable[[], bool] = lambda: False
        """Whether he last wrote from Telegram: with the app there, the button goes only then."""
        self.profile: Path | None = None
        """The browser's folder: its kept cookies say where it is signed in (D245)."""
        self.keep_file: Keep | None = None
        """Where a file a page gave goes (`orion.files`)."""
        self.read_file: ReadFile | None = None
        """A kept file, to put into a form."""

    @property
    def session(self) -> Session:
        if self._session is None:  # pragma: no cover - only after `_tab`
            raise Refused("No page is open: browser open first.")
        return self._session

    async def _tab(self, thread: str) -> _Live:
        """The conversation's tab, the browser opened first if need be; the session then acts
        on it. Called under the lock."""
        if self._session is not None and (not self._session.alive or self._stuck):
            # Chromium died under it, or an action was cut at its deadline and the browser may
            # hang still (ORION-25): its tabs go with it, a new browser takes over (D171).
            log.warning("browser.relaunch", tabs=len(self._live), stuck=self._stuck)
            for live in self._live.values():
                if live.closer is not None:
                    live.closer.cancel()
            self._live.clear()
            self.held.clear()
            self._stuck = False
            session, self._session = self._session, None
            try:
                async with asyncio.timeout(DISCARD_SECONDS):
                    await session.discard()
            except TimeoutError:
                log.warning("browser.discard_timed_out", seconds=DISCARD_SECONDS)
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

    def _in_hand(self, thread: str) -> bool:
        now = time.monotonic()
        return thread in self.held and any(
            h.thread == thread and h.until > now for h in self.handovers.values()
        )

    async def _close_later(self, thread: str) -> None:
        while True:
            await asyncio.sleep(self._idle)
            async with self._lock:  # never under an action running on that tab
                if not self._in_hand(thread):  # a hand-over forgotten does not keep it for ever
                    await self.close(thread)
                    return

    def _follow(self, live: _Live) -> None:
        """The conversation goes where the page went: a tab it opened, or back from one."""
        now = self.session.current()
        if now is not live.page and not live.page.is_closed():
            live.others.append(live.page)
        live.page = now
        live.others = [p for p in live.others if p is not now and not p.is_closed()]

    async def _need_tab(self, thread: str) -> None:
        """No tab, no element to act on: where it was is told, not a blank page opened."""
        if thread in self._live and (self._session is None or self._session.alive):
            return
        await self._runner.catch_up()
        last = self.state.last.get(thread)
        if last is None:
            raise Refused("No page is open: browser open first.")
        raise Refused(
            f"Ton onglet s'est fermé (inactif, ou redémarrage), sa page était {last}. "
            "`browser` read la rouvre, avec de nouveaux numéros."
        )

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
        for other in live.others:
            if not other.is_closed():
                await other.close()
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
            live = await self._tab(thread)
            done = await action(self.session)
            self._follow(live)
            await self.session.keep_cookies_soon()  # he signed in: a restart must not undo it
            return done

    # -- actions ------------------------------------------------------------------------------

    async def open(self, url: str, ctx: ToolContext) -> str:
        address = normalize(url)
        if not address:
            raise Refused(f"{url} is not a web address (http or https).")
        return await self._act(ctx, lambda s: s.goto(address), what=f"open {address}")

    async def _on_page(
        self,
        ctx: ToolContext,
        action: Callable[[Session], Any],
        what: str,
        element: int = -1,
        sends: bool = False,
        form: bool = False,
    ) -> str:
        """`sends`: the action may send what the element belongs to (a click, a key): what pays
        is `pay`'s, which he approves (ORION-13, D265). `form`: Enter in a field, which sends
        its whole form: the page decides."""
        await self._need_tab(ctx.thread_id)
        live = self._live.get(ctx.thread_id)
        shown = live.shown.elements if live is not None and live.shown is not None else []
        line = next((e for e in shown if e.startswith(f"[{element}] ")), None)
        if (
            (sends or form)
            and live is not None
            and pays(None if form else line, live.page.url, live.text)
        ):
            raise Refused(
                f"This may pay or confirm a purchase ({line or 'on a payment page'}): never by "
                f"a plain {what}. Use `pay` with element={element if element > 0 else 'N'}, "
                f"page={live.page.url} and what (what is bought, for how much): he approves "
                "it, unless he let you pay on this site alone."
            )
        return await self._act(ctx, action, what=f"{what} {line or ''}".strip())

    async def click(self, element: int, ctx: ToolContext) -> str:
        return await self._on_page(ctx, lambda s: s.click(element), "click", element, sends=True)

    async def pay(self, element: int, page: str, ctx: ToolContext) -> str:
        """The click that pays, once he approved it or let Orion pay on this site (D265): on the
        page the approval named, never another."""
        await self._need_tab(ctx.thread_id)
        live = self._live[ctx.thread_id]
        here, named = urlsplit(live.page.url), urlsplit(page)
        if (here.hostname, here.path) != (named.hostname, named.path):
            raise Refused(
                f"The page is now {live.page.url}, not {page}: nothing was paid. Look at it "
                "again and pay on the page it is."
            )
        return await self._on_page(ctx, lambda s: s.click(element), "pay", element)

    async def type(self, element: int, text: str, enter: bool, ctx: ToolContext) -> str:
        return await self._on_page(
            ctx, lambda s: s.type(element, text, enter), "type", element, form=enter
        )

    async def select(self, element: int, option: str, ctx: ToolContext) -> str:
        what = f"select « {option} »"
        return await self._on_page(ctx, lambda s: s.select(element, option), what, element)

    async def scroll(self, down: bool, ctx: ToolContext) -> str:
        return await self._on_page(ctx, lambda s: s.scroll(down), "scroll")

    async def back(self, ctx: ToolContext) -> str:
        return await self._on_page(ctx, lambda s: s.back(), "back")

    async def press(self, key: str, element: int | None, ctx: ToolContext) -> str:
        what = f"press {key}"
        return await self._on_page(
            ctx,
            lambda s: s.press(key, element),
            what,
            -1 if element is None else element,
            sends=key in ("Enter", "Space"),
        )

    async def hover(self, element: int, ctx: ToolContext) -> str:
        return await self._on_page(ctx, lambda s: s.hover(element), "hover", element)

    async def view(self, screen: str, ctx: ToolContext) -> str:
        await self._need_tab(ctx.thread_id)
        return await self._act(ctx, lambda s: s.view(screen == "computer"), whole=True)

    async def upload(self, element: int, file_id: str, ctx: ToolContext) -> str:
        found = self.read_file(file_id) if self.read_file is not None else None
        if found is None:
            raise Refused(f"No file {file_id}: `file` lists those you have.")
        name, data, kind = found
        return await self._on_page(
            ctx, lambda s: s.upload(element, name, data, kind), "upload", element
        )

    async def _reopen(self, ctx: ToolContext) -> bool:
        """The conversation's tab closed (idle, a restart): its last page opens again. Whether
        it had to."""
        if ctx.thread_id in self._live and (self._session is None or self._session.alive):
            return False
        await self._runner.catch_up()
        last = self.state.last.get(ctx.thread_id)
        if last is None:
            raise Refused("No page is open: browser open first.")
        await self._act(ctx, lambda s: s.goto(last), whole=True)
        return True

    async def read(self, part: int, find: str, ctx: ToolContext) -> str:
        reopened = ""
        if await self._reopen(ctx):
            reopened = "[Ton onglet s'était fermé : rouvert sur sa dernière page.]\n\n"
        live = self._live[ctx.thread_id]

        async def look(_session: Session) -> None:
            return None

        # Always the page as it is now: what was read after the last action may have been a
        # « veuillez patienter » long gone (seen for real on Hyperplanning, D119).
        page = await self._act(
            ctx, look, whole=True, elements=FOUND_ELEMENTS if find else MAX_ELEMENTS
        )
        text = live.text
        if not find and (part == 0 or (part == 1 and len(text) <= SHOWN_CHARS)):
            return reopened + page  # it fits: the whole page, as it is now
        if find:
            shown = live.shown.elements if live.shown is not None else []
            live.shown = None  # read wider than a page: the next action shows it whole
            found = [e for e in shown if find.lower() in e.lower()][:20]
            spots = [m.start() for m in re.finditer(re.escape(find), text, re.IGNORECASE)][:5]
            if not spots and not found:
                return f"{reopened}« {find} » n'apparaît pas sur la page."
            said = [f"…{text[max(0, i - 300) : i + 700]}…" for i in spots]
            if found:
                said.append("Éléments (utilise leur numéro) :\n" + "\n".join(found))
            return reopened + "\n\n".join(said)
        start = part * SHOWN_CHARS
        if start >= len(text):
            return f"La page s'arrête avant la partie {part} ({len(text)} caractères)."
        chunk = text[start : start + SHOWN_CHARS]
        more = len(text) > start + SHOWN_CHARS
        return chunk + (f"\n[suite : browser read, part={part + 1}]" if more else "\n[fin]")

    async def _login_name(self, url: str, thread: str) -> str:
        """No login named: the vault's one for the page asked, or the page open."""
        if self.vault is None:
            raise Refused(NO_VAULT)
        live = self._live.get(thread)
        address = url or (live.page.url if live is not None else "")
        site = site_of(address)
        try:
            kept = await self.vault.accounts()
        except (GuardianUnavailableError, VaultError) as exc:
            raise Refused(f"The vault did not answer: {exc}") from exc
        for login in kept["logins"]:
            if site and site in login["sites"]:
                return str(login["name"])
        raise Refused(
            f"Ton coffre n'a rien pour {site or 'cette page'} : save_password lui demande "
            "l'identifiant, ou hand_over s'il faut qu'il se connecte lui-même."
        )

    async def login(self, args: Any, ctx: ToolContext) -> str:
        """A login from the vault (D111, D120) typed into the page's sign-in form and sent, in
        one go (D245): Orion never sees it. Opens `url` or the login's own page, unless it is
        already on the login's site, a step or a button further."""
        vault = self.vault
        if vault is None:
            raise Refused(NO_VAULT)
        name = args.login or await self._login_name(str(args.url), ctx.thread_id)
        try:
            lent = await vault.lend_login(name, {"action": "login", "login": name}, ctx.grant)
        except (GuardianUnavailableError, VaultError) as exc:
            raise Refused(f"The vault did not lend it: {exc}") from exc
        sites = list(lent["sites"])
        await self._runner.catch_up()
        start = normalize(str(args.url)) if args.url else ""
        start = (
            start
            or self.state.signed_in.get(name, "")
            or str(lent.get("url") or "")
            or (f"https://{sites[0]}/" if sites else "")
        )
        async with self._lock:
            live = await self._tab(ctx.thread_id)
            loan = str(lent["loan"])

            async def code() -> str:
                return await vault.login_code(name, loan)

            live.login = {**lent, "code": code}
            live.hidden.add(str(lent["password"]))
            here = urlsplit(live.page.url).hostname or ""
            go = start if args.url or not host_in(here, sites) else ""
        sent: dict[str, bool] = {}
        where = ""
        still = [False]

        async def signing_in(session: Session) -> None:
            nonlocal sent, where
            if go:
                await session.goto(go)
                await session.ready()
            sent, where = await session.sign_in_lent(
                str(lent["username"]), str(lent["password"]), sites
            )
            still[0] = bool(sent.get("password")) and await session.shows_password_field()

        said = await self._act(ctx, signing_in, whole=True)
        if not sent.get("username") and not sent.get("password"):
            head = (
                f"Aucun formulaire de connexion de {', '.join(sites)} sur cette page : rien n'a "
                "été tapé, et rien ne dit que le mot de passe soit faux. Sa page de connexion "
                "est ailleurs (lien « Connexion », « Espace client », un sous-domaine) : "
                "va dessus, puis `browser` login à nouveau."
            )
        elif still[0]:
            head = (
                "Identifiant envoyé, mais le formulaire de connexion est encore là : mot de passe "
                "refusé, captcha ou étape de plus. Regarde la page ; en dernier recours, hand_over."
            )
        else:
            head = (
                "Identifiant tapé et envoyé (tu ne le vois jamais). Un code demandé ensuite : fill "
                "avec field=code, s'il y a sa clé, sinon demande-le-lui."
            )
            if sent.get("password") and where and where != self.state.signed_in.get(name):
                await self._signed_in(name, where, ctx)
        return f"{head}\n\n{said}"

    async def _signed_in(self, name: str, url: str, ctx: ToolContext) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=SIGNED_IN,
                    payload={"login": name, "url": url},
                    provenance=Provenance(source=f"web:{url}", actor="orion:browser"),
                    trust=Trust.WEB,
                    correlation_id=ctx.correlation_id,
                )
            ]
        )
        await self._runner.catch_up()

    async def fill(self, element: int, what: str, ctx: ToolContext) -> str:
        await self._need_tab(ctx.thread_id)
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
        busy.hold(HANDOVER_SECONDS)  # a restart would take the tab from under him
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
            live.shown = None
            self._arm(found.thread, live)
        if not found.asked:  # he opened it himself: nobody waits, his tab goes when he is done
            if self._session is not None:
                await self._session.keep_cookies()
            await self._offer_typed(found, live)
            async with self._lock:
                await self.close(found.thread)
            return url
        if done:
            if self._session is not None:
                await self._session.keep_cookies()  # signed in: kept at once
            await self._offer_typed(found, live)
            if self.resume is not None and found.thread == OWNER_THREAD and not self.on_telegram():
                await self.resume()
            else:
                self.finished.append((token, found, url, datetime.now(UTC)))
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

    # -- the browser in his own hands, when he wants it (D245) -----------------------------------

    async def take(self, url: str = "") -> dict[str, str]:
        """He opens Orion's browser himself: to sign in once and for all, or just to see. A tab
        of his own, with the same sign-ins; nothing is asked of Orion."""
        address = normalize(url) if url else ""
        if url and not address:
            raise Refused(f"{url} n'est pas une adresse web.")
        why = "Ton navigateur, avec mes connexions : ce que tu y fais, je le garde."
        async with self._lock:
            await self._tab(HANDS)
            self.held.add(HANDS)
            if address:
                await self.session.goto(address)
        token = tokens.token_urlsafe(24)
        now = time.monotonic()
        self.handovers = {t: h for t, h in self.handovers.items() if h.until > now}
        self.handovers[token] = HandOver(HANDS, why, now + HANDOVER_SECONDS, asked=False)
        busy.hold(HANDOVER_SECONDS)
        return {"token": token, "why": why}

    async def sites(self) -> list[str]:
        """The sites its browser keeps cookies of, among those it went to: not all signed in,
        but all it would be signed in to."""
        await self._runner.catch_up()
        async with self._lock:
            if self._session is not None and self._session.alive:
                await self._session.keep_cookies()
        kept = self.profile / KEPT_COOKIES if self.profile is not None else None
        if kept is None or not kept.is_file():
            return []
        now = time.time()
        found: set[str] = set()
        for cookie in json.loads(kept.read_text(encoding="utf-8")):
            expires = float(cookie.get("expires", -1))
            site = site_of(str(cookie.get("domain", "")).lstrip("."))
            if site in self.state.visited and (expires < 0 or expires > now):
                found.add(site)
        return sorted(found)

    async def sign_out(self, site: str) -> None:
        """From the app: signed out of a site, the browser opened for that if need be."""
        async with self._lock:
            mine = HANDS not in self._live
            await self._tab(HANDS)
            await self.session.forget(site)
            await self.session.keep_cookies()
            if mine:
                await self.close(HANDS)

    async def forget(self, site: str, ctx: ToolContext) -> str:
        """Signed out of a site (its cookies go), or of every site."""
        async with self._lock:
            await self._tab(ctx.thread_id)
            await self.session.forget(site)
            await self.session.keep_cookies()
        return f"Déconnecté de {site}." if site else "Déconnecté de tous les sites."

    async def screenshot(self, full: bool, ctx: ToolContext) -> bytes:
        # « Envoie une capture » a while later: the tab had closed, and he got « no page »
        # (05/11). Like `read`, it opens the last page again.
        await self._reopen(ctx)
        async with self._lock:
            await self._tab(ctx.thread_id)
            return await self.session.screenshot(full)

    @contextlib.asynccontextmanager
    async def _cut_means_stuck(self) -> AsyncGenerator[None]:
        """An action cut at its deadline (`BROWSER_DEADLINE`) leaves the browser in an unknown
        state: the lock is released on the way out, and the next action relaunches it."""
        try:
            yield
        except asyncio.CancelledError:
            self._stuck = True
            log.warning("browser.action_cut")
            raise

    async def _act(
        self,
        ctx: ToolContext,
        action: Callable[[Session], Any],
        whole: bool = False,
        elements: int = MAX_ELEMENTS,
        what: str = "read",
    ) -> str:
        async with self._lock, self._cut_means_stuck():
            live = await self._tab(ctx.thread_id)
            session = self.session
            blocked_before = len(session.blocked)
            before = None if whole else live.shown
            acted_on = site_of(live.shown.url) if live.shown is not None else ""
            live.text = ""
            live.shown = None
            await action(session)
            self._follow(live)
            snapshot = await session.snapshot(READ_CHARS, elements)
            await session.files_landed()
            self._follow(live)
            if live.hidden:
                snapshot.url = masked(snapshot.url, live.hidden)
                snapshot.text = masked(snapshot.text, live.hidden)
                snapshot.elements = [masked(e, live.hidden) for e in snapshot.elements]
            live.text = snapshot.text
            live.shown = snapshot
            blocked = session.blocked[blocked_before:]
            notes = [masked(n, live.hidden) for n in session.take_notes()]
            files = session.take_files()
            await session.keep_cookies_soon()
            hint = await self._login_hint(session, live, snapshot.url)
            guide = await self._guide(live, snapshot.url)
            self._saw(ctx, what, snapshot, acted_on)
        await self._record(snapshot, ctx)
        notes += [await self._keep_given(name, data, ctx) for name, data in files]
        said = _changes(before, snapshot) if before is not None else None
        said = said if said is not None else _render(snapshot, blocked)
        return "\n\n".join([*([guide] if guide else []), *([hint] if hint else []), said, *notes])

    async def _guide(self, live: _Live, url: str) -> str:
        """The démarches that went through on this site before, once per site and tab: kept
        from any conversation, so the journal is read up to now first."""
        site = site_of(url)
        if not site or site in live.guided:
            return ""
        await self._runner.catch_up()
        guide = self.procedures.hint(url)
        if guide:
            live.guided.add(site)
        return guide

    def _saw(self, ctx: ToolContext, what: str, snapshot: Snapshot, acted_on: str = "") -> None:
        """What the browser showed in this turn, the evidence a procedure rests on (ORION-5).
        Held in memory, a few recent turns: after a restart nothing is kept (fail closed)."""
        turn = self._seen.pop(ctx.correlation_id, [])
        self._seen[ctx.correlation_id] = turn  # the newest turn last
        text = "\n".join([snapshot.title, snapshot.text, *snapshot.elements])
        action_id = ""
        if (acting := _ACTING.get()) is not None:
            # One id per call that acts: confirming a démarche settles its own steps, never
            # another one on the same site (D258).
            site = acted_on or site_of(snapshot.url) or "page"
            action_id = f"browser:{site}#{tokens.token_hex(4)}"
            acting.append(action_id)
        landed = site_of(snapshot.url)
        turn.append(
            Seen(
                what,
                snapshot.url,
                text[:SEEN_CHARS],
                action_id,
                acted_on if acted_on and acted_on != landed else "",
            )
        )
        del turn[:-SEEN_PAGES]
        while len(self._seen) > SEEN_TURNS:
            del self._seen[next(iter(self._seen))]

    async def save_procedure(
        self, args: ProcedureInput, ctx: ToolContext
    ) -> tuple[Outcome | None, list[str], str]:
        seen = list(self._seen.get(ctx.correlation_id, []))
        logins = dict(self.state.signed_in)
        kept, said = await keep_procedure(self._journal, self.verifier, args, ctx, seen, logins)
        if kept is not None:
            await self._runner.catch_up()
        settled = settled_steps(args, seen) if kept is Outcome.CONFIRMED else []
        left = [a for a in args.actions if a not in settled]
        if left and kept is Outcome.CONFIRMED:
            said += f" Pas confirmées, hors de cette démarche : {', '.join(left)}."
        return kept, settled, said

    async def _keep_given(self, name: str, data: bytes, ctx: ToolContext) -> str:
        """A file the page gave: kept like a download, worth what the page is worth."""
        if self.keep_file is None:
            return f"[La page a donné le fichier {name}, mais je n'ai nulle part où le garder.]"
        kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        try:
            kept = await self.keep_file(name, data, kind, ctx, "download")
        except ValueError as exc:
            return f"[La page a donné le fichier {name}, pas gardé : {exc}.]"
        return (
            f"[La page a donné un fichier : {name} ({kept}). `attachment_look` pour le lire, "
            "`file` send pour le lui envoyer.]"
        )

    async def _login_hint(self, session: Session, live: _Live, url: str) -> str:
        """A sign-in page, once per site: whether the vault can do it, rather than guess."""
        site = site_of(url)
        if self.vault is None or not site or site in live.hinted:
            return ""
        form = await session.sign_in_form()
        if form is None or not form["password"]:
            return ""
        live.hinted.add(site)
        try:
            kept = await self.vault.accounts()
        except (GuardianUnavailableError, VaultError) as exc:
            log.warning("browser.vault_silent", error=str(exc))
            return ""
        for login in kept["logins"]:
            if site in login["sites"]:
                return f"[Page de connexion. Ton coffre a « {login['name']} » : `browser` login.]"
        return (
            f"[Page de connexion. Ton coffre n'a rien pour {site} : save_password, ou hand_over.]"
        )

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


NOTHING_CHANGED = (
    "[Rien n'a changé sur la page : elle et ses numéros sont comme à ta dernière lecture. Si tu "
    "attendais autre chose : hover, press, une capture (screenshot), ou view computer.]"
)


def _changes(before: Snapshot, after: Snapshot) -> str | None:
    """What an action changed on the page, rather than all of it again; None when it is another
    page, or so changed that it is better read whole."""
    if before.url != after.url or before.title != after.title or after.blocked:
        return None
    old_lines = set(before.text.splitlines())
    new_lines = after.text.splitlines()
    added = [line for line in new_lines if line.strip() and line not in old_lines]
    kept_lines = set(new_lines)
    gone = [line for line in before.text.splitlines() if line.strip() and line not in kept_lines]
    old_elements = set(before.elements)
    new_elements = set(after.elements)
    came = [e for e in after.elements if e not in old_elements]
    left = [e.split(" ", 1)[0] for e in before.elements if e not in new_elements]
    moving = [MOVING] if not after.steady else []
    if not (added or gone or came or left):
        return "\n".join([f"URL : {after.url}", NOTHING_CHANGED, *moving])
    if len(added) + len(came) > max(6, (len(new_lines) + len(after.elements)) // 2):
        return None
    lines = [f"URL : {after.url}", "Même page ; ce qui a changé (`browser` read : tout) :"]
    lines += [f"+ {line}" for line in added]
    lines += [f"- {line}" for line in gone[:10]]
    if came:
        lines += ["Éléments nouveaux ou changés :", *came]
    if left:
        lines.append("Éléments partis : " + " ".join(left[:30]))
    said = "\n".join([*lines, *moving])
    return said if len(said) <= SHOWN_CHARS else None


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
    if snapshot.omitted:
        lines += [f"[{snapshot.omitted} autres éléments plus bas : browser read, avec find]"]
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


class PayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    element: int = Field(ge=1, description="The button that pays, on the page shown")
    page: str = Field(max_length=2000, description="The address of the page shown")
    what: str = Field(max_length=300, description="What is bought, from whom, for how much")


class TypeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    element: int = Field(ge=1)
    text: str = Field(max_length=5000)
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


class PressInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Literal[
        "Enter",
        "Escape",
        "Tab",
        "Space",
        "Backspace",
        "ArrowDown",
        "ArrowUp",
        "ArrowLeft",
        "ArrowRight",
        "PageDown",
        "PageUp",
        "Home",
        "End",
    ]
    element: int | None = Field(default=None, ge=1, description="Empty: what has the focus")


class UploadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    element: int = Field(ge=1, description="The file field, or the button that opens the picker")
    file: str = Field(pattern=r"^fi_", max_length=60, description="A file you have (`fi_…`)")


class ViewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    screen: Literal["computer", "phone"]


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login: str = Field(
        default="", max_length=60, description="A login in your vault; empty: the page's site's"
    )
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


Keep = Callable[[str, bytes, str, ToolContext, str], Awaitable[str]]
"""(name, data, content type, ctx, how it came) → the id of the file kept (`orion.files`)."""
ReadFile = Callable[[str], tuple[str, bytes, str] | None]
"""A kept file's id → (name, data, content type)."""


def browser_tools(desk: BrowserDesk, keep: Keep | None = None, looks: bool = False) -> list[Tool]:
    """`looks`: `attachment_look` can look at a screenshot (the photos module is on)."""
    desk.keep_file = keep

    def run(act: Callable[[Any, ToolContext], Any], acts: bool = False) -> Any:
        """`acts`: the call acts on the page (click, typing, a choice, a key, a file): it is
        attempted, never confirmed by itself; `browser_save_procedure` confirms it (D255, D258)."""

        async def handler(args: Any, ctx: ToolContext) -> ToolResult:
            acting: list[str] = []
            token = _ACTING.set(acting if acts else None)
            try:
                said = await act(args, ctx)
            except (Refused, BrowserError) as exc:
                return ToolResult(str(exc), is_error=True)
            finally:
                _ACTING.reset(token)
            if not acts:
                return ToolResult(said, trust=Trust.WEB)
            if not acting:  # pragma: no cover - every act shows the page it led to
                live = desk.tab(ctx.thread_id)
                url = live.shown.url if live is not None and live.shown is not None else ""
                acting.append(f"browser:{site_of(url) or 'page'}#{tokens.token_hex(4)}")
            return ToolResult(
                said, trust=Trust.WEB, outcome=Outcome.ATTEMPTED, action_id=",".join(acting)
            )

        return handler

    async def screenshot(args: Any, ctx: ToolContext) -> ToolResult:
        if keep is None:  # pragma: no cover - the tool exists only with a store
            return ToolResult("Screenshots are not available.", is_error=True)
        try:
            data = await desk.screenshot(args.full, ctx)
        except (Refused, BrowserError) as exc:
            return ToolResult(str(exc), is_error=True)
        kind = "image/jpeg" if args.full else "image/png"
        file_id = await keep("capture", data, kind, ctx, "screenshot")
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

    async def save_procedure(args: Any, ctx: ToolContext) -> ToolResult:
        kept, settled, said = await desk.save_procedure(args, ctx)
        # The verifier's words may quote the pages: worth what they are worth.
        if kept is None:
            return ToolResult(said, is_error=True, trust=Trust.WEB)
        # The steps of this démarche only, by their ids: another one on the site keeps its own
        # outcome (D258).
        return ToolResult(
            said,
            trust=Trust.WEB,
            outcome=kept,
            action_id=",".join(settled) or f"browser:{site_of(args.site)}",
            proof=args.proof if kept is Outcome.CONFIRMED else "",
        )

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
            run(lambda a, c: desk.click(a.element, c), acts=True),
        ),
        tool(
            "browser_pay",
            "The click that pays or confirms a purchase, which click refuses: he approves it, "
            "unless he let you pay on this site alone.",
            PayInput,
            run(lambda a, c: desk.pay(a.element, a.page, c), acts=True),
            Effect.EXTERNAL,
        ),
        tool(
            "browser_type",
            "Type into a field of the page shown, then optionally press Enter (to search, or "
            "to send a form).",
            TypeInput,
            run(lambda a, c: desk.type(a.element, a.text, a.enter, c), acts=True),
        ),
        tool(
            "browser_select",
            "Choose an option in a list of the page shown.",
            SelectInput,
            run(lambda a, c: desk.select(a.element, a.option, c), acts=True),
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
            "The page as it is now, whole; or the next part of its text; or, with find, where "
            "a word appears in it and which elements bear it.",
            ReadInput,
            run(lambda a, c: desk.read(a.part, a.find, c)),
        ),
        tool(
            "browser_press",
            "Press a key, on an element or on what has the focus: Escape closes a menu, the "
            "arrows move in a list.",
            PressInput,
            run(lambda a, c: desk.press(a.key, a.element, c), acts=True),
        ),
        tool(
            "browser_hover",
            "Put the pointer over an element: a menu that opens only then.",
            ElementInput,
            run(lambda a, c: desk.hover(a.element, c)),
        ),
        tool(
            "browser_view",
            "Show this tab as a computer's or a phone's, and reload it: a site that gives a "
            "phone only half of itself, or turns one away. Your browser is a phone's until then.",
            ViewInput,
            run(lambda a, c: desk.view(a.screen, c)),
        ),
        tool("browser_close", "Close your browser when you are done.", NoInput, close),
        tool(
            "browser_login",
            "Sign in with your vault, in one go: opens the sign-in page, types his username "
            "and password into the form and sends it, without you seeing them. No form yet: "
            "reach it, then login again.",
            LoginInput,
            run(lambda a, c: desk.login(a, c)),
            Effect.INTERNAL,
        ),
        tool(
            "browser_fill",
            "After login: type his username, password or 2FA code into one field (for a code, "
            "or a form login did not find).",
            FillInput,
            run(lambda a, c: desk.fill(a.element, a.field, c), acts=True),
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
        tool(
            "browser_save_procedure",
            "Once a démarche went through on a site (a page shows it done): keep how, the path, "
            "the login, the traps, so the next visit follows it instead of exploring. Kept only "
            "if what the pages showed this turn confirms it.",
            ProcedureInput,
            save_procedure,
            Effect.INTERNAL,
        ),
        *(
            [
                tool(
                    "browser_upload",
                    "Put a file you have into a form of the page: a document of his goes out "
                    "to that site, so he approves.",
                    UploadInput,
                    run(lambda a, c: desk.upload(a.element, a.file, c), acts=True),
                    Effect.EXTERNAL,
                )
            ]
            if desk.read_file is not None
            else []
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
