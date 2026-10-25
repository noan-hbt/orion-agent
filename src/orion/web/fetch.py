"""Fetching a page safely: public addresses only, bounded size, text extracted by code.

Any public address (D115). Private, loopback and link-local addresses are refused, on every
redirect, so a page cannot point Orion at the guardian or the local network. The address is
resolved for the check, then again by the HTTP client: a host that changes its answer between
the two (DNS rebinding) is not caught here.
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
DEADLINE = 60.0
"""Seconds a whole fetch may take, redirects and body included."""
_SKIP = {"script", "style", "noscript", "svg", "template", "head"}
_IMAGE_META = {"og:image", "og:image:url", "og:image:secure_url", "twitter:image"}
_NOT_A_PHOTO = ("logo", "icon", "sprite", "pixel", "placeholder", "avatar", "badge", "flag")
MAX_IMAGES = 12
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
    images: list[Link] = field(default_factory=list[Link])
    """The page's own pictures (its preview image first, then its photos), with their alt text:
    addresses written in the page, not made up, so they may be downloaded as they are."""


def normalize(url: str) -> str:
    """The form URLs are compared in: no fragment, scheme and host lowercase (a login or a
    path keeps its case). '' for what is not a web address, malformed ones included."""
    try:
        bare, _ = urldefrag(url.strip())
        parts = urlsplit(bare)
        host, port = parts.hostname, parts.port
    except ValueError:  # `http://[::1`, a port that is not a number…
        return ""
    if parts.scheme.lower() not in ("http", "https") or not host:
        return ""
    login = parts.netloc.rpartition("@")[0]
    shown = f"[{host}]" if ":" in host else host
    netloc = (f"{login}@" if login else "") + shown + (f":{port}" if port is not None else "")
    return parts._replace(scheme=parts.scheme.lower(), netloc=netloc).geturl()


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
        self.previews: list[Link] = []
        self.photos: list[Link] = []
        self._skipping = 0
        self._in_title = False
        self._href: str | None = None
        self._anchor: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta":
            self._meta(dict(attrs))
        if tag == "img":
            self._img(dict(attrs))
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

    def _meta(self, attrs: dict[str, str | None]) -> None:
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        content = attrs.get("content")
        if key in _IMAGE_META and content:
            self.previews.append(Link(normalize(urljoin(self.base, content)), "aperçu de la page"))

    def _img(self, attrs: dict[str, str | None]) -> None:
        # Lazy pages put a placeholder in `src` and the photo in `data-src` or `srcset`.
        sources = [attrs.get(k) or "" for k in ("data-src", "data-lazy-src", "src")]
        source = next((x for x in sources if x and not x.startswith("data:")), "")
        srcset = attrs.get("srcset") or attrs.get("data-srcset") or ""
        if srcset and not source:
            source = srcset.split(",")[-1].split()[0]  # the largest
        alt = " ".join((attrs.get("alt") or "").split())[:100]
        if not source:
            return
        url = normalize(urljoin(self.base, source))
        if url.lower().split("?")[0].endswith((".svg", ".gif", ".ico")):
            return
        if any(word in f"{url} {alt}".lower() for word in _NOT_A_PHOTO):
            return
        self.photos.append(Link(url, alt))

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
    images: list[Link] = []
    for image in [*parser.previews, *parser.photos]:
        if image.url and image.url not in {i.url for i in images}:
            images.append(image)
    return Page(
        url,
        " ".join(parser.title.split()),
        "\n".join(x for x in lines if x),
        links,
        images[:MAX_IMAGES],
    )


Check = Callable[[str, int], Awaitable[None]]


@dataclass(frozen=True)
class Download:
    url: str
    content_type: str
    data: bytes


async def _get(
    client: httpx.AsyncClient,
    url: str,
    check: Check | None,
    limit: int,
    accept: Callable[[str], bool],
    whole: bool,
) -> tuple[str, str, bytes, str | None]:
    """GET a public address, following redirects by hand (each target is checked). Returns the
    final address, the content type, the body (at most `limit` bytes) and its encoding. With
    `whole`, a body over `limit` is refused rather than cut."""
    try:
        async with asyncio.timeout(DEADLINE):  # a page that drips forever is given up
            return await _get_within(client, url, check, limit, accept, whole)
    except TimeoutError as exc:
        raise FetchError(f"{url} took more than {DEADLINE:.0f} s") from exc


async def _get_within(
    client: httpx.AsyncClient,
    url: str,
    check: Check | None,
    limit: int,
    accept: Callable[[str], bool],
    whole: bool,
) -> tuple[str, str, bytes, str | None]:
    check = check or check_public
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            parts = urlsplit(current)
            host, port = parts.hostname, parts.port
        except ValueError as exc:
            raise FetchError(f"not a web address: {current}") from exc
        if parts.scheme not in ("http", "https") or not host:
            raise FetchError(f"not a web address: {current}")
        await check(host, port or (443 if parts.scheme == "https" else 80))
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
                if not accept(kind):
                    raise FetchError(f"{current} is not text ({kind or 'unknown type'})")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > limit:
                        if whole:
                            raise FetchError(f"{current} is larger than {limit // 1_000_000} MB")
                        break
                return current, kind, bytes(body[:limit]), response.encoding
        except httpx.HTTPError as exc:
            raise FetchError(f"{current} unreachable: {exc!r}") from exc
    raise FetchError(f"too many redirects from {url}")


async def fetch(client: httpx.AsyncClient, url: str, check: Check | None = None) -> Page:
    """A public page, as text."""
    final, kind, body, encoding = await _get(
        client,
        url,
        check,
        MAX_BYTES,
        lambda kind: any(t in kind for t in ("html", "text", "xml", "json")),
        whole=False,
    )
    text = body.decode(encoding or "utf-8", errors="replace")
    if "html" in kind:
        return extract(final, text)
    return Page(final, "", text)


async def download(
    client: httpx.AsyncClient, url: str, limit: int, check: Check | None = None
) -> Download:
    """A public file, byte for byte, whatever its type; refused over `limit`."""
    final, kind, body, _ = await _get(client, url, check, limit, lambda _kind: True, whole=True)
    return Download(final, kind.split(";")[0].strip().lower(), body)
