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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote, quote_plus

import structlog
from pydantic import BaseModel, ConfigDict, Field

from orion.computer.browser import BrowserError, Session, Snapshot
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.guardian.client import GuardianClient, GuardianUnavailableError
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
    session: Session
    closer: asyncio.Task[None] | None = None
    text: str = ""
    login: dict[str, Any] | None = None
    """A login the guardian lent for this browser: username, password, sites, code (D111)."""
    hidden: set[str] = field(default_factory=set[str])
    """What Orion must never read: the password, and the codes typed."""


MASK = "••••••"


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
    ) -> None:
        self._journal = journal
        self._sessions = sessions
        self.guardian = guardian
        self._idle = idle_seconds
        self.state = BrowsingState()
        self._runner = ProjectionRunner(journal, [self.state])
        self._live: dict[str, _Live] = {}
        self._lock = asyncio.Lock()

    async def _browser(self, thread: str, state: dict[str, Any] | None = None) -> _Live:
        live = self._live.get(thread)
        if live is not None and state is not None:  # a lent session: a browser that holds it
            await self.close(thread)
            live = None
        if live is None:
            session = self._sessions()
            session.storage_state = state
            await session.__aenter__()
            live = self._live[thread] = _Live(session)
        if live.closer is not None:
            live.closer.cancel()
        live.closer = asyncio.create_task(self._close_later(thread))
        return live

    async def _close_later(self, thread: str) -> None:
        await asyncio.sleep(self._idle)
        await self.close(thread)

    async def close(self, thread: str) -> bool:
        live = self._live.pop(thread, None)
        if live is None:
            return False
        if live.closer is not None and live.closer is not asyncio.current_task():
            live.closer.cancel()
        await live.session.__aexit__(None, None, None)
        log.info("browser.closed", thread=thread)
        return True

    async def aclose(self) -> None:
        for thread in list(self._live):
            await self.close(thread)

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
        if live is None or not live.text:
            raise Refused("No page is open: browser open first.")
        text = live.text
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
        """Open the owner's account: his session, or his login (D113), lent by the guardian when
        Orion asks (D115). It opens `url`, or the page kept with the account."""
        if not args.session and not args.login:
            raise Refused("Name a session or a login (`browser` accounts lists them).")
        if self.guardian is None:
            raise Refused("Sessions and logins are kept by the guardian, which is not running.")
        params = {"action": "login", "session": args.session, "login": args.login}
        lent: dict[str, Any] | None = None
        sites: list[str] = []
        try:
            if args.login:
                lent = await self.guardian.lend_login(args.login, params, ctx.grant)
                sites = list(lent["sites"])
            state = (
                await self.guardian.lend_session(args.session, params, ctx.grant)
                if args.session
                else None
            )
        except GuardianUnavailableError as exc:
            raise Refused(f"The guardian did not lend it: {exc}") from exc
        if state is not None:
            sites = sites or list(state.get("domains") or [])
        start = normalize(str(args.url)) if args.url else ""
        if lent is not None:
            start = start or str(lent.get("url") or "")
        if state is not None:  # the page kept with the session, the one Orion chose (D115)
            start = start or str(dict(state["state"].get("orion") or {}).get("url") or "")
        start = start or (f"https://{sites[0]}/" if sites else "")
        if not start:
            raise Refused("This account names no site.")
        async with self._lock:
            live = await self._browser(ctx.thread_id, state["state"] if state else None)
            if lent is not None:
                guardian, name, loan = self.guardian, args.login, str(lent["loan"])

                async def code() -> str:
                    return await guardian.login_code(name, loan)

                live.login = {**lent, "code": code}
                live.hidden.add(str(lent["password"]))
        said = await self._act(ctx, lambda s: s.goto(start))
        how = (
            "Identifiant prêt : `browser` fill tape l'identifiant, le mot de passe ou le code "
            "(tu ne les vois jamais), puis clique pour te connecter.\n\n"
            if lent is not None
            else "Ouvert avec sa session.\n\n"
        )
        return how + said

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
            except GuardianUnavailableError as exc:
                raise Refused(f"No code from the guardian: {exc}") from exc
            live.hidden.add(value)
        else:
            value = str(login[what])
        sites = list(login["sites"])
        return await self._act(ctx, lambda s: s.fill_secret(element, value, sites, what))

    async def accounts(self) -> str:
        if self.guardian is None:
            return "Le gardien ne tourne pas : aucun compte."
        try:
            kept = await self.guardian.accounts()
        except GuardianUnavailableError as exc:
            raise Refused(f"The guardian did not answer: {exc}") from exc
        lines = [f"- session « {n} » : {', '.join(s)}" for n, s in kept["sessions"].items()]
        lines += [
            f"- identifiant « {k['name']} » : {', '.join(k['sites'])}"
            + (" (avec la double authentification)" if k["totp"] else "")
            for k in kept["logins"]
        ]
        return "\n".join(lines) or "Aucun compte gardé : `browser` connect pour lui en demander."

    async def connect(self, args: Any) -> str:
        if self.guardian is None:
            raise Refused("Sessions and logins are kept by the guardian, which is not running.")
        try:
            if args.keep == "session":
                await self.guardian.ask_session(args.name, args.site, args.why)
            else:
                await self.guardian.ask_login(args.name, args.site, args.why)
        except GuardianUnavailableError as exc:
            raise Refused(f"Not asked: {exc}") from exc
        if args.keep == "session":
            return (
                "Il a reçu un bouton pour se connecter lui-même, depuis son téléphone, dans le "
                "navigateur du gardien. Quand le gardien dit que c'est gardé : `browser` login "
                f'avec session "{args.name}".'
            )
        return (
            f"Il a reçu un bouton pour saisir son identifiant « {args.name} » dans le formulaire "
            "du gardien. Tu ne verras jamais le mot de passe : quand le gardien dit qu'il est "
            f'gardé, `browser` login avec login "{args.name}".'
        )

    async def screenshot(self, full: bool, ctx: ToolContext) -> bytes:
        async with self._lock:
            live = self._live.get(ctx.thread_id)
            if live is None:
                raise Refused("No page is open: browser open first.")
            return await live.session.screenshot(full)

    async def _act(self, ctx: ToolContext, action: Callable[[Session], Any]) -> str:
        async with self._lock:
            live = await self._browser(ctx.thread_id)
            blocked_before = len(live.session.blocked)
            live.text = ""
            await action(live.session)
            snapshot = await live.session.snapshot(READ_CHARS)
            if live.hidden:
                snapshot.url = masked(snapshot.url, live.hidden)
                snapshot.text = masked(snapshot.text, live.hidden)
                snapshot.elements = [masked(e, live.hidden) for e in snapshot.elements]
            live.text = snapshot.text
            blocked = live.session.blocked[blocked_before:]
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
    if blocked:
        lines += [
            "",
            "Bloqué (une adresse du serveur lui-même ou de son réseau, jamais atteinte) : "
            + "; ".join(blocked[:3]),
        ]
    return "\n".join(lines)


# -- tools --------------------------------------------------------------------------------------


class OpenInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(description="An address the owner gave, a search found, or a page linked")


class ElementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    element: int = Field(ge=1, description="The element's number on the last page shown")


class TypeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    element: int = Field(ge=1)
    text: str = Field(max_length=200, description="Only words from the owner's message")
    enter: bool = Field(default=False, description="Press Enter after typing (to search)")


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

    session: str = Field(default="", max_length=60, description="A session the guardian keeps")
    login: str = Field(
        default="",
        max_length=60,
        description="A login it keeps (to sign in when the session expired, or none)",
    )
    url: str = Field(default="", max_length=500, description="A page of that account to open")


class FillInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    element: int = Field(ge=1)
    field: Literal["username", "password", "code"] = Field(
        description="What to type there: his username, password, or the current 2FA code"
    )


class ConnectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,40}$", description="e.g. doctolib")
    site: str = Field(
        max_length=300,
        description="The full address of the page to keep with the account: where he signs "
        "in, or the page you will come back to (his timetable…), not just the domain",
    )
    keep: Literal["session", "password"] = Field(
        default="session",
        description="session: he signs in himself from his phone, in the guardian's browser "
        "(SMS codes, captchas work); password: the guardian keeps his password, for you to sign "
        "in again when a session expires",
    )
    why: str = Field(default="", max_length=300)


Keep = Callable[[str, bytes, str, ToolContext], Awaitable[str]]
"""(name, data, content type, ctx) → the id of the file kept (`orion.files`)."""


def browser_tools(desk: BrowserDesk, keep: Keep | None = None) -> list[Tool]:
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
        return ToolResult(f"Capture prête ({file_id}). Envoie-la avec `file` (send).")

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

    async def connect(args: Any, _ctx: ToolContext) -> ToolResult:
        try:
            return ToolResult(await desk.connect(args))
        except Refused as exc:
            return ToolResult(str(exc), is_error=True)

    return [
        tool(
            "browser_open",
            "Open a page in your own browser and see it: its text and numbered elements. For "
            "browsing a site yourself, step by step (listings, schedules, a site's search). "
            "Only addresses the owner gave, a search found, or a page you opened links to.",
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
            "Type into a field of the page shown, then optionally press Enter. Only words the "
            "owner wrote in the message you answer; nothing is ever sent (forms are blocked).",
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
            "Open one of the owner's accounts: with his session, or his login (you type it "
            "with fill without seeing it).",
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
            "The owner's accounts the guardian keeps (sessions, logins).",
            NoInput,
            accounts,
        ),
        tool(
            "browser_connect",
            "Ask him to sign in to an account from his phone (session) or give his password "
            "to the guardian's form. Never ask for a password in a message.",
            ConnectInput,
            connect,
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
