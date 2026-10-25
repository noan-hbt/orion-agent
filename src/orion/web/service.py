"""The web desk: what Orion may read, and the record of what it read.

    web.searched  (query, the pages found)
    web.read      (address, title, the page's links and pictures; its text is not journaled)
    web.downloaded (address, type, size: a file kept as it is, its content never journaled)

All carry `Trust.WEB`. Orion reads pages itself (D114). Since D115 it may open any public
address (the owner asked for it, knowing a page could steer Orion into sending data out in an
address it makes up); the addresses seen are still projected (`WebState.allowed`), for the
record.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import Field

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.modules.base import ModuleSettings
from orion.web.fetch import Check, Download, FetchError, download, fetch, normalize
from orion.web.search import Findings, Reading, WebSearch, reading

SEARCHED = "web.searched"
READ = "web.read"
DOWNLOADED = "web.downloaded"
OWNER_MESSAGE = "message.received"
BROWSED = "browser.page"
"""A page Orion's browser showed (`orion.computer`): its pictures may be downloaded too."""
KEPT_PAGES = 20
_URL = re.compile(r"https?://[^\s<>\"'«»]+", re.IGNORECASE)


class WebSettings(ModuleSettings):
    model: str | None = Field(
        default=None, description="Unused since D114: searching runs on Orion's own model"
    )
    max_results: int = Field(default=6, ge=1, le=10)


class NotAllowedError(ValueError):
    """An address neither the owner nor a search gave."""


@dataclass
class WebState:
    name: str = "web"
    allowed: set[str] = field(default_factory=set[str])
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.allowed.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind == SEARCHED:
            self.allowed.update(str(r["url"]) for r in payload.get("results", []))
        elif event.kind == READ:
            self.allowed.add(str(payload["url"]))
            self.allowed.update(str(link["url"]) for link in payload.get("links", []))
            self.allowed.update(str(image["url"]) for image in payload.get("images", []))
        elif event.kind == BROWSED:
            self.allowed.update(str(url) for url in payload.get("images", []))
        elif event.kind == OWNER_MESSAGE and event.trust >= Trust.OWNER:
            for found in _URL.findall(str(payload.get("text", ""))):
                url = normalize(found.rstrip(".,;:!?)]"))
                if url:
                    self.allowed.add(url)


class WebDesk:
    def __init__(
        self,
        journal: EventStore,
        searcher: WebSearch,
        http: httpx.AsyncClient | None = None,
        check: Check | None = None,
    ) -> None:
        self._journal = journal
        self._searcher = searcher
        self._pages: dict[str, Reading] = {}
        """The last pages read, by address: their next parts are read without fetching again."""
        self._http = http or httpx.AsyncClient(timeout=20.0)
        self._check = check
        self.state = WebState()
        self._runner = ProjectionRunner(journal, [self.state])

    async def start(self) -> None:
        await self._runner.catch_up()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def search(self, query: str, correlation_id: str | None = None) -> Findings:
        findings = await self._searcher.search(query)
        await self._record(
            SEARCHED,
            {
                "query": query[:300],
                "results": [{"url": r.url, "title": r.title} for r in findings.results],
            },
            "web:search",
            correlation_id,
        )
        return findings

    async def may_open(self, url: str) -> bool:
        """Any web address (D115): the server's own and the local network stay out (`fetch`)."""
        return bool(normalize(url))

    async def read(self, url: str, correlation_id: str | None = None) -> Reading:
        """The page itself, for Orion to read; fetched again only when not read lately."""
        address = normalize(url)
        if not await self.may_open(address):
            raise NotAllowedError(f"{url} is not a web address (http or https)")
        kept = self._pages.get(address)
        if kept is not None:
            return kept
        page = reading(await fetch(self._http, address, self._check))
        await self._record(
            READ,
            {
                "url": address,
                "final_url": page.url,
                "title": page.title,
                "links": [{"url": link.url, "text": link.text} for link in page.links],
                "images": [{"url": i.url, "text": i.text} for i in page.images],
            },
            f"web:{address}",
            correlation_id,
        )
        self._pages[address] = page
        while len(self._pages) > KEPT_PAGES:
            self._pages.pop(next(iter(self._pages)))
        return page

    async def download(self, url: str, limit: int, correlation_id: str | None = None) -> Download:
        """A file at an address the owner gave or a search found, byte for byte."""
        address = normalize(url)
        if not await self.may_open(address):
            raise NotAllowedError(f"{url} is not a web address (http or https)")
        got = await download(self._http, address, limit, self._check)
        await self._record(
            DOWNLOADED,
            {
                "url": address,
                "final_url": got.url,
                "content_type": got.content_type,
                "size": len(got.data),
            },
            f"web:{address}",
            correlation_id,
        )
        return got

    async def _record(
        self, kind: str, payload: dict[str, Any], source: str, correlation_id: str | None
    ) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=Provenance(source=source, actor="orion:web"),
                    trust=Trust.WEB,
                    correlation_id=correlation_id,
                )
            ]
        )
        await self._runner.catch_up()


__all__ = ["FetchError", "NotAllowedError", "WebDesk", "WebSettings"]
