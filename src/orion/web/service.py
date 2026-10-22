"""The web desk: what Orion may read, and the record of what it read.

    web.searched  (query, the clerk's answer, the pages found)
    web.read      (address, question, the clerk's answer, links worth following)
    web.downloaded (address, type, size: a file kept as it is, its content never journaled)

Both carry `Trust.WEB`. The addresses Orion may open are a projection: those the owner wrote
in a message, those a search returned, the links the clerk picked on a page it read, and that
page's own pictures (to send the owner a product's photo, not a screenshot). So
Orion can follow a trail, but never open an address it made up (one could carry the owner's
data out in its query string).
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
from orion.web.clerk import Findings, Reading, WebClerk
from orion.web.fetch import Check, Download, FetchError, download, fetch, normalize

SEARCHED = "web.searched"
READ = "web.read"
DOWNLOADED = "web.downloaded"
OWNER_MESSAGE = "message.received"
BROWSED = "browser.page"
"""A page Orion's browser showed (`orion.computer`): its pictures may be downloaded too."""
_URL = re.compile(r"https?://[^\s<>\"'«»]+", re.IGNORECASE)


class WebSettings(ModuleSettings):
    model: str | None = Field(
        default=None, description="The clerk's model (reads the web); defaults to extractor_model"
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
        clerk: WebClerk,
        http: httpx.AsyncClient | None = None,
        check: Check | None = None,
    ) -> None:
        self._journal = journal
        self._clerk = clerk
        self._http = http or httpx.AsyncClient(timeout=20.0)
        self._check = check
        self.state = WebState()
        self._runner = ProjectionRunner(journal, [self.state])

    async def start(self) -> None:
        await self._runner.catch_up()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def search(self, query: str, correlation_id: str | None = None) -> Findings:
        findings = await self._clerk.search(query)
        await self._record(
            SEARCHED,
            {
                "query": query[:300],
                "answer": findings.answer,
                "suspicious": findings.suspicious,
                "results": [
                    {"url": r.url, "title": r.title, "summary": r.summary} for r in findings.results
                ],
            },
            "web:search",
            correlation_id,
        )
        return findings

    async def may_open(self, url: str) -> bool:
        await self._runner.catch_up()
        return normalize(url) in self.state.allowed

    async def read(self, url: str, question: str, correlation_id: str | None = None) -> Reading:
        address = normalize(url)
        if not await self.may_open(address):
            raise NotAllowedError(
                f"{url} was neither given by the owner nor found by a search or on a page "
                "already read: search first"
            )
        page = await fetch(self._http, address, self._check)
        reading = await self._clerk.read(page, question)
        await self._record(
            READ,
            {
                "url": address,
                "final_url": page.url,
                "question": question[:400],
                "title": reading.title,
                "answer": reading.answer,
                "suspicious": reading.suspicious,
                "links": [{"url": link.url, "text": link.text} for link in reading.links],
                "images": [{"url": i.url, "text": i.text} for i in reading.images],
            },
            f"web:{address}",
            correlation_id,
        )
        return reading

    async def download(self, url: str, limit: int, correlation_id: str | None = None) -> Download:
        """A file at an address the owner gave or a search found, byte for byte."""
        address = normalize(url)
        if not await self.may_open(address):
            raise NotAllowedError(
                f"{url} was neither given by the owner nor found by a search or on a page "
                "already read: search first"
            )
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
                    provenance=Provenance(source=source, actor="orion:web-clerk"),
                    trust=Trust.WEB,
                    correlation_id=correlation_id,
                )
            ]
        )
        await self._runner.catch_up()


__all__ = ["FetchError", "NotAllowedError", "WebDesk", "WebSettings"]
