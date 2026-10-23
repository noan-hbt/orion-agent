"""The browser: headless Chromium, a fresh profile per job, guarded at the network layer.

Two rules hold whatever the worker decides, because they are enforced on every request the
page makes, not by asking the model:

- private, loopback and link-local addresses are never reached (the guardian, the server's
  own services, the local network);
- a read-only job sends nothing: every request but GET and HEAD is aborted, so no form is
  submitted, no message posted, nothing bought.

Downloads are refused and nothing persists between jobs (no cookies, no history).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    Error,
    Frame,
    Locator,
    Page,
    Playwright,
    Route,
    async_playwright,
)
from playwright.async_api import Request as PwRequest

from orion.guardian.limits import host_in
from orion.web.fetch import FetchError, check_public

log = structlog.get_logger(__name__)

TEXT_CHARS = 10_000
MAX_ELEMENTS = 150
MAX_IMAGES = 8
KEPT_COOKIES = "cookies.json"
HostCheck = Callable[[str, int], Awaitable[None]]

_SNAPSHOT = """({textChars, start}) => {
  document.querySelectorAll('[data-orion-id]').forEach(e => e.removeAttribute('data-orion-id'));
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, ' +
              '[role=button], [role=link], [role=checkbox], [role=tab], [onclick]';
  const out = [];
  let n = start;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    if (getComputedStyle(el).visibility === 'hidden') continue;
    n += 1;
    el.setAttribute('data-orion-id', String(n));
    const tag = el.tagName.toLowerCase();
    const kind = el.type && tag !== 'a' ? `(${el.type})` : '';
    const value = el.type === 'password' ? '' : el.value;
    const label = (el.innerText || value || el.placeholder || el.getAttribute('aria-label') ||
                   el.title || el.name || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
    const href = tag === 'a' && el.href ? ` -> ${el.href}` : '';
    out.push(`[${n}] ${tag}${kind} ${label}${href}`);
    if (n - start >= MAX_ELEMENTS) break;
  }
  const pictures = [];
  const meta = document.querySelector('meta[property="og:image"], meta[name="twitter:image"]');
  if (meta && meta.content) pictures.push({url: new URL(meta.content, location.href).href,
                                           text: 'aperçu de la page', area: Infinity});
  for (const img of document.images) {
    const r = img.getBoundingClientRect();
    const url = img.currentSrc || img.src;
    if (!url || url.startsWith('data:') || img.naturalWidth < 200 || img.naturalHeight < 200 ||
        r.width < 80 || r.height < 80) continue;
    pictures.push({url, text: (img.alt || '').replace(/\\s+/g, ' ').trim().slice(0, 100),
                   area: r.width * r.height});
  }
  pictures.sort((a, b) => b.area - a.area);
  const images = [];
  for (const p of pictures) {
    if (!images.some(i => i.url === p.url)) images.push({url: p.url, text: p.text});
    if (images.length >= MAX_IMAGES) break;
  }
  return {title: document.title, url: location.href,
          text: (document.body ? document.body.innerText : '').slice(0, textChars), elements: out,
          images};
}""".replace("MAX_ELEMENTS", str(MAX_ELEMENTS)).replace("MAX_IMAGES", str(MAX_IMAGES))


_SIGNATURE = """() => {
  const text = document.body ? document.body.innerText : '';
  return [location.href, text.length, text.slice(0, 300),
          document.querySelectorAll('a, button, input, select, textarea').length];
}"""
"""What changes while a page is still building itself: compared twice, a short while apart."""

_FOCUSED = """() => {
  if (!document.hasFocus()) return null;
  const e = document.activeElement;
  if (!e || e.tagName !== 'INPUT') return null;
  const type = (e.type || 'text').toLowerCase();
  let username = '';
  if (type === 'password') {
    const scope = e.form || document;
    const named = [...scope.querySelectorAll('input')].find(i =>
      ['text', 'email', 'tel'].includes((i.type || 'text').toLowerCase()) && i.value);
    username = named ? named.value : '';
  }
  return {type, value: e.value, username, host: location.hostname, url: location.href};
}"""
"""The field he is typing into in a hand-over (D120): a password, and the username beside it."""
_PASSWORD_SHOWN = """() => [...document.querySelectorAll('input[type=password]')].some(i => {
  const r = i.getBoundingClientRect();
  return r.width > 0 && r.height > 0 && getComputedStyle(i).visibility !== 'hidden';
})"""

STEADY_SECONDS = 8.0
"""How long a page may take to finish building itself after an action (a sign-in, an app)."""
STEADY_STEP_MS = 250
QUIET_SECONDS = 0.5
"""A page is not steady while a request started or ended this recently (a sign-in being sent,
an app fetching what it will show)."""
STALE_REQUEST_SECONDS = 4.0
"""A request open longer than this is a page's long poll, not something it waits for."""
_LOADING = re.compile(
    r"veuillez patienter|patientez|chargement|loading|please wait|un instant|redirection",
    re.IGNORECASE,
)
"""What a page says while it is not ready (« Veuillez patienter », Hyperplanning, PRONOTE)."""
GONE = (
    "L'élément [{n}] n'existe plus : la page a changé depuis que tu l'as lue. Relis-la "
    "(browser read) et prends le nouveau numéro."
)


def _loading(texts: list[str]) -> bool:
    """Nothing to read yet, or only a « please wait »."""
    shown = " ".join(t.strip() for t in texts).strip()
    return not shown or (len(shown) < 300 and _LOADING.search(shown) is not None)


@dataclass
class Snapshot:
    url: str
    title: str
    text: str
    elements: list[str] = field(default_factory=list[str])
    images: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    """The page's large pictures (address, alt text), biggest first: what a product looks like."""
    steady: bool = True
    """False when the page was still changing (or still saying « veuillez patienter ») after
    `STEADY_SECONDS`: what is read may not last."""

    def render(self) -> str:
        return (
            f"URL: {self.url}\nTitre: {self.title}\n\nTexte de la page :\n{self.text}\n\n"
            "Éléments (utilise leur numéro) :\n" + "\n".join(self.elements)
        )


class BrowserError(RuntimeError):
    """The action could not be done (no such element, page unreachable…)."""


class Session:
    """One job's browser: opened, used, closed."""

    def __init__(
        self,
        read_only: bool = True,
        check: HostCheck | None = None,
        routes: dict[str, Callable[[Route], Awaitable[None]]] | None = None,
        executable: str | None = None,
    ) -> None:
        self.read_only = read_only
        self._check = check or check_public
        self._routes = routes or {}
        """Test sites served in-process: host → handler."""
        self._executable = executable
        self.send_hosts: tuple[str, ...] = ()
        """When sending is allowed: only to these sites (and their subdomains); empty: any."""
        self.storage_state: dict[str, Any] | None = None
        """A logged-in session (cookies), lent by the guardian for this one task."""
        self.phone: dict[str, int] | None = None
        """A phone's screen (width, height): the browser poses as a phone's, with touch. A
        session signed in from the owner's phone (D112) keeps it under `orion`, and is used
        the same way, as sites tie a session to the browser that made it."""
        self.profile: Path | None = None
        """A profile kept on disk (D116): Orion's own browser keeps its cookies, so a site it
        was signed in to stays signed in, from one conversation to the next and over restarts."""
        self.blocked: list[str] = []
        self._open_requests: dict[PwRequest, float] = {}
        self._last_traffic = 0.0
        self._numbered: list[tuple[int, int, Frame]] = []
        """The last snapshot's elements, by frame: (first, last, frame). A sign-in form is
        often in a frame of its own (a school's, a payment's)."""
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self._cleared: set[str] = set()

    async def __aenter__(self) -> Session:
        self._pw = await async_playwright().start()
        if self.profile is not None:
            phone = self.phone
            self.profile.mkdir(parents=True, exist_ok=True)
            self._context = await self._pw.chromium.launch_persistent_context(
                str(self.profile),
                headless=True,
                executable_path=self._executable,
                accept_downloads=False,
                locale="fr-FR",
                timezone_id="Europe/Paris",
                viewport={"width": phone["width"], "height": phone["height"]} if phone else None,
                device_scale_factor=2 if phone else None,
                is_mobile=bool(phone),
                has_touch=bool(phone),
                user_agent=self.phone_agent() if phone else None,
            )
            await self._context.route("**/*", self._guard)
            self._listen(self._context)
            kept = self.profile / KEPT_COOKIES
            if kept.is_file():  # session cookies: Chromium drops them, the sign-in is in them
                cookies = json.loads(kept.read_text(encoding="utf-8"))
                await self._context.add_cookies(cookies)
            self.page = (
                self._context.pages[0] if self._context.pages else await self._context.new_page()
            )
            self.page.set_default_timeout(15_000)
            return self
        self._browser = await self._pw.chromium.launch(
            headless=True, executable_path=self._executable
        )
        state = dict(self.storage_state) if self.storage_state is not None else None
        profile: dict[str, Any] = dict(state.pop("orion", None) or {}) if state else {}
        phone: dict[str, Any] | None = self.phone or profile.get("phone")
        self._context = await self._browser.new_context(
            accept_downloads=False,
            locale="fr-FR",
            timezone_id="Europe/Paris",
            storage_state=cast(Any, state),
            viewport=(
                {"width": int(phone["width"]), "height": int(phone["height"])} if phone else None
            ),
            device_scale_factor=2 if phone else None,
            is_mobile=bool(phone),
            has_touch=bool(phone),
            user_agent=(str(profile.get("user_agent") or self.phone_agent()) if phone else None),
        )
        await self._context.route("**/*", self._guard)
        self._listen(self._context)
        self.page = await self._context.new_page()
        self.page.set_default_timeout(15_000)
        return self

    def _listen(self, context: BrowserContext) -> None:
        """Keep count of the requests under way: a page waiting for one is not ready."""

        def started(request: PwRequest) -> None:
            self._open_requests[request] = self._last_traffic = time.monotonic()

        def ended(request: PwRequest) -> None:
            self._open_requests.pop(request, None)
            self._last_traffic = time.monotonic()

        context.on("request", started)
        context.on("requestfinished", ended)
        context.on("requestfailed", ended)

    def _busy(self) -> bool:
        """A request under way (not a long poll), or traffic a moment ago."""
        now = time.monotonic()
        if now - self._last_traffic < QUIET_SECONDS:
            return True
        return any(now - at < STALE_REQUEST_SECONDS for at in self._open_requests.values())

    def phone_agent(self) -> str:
        version = self._browser.version if self._browser is not None else "130.0.0.0"
        return (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{version} Mobile Safari/537.36"
        )

    async def keep_cookies(self) -> None:
        """Every cookie, session ones included, written beside the profile (0600): a site
        signed in to stays signed in after the browser closes or the server restarts."""
        if self.profile is None or self._context is None:
            return
        cookies = await self._context.cookies()
        path = self.profile / KEPT_COOKIES
        path.write_text(json.dumps(cookies), encoding="utf-8")
        path.chmod(0o600)

    async def new_page(self) -> Page:
        """Another tab of this browser (one per conversation), sharing its cookies."""
        if self._context is None:  # pragma: no cover - inside `with` only
            raise BrowserError("the browser is not open")
        page = await self._context.new_page()
        page.set_default_timeout(15_000)
        return page

    async def forget(self, site: str = "") -> None:
        """Signed out: the cookies of a site (and its subdomains), or of every site."""
        if self._context is None:  # pragma: no cover - inside `with` only
            raise BrowserError("the browser is not open")
        if not site:
            await self._context.clear_cookies()
            return
        for cookie in await self._context.cookies():
            domain = str(cookie.get("domain", "")).lstrip(".")
            if host_in(domain, [site]):
                await self._context.clear_cookies(
                    name=cookie.get("name"), domain=cookie.get("domain")
                )

    async def saved_state(self) -> dict[str, Any]:
        """The session as it is now (cookies, local storage), to keep and use again; with the
        phone it was made on, if any."""
        if self._context is None or self.page is None:  # pragma: no cover - inside `with` only
            raise BrowserError("the browser is not open")
        state = cast(dict[str, Any], await self._context.storage_state())
        if self.phone:
            agent = await self.page.evaluate("navigator.userAgent")
            state["orion"] = {"phone": self.phone, "user_agent": str(agent)}
        return state

    async def tap(self, x: float, y: float) -> None:
        page = self._page()
        await self._do(lambda: page.touchscreen.tap(x, y) if self.phone else page.mouse.click(x, y))

    async def keys(self, text: str = "", key: str = "") -> None:
        """Type into whatever has the focus, or press one key."""
        keyboard = self._page().keyboard
        await self._do(lambda: keyboard.press(key) if key else keyboard.type(text, delay=20))

    async def settle(self) -> None:
        """After a tap or a key that may start a navigation (a form sent): let it land."""
        page = self._page()
        await page.wait_for_timeout(400)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Error:
            log.info("computer.slow_page", url=page.url)

    async def focused_field(self) -> dict[str, str] | None:
        """The input that has the focus, in whichever frame: its type and value, and for a
        password the username filled beside it. For the owner's hand-over only (D120)."""
        page = self._page()
        for frame in [page.main_frame, *[f for f in page.frames if f is not page.main_frame]]:
            try:
                found = await frame.evaluate(_FOCUSED)
            except Error:
                continue
            if found:
                return {str(k): str(v) for k, v in cast(dict[str, Any], found).items()}
        return None

    async def shows_password_field(self) -> bool:
        """Whether a password field is still on show (a sign-in not gone through)."""
        page = self._page()
        for frame in page.frames:
            try:
                if await frame.evaluate(_PASSWORD_SHOWN):
                    return True
            except Error:
                continue
        return False

    async def wheel(self, dy: int) -> None:
        await self._do(lambda: self._page().mouse.wheel(0, dy))

    async def __aexit__(self, *_exc: object) -> None:
        if self._context is not None:
            await self.keep_cookies()
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()

    async def _guard(self, route: Route, request: PwRequest) -> None:
        parts = urlsplit(request.url)
        if parts.scheme in ("data", "blob", "about"):
            await route.continue_()
            return
        sending = request.method not in ("GET", "HEAD")
        if (
            sending
            and not self.read_only
            and self.send_hosts
            and not host_in(parts.hostname or "", list(self.send_hosts))
        ):
            self.blocked.append(f"{request.method} {request.url}")
            log.info("computer.blocked_off_site", method=request.method, url=request.url)
            await route.abort("blockedbyclient")
            return
        if self.read_only and sending:
            self.blocked.append(f"{request.method} {request.url}")
            log.info("computer.blocked_send", method=request.method, url=request.url)
            await route.abort("blockedbyclient")
            return
        host = parts.hostname or ""
        if parts.scheme not in ("http", "https") or not host:
            await route.abort("blockedbyclient")
            return
        if host not in self._cleared:
            try:
                await self._check(host, parts.port or (443 if parts.scheme == "https" else 80))
            except FetchError:
                self.blocked.append(f"{request.method} {request.url}")
                await route.abort("blockedbyclient")
                return
            self._cleared.add(host)
        handler = self._routes.get(host)
        if handler is not None:
            await handler(route)
            return
        await route.continue_()

    def _page(self) -> Page:
        if self.page is None:  # pragma: no cover - used only inside `async with`
            raise BrowserError("the browser is not open")
        return self.page

    async def screenshot(self, full: bool) -> bytes:
        """The page as the owner would see it: the visible part as PNG, or the whole page as
        JPEG (smaller, for a long page)."""
        if full:
            return await self._page().screenshot(full_page=True, type="jpeg", quality=80)
        return await self._page().screenshot(type="png")

    async def snapshot(self, text_chars: int = TEXT_CHARS) -> Snapshot:
        """The page as it is once it stops changing, its frames included: an app that builds
        itself after loading (Hyperplanning, PRONOTE) first shows « veuillez patienter », or
        nothing at all."""
        page = self._page()
        steady = await self._steady()
        for attempt in range(3):
            try:
                await page.wait_for_load_state("domcontentloaded")
                return await self._read(text_chars, steady)
            except Error as exc:
                # A page still navigating (a redirect, a blocked submit) replaces the document
                # under the script: read the new one.
                if "context was destroyed" not in exc.message or attempt == 2:
                    raise BrowserError(f"the page cannot be read: {exc.message}") from exc
                await page.wait_for_timeout(300)
        raise BrowserError("the page cannot be read")  # pragma: no cover - returns or raises

    async def _read(self, text_chars: int, steady: bool) -> Snapshot:
        page = self._page()
        data = cast(
            dict[str, Any], await page.evaluate(_SNAPSHOT, {"textChars": text_chars, "start": 0})
        )
        elements = [str(e) for e in data["elements"]]
        text = " ".join(str(data["text"]).split(" "))
        numbered = [(1, len(elements), page.main_frame)]
        for frame in await self._frames():
            try:
                inner = cast(
                    dict[str, Any],
                    await frame.evaluate(
                        _SNAPSHOT, {"textChars": text_chars, "start": len(elements)}
                    ),
                )
            except Error as exc:  # a frame gone meanwhile: the rest of the page still reads
                log.info("computer.frame_unreadable", url=frame.url, error=exc.message[:120])
                continue
            found = [str(e) for e in inner["elements"]]
            if not found and not str(inner["text"]).strip():
                continue
            where = urlsplit(frame.url).hostname or frame.url
            numbered.append((len(elements) + 1, len(elements) + len(found), frame))
            text += f"\n\n[Dans un cadre de {where}]\n" + " ".join(str(inner["text"]).split(" "))
            elements += [f"— dans le cadre de {where} —", *found] if found else []
        self._numbered = numbered
        return Snapshot(
            str(data["url"]),
            str(data["title"]),
            text[:text_chars],
            elements,
            [(str(i["url"]), str(i["text"])) for i in data.get("images", [])],
            steady,
        )

    async def _frames(self) -> list[Frame]:
        """The page's frames that show something (not hidden, not blank)."""
        page = self._page()
        shown: list[Frame] = []
        for frame in page.frames:
            if frame is page.main_frame or frame.url in ("", "about:blank"):
                continue
            try:
                box = await (await frame.frame_element()).bounding_box()
            except Error:
                continue
            if box is not None and box["width"] > 1 and box["height"] > 1:
                shown.append(frame)
        return shown

    async def _steady(self) -> bool:
        """Wait until the page stops changing and has more to say than « veuillez patienter »;
        False when it still had not after `STEADY_SECONDS`."""
        page = self._page()
        deadline = time.monotonic() + STEADY_SECONDS
        last: list[object] | None = None
        while True:
            now: list[object] | None
            try:
                now = [await page.evaluate(_SIGNATURE)]
                for frame in await self._frames():
                    now.append(await frame.evaluate(_SIGNATURE))
            except Error:  # navigating: the document is being replaced
                now = None
            if now is not None and now == last and not self._busy():
                texts = [str(cast(list[object], sign)[2]) for sign in now]
                if not _loading(texts):
                    return True
            if time.monotonic() >= deadline:
                log.info("computer.page_not_steady", url=page.url)
                return False
            last = now
            await page.wait_for_timeout(STEADY_STEP_MS)

    async def _do(self, action: Callable[[], Awaitable[object]]) -> None:
        try:
            await action()
        except Error as exc:
            raise BrowserError(exc.message.splitlines()[0]) from exc
        page = self._page()
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Error:
            log.info("computer.slow_page", url=page.url)

    def _frame_of(self, number: int) -> Frame:
        return next(
            (f for first, last, f in self._numbered if first <= number <= last),
            self._page().main_frame,
        )

    def _element(self, number: int) -> Locator:
        return self._frame_of(number).locator(f'[data-orion-id="{number}"]').first

    async def _present(self, number: int) -> Locator:
        """The element numbered on the last page read, if it is still there: an app redraws
        itself, and the numbers go with the old drawing."""
        element = self._element(number)
        try:
            count = await element.count()
        except Error as exc:
            raise BrowserError(exc.message.splitlines()[0]) from exc
        if count == 0:
            raise BrowserError(GONE.format(n=number))
        return element

    async def goto(self, url: str) -> None:
        if urlsplit(url).scheme not in ("http", "https"):
            raise BrowserError(f"not a web address: {url}")
        await self._do(lambda: self._page().goto(url))

    async def click(self, number: int) -> None:
        element = await self._present(number)
        await self._do(lambda: element.click(timeout=8_000))

    async def type(self, number: int, text: str, enter: bool) -> None:
        element = await self._present(number)

        async def typing() -> None:
            await element.fill(text, timeout=8_000)
            if enter:
                await element.press("Enter")

        await self._do(typing)

    async def fill_secret(self, number: int, value: str, sites: list[str], kind: str) -> None:
        """Type a lent login's username, password or 2FA code (`kind`) into a field, only on
        a page of the login's sites, and the password only into a password field (D111)."""
        host = urlsplit(self._frame_of(number).url).hostname or ""  # the field's own frame
        if not host_in(host, sites):
            raise BrowserError(f"this page is not on {', '.join(sites)}: nothing typed")
        element = await self._present(number)
        try:
            tag, field_type = cast(
                list[str],
                await element.evaluate(
                    "e => [e.tagName.toLowerCase(), (e.getAttribute('type') || '').toLowerCase()]",
                    timeout=8_000,
                ),
            )
        except Error as exc:
            raise BrowserError(exc.message.splitlines()[0]) from exc
        if tag != "input":
            raise BrowserError("not an input field")
        if (kind == "password") != (field_type == "password") and kind != "code":
            raise BrowserError(
                "the password goes into a password field only, the username never into one"
            )
        if field_type not in ("", "text", "email", "tel", "number", "password"):
            raise BrowserError(f"not a field to type into ({field_type})")
        await self._do(lambda: element.fill(value, timeout=8_000))

    async def select(self, number: int, value: str) -> None:
        element = await self._present(number)
        await self._do(lambda: element.select_option(label=value, timeout=8_000))

    async def scroll(self, down: bool) -> None:
        await self._do(lambda: self._page().mouse.wheel(0, 900 if down else -900))

    async def back(self) -> None:
        await self._do(lambda: self._page().go_back())
