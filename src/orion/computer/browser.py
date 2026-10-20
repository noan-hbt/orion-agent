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

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlsplit

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    Error,
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
HostCheck = Callable[[str, int], Awaitable[None]]

_SNAPSHOT = """(textChars) => {
  document.querySelectorAll('[data-orion-id]').forEach(e => e.removeAttribute('data-orion-id'));
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, ' +
              '[role=button], [role=link], [role=checkbox], [role=tab], [onclick]';
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    n += 1;
    el.setAttribute('data-orion-id', String(n));
    const tag = el.tagName.toLowerCase();
    const kind = el.type && tag !== 'a' ? `(${el.type})` : '';
    const label = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') ||
                   el.title || el.name || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
    const href = tag === 'a' && el.href ? ` -> ${el.href}` : '';
    out.push(`[${n}] ${tag}${kind} ${label}${href}`);
    if (n >= MAX_ELEMENTS) break;
  }
  return {title: document.title, url: location.href,
          text: (document.body ? document.body.innerText : '').slice(0, textChars), elements: out};
}""".replace("MAX_ELEMENTS", str(MAX_ELEMENTS))


@dataclass
class Snapshot:
    url: str
    title: str
    text: str
    elements: list[str] = field(default_factory=list[str])

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
        self.blocked: list[str] = []
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self._cleared: set[str] = set()

    async def __aenter__(self) -> Session:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True, executable_path=self._executable
        )
        self._context = await self._browser.new_context(
            accept_downloads=False,
            locale="fr-FR",
            timezone_id="Europe/Paris",
            storage_state=cast(Any, self.storage_state),
        )
        await self._context.route("**/*", self._guard)
        self.page = await self._context.new_page()
        self.page.set_default_timeout(15_000)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._context is not None:
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
        page = self._page()
        for attempt in range(3):
            try:
                await page.wait_for_load_state("domcontentloaded")
                data = cast(dict[str, Any], await page.evaluate(_SNAPSHOT, text_chars))
                break
            except Error as exc:
                # A page still navigating (a redirect, a blocked submit) replaces the document
                # under the script: read the new one.
                if "context was destroyed" not in exc.message or attempt == 2:
                    raise BrowserError(f"the page cannot be read: {exc.message}") from exc
                await page.wait_for_timeout(300)
        else:  # pragma: no cover - the loop either breaks or raises
            raise BrowserError("the page cannot be read")
        return Snapshot(
            str(data["url"]),
            str(data["title"]),
            " ".join(str(data["text"]).split(" ")),
            [str(e) for e in data["elements"]],
        )

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

    def _element(self, number: int) -> Any:
        return self._page().locator(f'[data-orion-id="{number}"]').first

    async def goto(self, url: str) -> None:
        if urlsplit(url).scheme not in ("http", "https"):
            raise BrowserError(f"not a web address: {url}")
        await self._do(lambda: self._page().goto(url))

    async def click(self, number: int) -> None:
        await self._do(lambda: self._element(number).click(timeout=8_000))

    async def type(self, number: int, text: str, enter: bool) -> None:
        async def typing() -> None:
            element = self._element(number)
            await element.fill(text, timeout=8_000)
            if enter:
                await element.press("Enter")

        await self._do(typing)

    async def select(self, number: int, value: str) -> None:
        await self._do(lambda: self._element(number).select_option(label=value, timeout=8_000))

    async def scroll(self, down: bool) -> None:
        await self._do(lambda: self._page().mouse.wheel(0, 900 if down else -900))

    async def back(self) -> None:
        await self._do(lambda: self._page().go_back())
