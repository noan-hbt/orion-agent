"""His reading: `[modules.feeds]` (on by default; nothing to do until he follows a feed).

The blogs, newsletters and sites he follows publish RSS or Atom feeds. Orion reads them every
few hours and journals what is new (`feed.item`, web content: `Trust.WEB`). He adds one by
message (« suis le blog de Paul Graham ») or in orion.toml. Then:

- `feeds` (list) tells what is new in his reading;
- the weekly curiosity (D76) weighs the week's new items beside what its searches found: its
  judge, who never sees his memory, may keep one for him. The lead itself is still chosen from
  his memory alone, so nothing a feed says can steer what Orion looks for.

Only public addresses are fetched, as for the web (`orion.web.fetch`).

    feed.added  feed.dropped  feed.item  feed.synced
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import Event, NewEvent, Provenance, Trust, enclosed
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.feeds.parse import FeedFormatError, Item, parse
from orion.modules.base import Daemon, Module, ModuleSettings, Plate
from orion.tools.actions import grouped
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult
from orion.web.fetch import FetchError, download, normalize

if TYPE_CHECKING:
    from orion.config import OrionConfig

log = structlog.get_logger(__name__)

ADDED = "feed.added"
DROPPED = "feed.dropped"
ITEM = "feed.item"
SYNCED = "feed.synced"
MAX_FEED_BYTES = 2_000_000
KEPT_PER_FEED = 60
NEW_FOR = timedelta(days=7)
"""What counts as new in his reading."""
FIRST_SYNC_ITEMS = 5
"""Of a feed just added, only the latest are journaled: its archive is not news."""

Fetch = Callable[[str], Awaitable[bytes]]


class FeedSettings(ModuleSettings):
    enabled: bool = True
    urls: list[str] = Field(
        default_factory=list[str], description="Feeds he follows (RSS or Atom addresses)"
    )
    refresh_hours: float = Field(default=3.0, ge=0.5, le=48.0)


@dataclass(frozen=True)
class Seen:
    feed: str
    item: Item
    at: datetime
    """When Orion first saw it."""


@dataclass
class FeedState:
    name: str = "feeds"
    followed: dict[str, str] = field(default_factory=dict[str, str])
    """url → title (added by message; orion.toml's are followed too)"""
    titles: dict[str, str] = field(default_factory=dict[str, str])
    items: dict[str, list[Seen]] = field(default_factory=dict[str, list[Seen]])
    """feed url → items seen, oldest first"""
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.followed.clear()
        self.titles.clear()
        self.items.clear()
        self._seq = 0

    def known(self, feed: str) -> set[str]:
        return {s.item.id for s in self.items.get(feed, [])}

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        url = str(payload.get("url") or payload.get("feed") or "")
        if event.kind == ADDED:
            self.followed[url] = str(payload.get("title", ""))
        elif event.kind == DROPPED:
            self.followed.pop(url, None)
        elif event.kind == SYNCED and payload.get("title"):
            self.titles[url] = str(payload["title"])
        elif event.kind == ITEM:
            published = payload.get("published")
            seen = Seen(
                str(payload["feed"]),
                Item(
                    str(payload["id"]),
                    str(payload.get("title", "")),
                    str(payload.get("link", "")),
                    datetime.fromisoformat(str(published)) if published else None,
                    str(payload.get("summary", "")),
                ),
                datetime.fromisoformat(str(payload["at"])),
            )
            kept = self.items.setdefault(seen.feed, [])
            kept.append(seen)
            del kept[:-KEPT_PER_FEED]


class Feeds:
    def __init__(
        self,
        journal: EventStore,
        settings: FeedSettings,
        timezone: str,
        fetch: Fetch | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._journal = journal
        self.settings = settings
        self.zone = ZoneInfo(timezone)
        self.clock = clock or (lambda: datetime.now(self.zone))
        self._fetch = fetch or self._http_fetch
        self._http: httpx.AsyncClient | None = None
        self.state = FeedState()
        self._runner = ProjectionRunner(journal, [self.state])

    async def sync(self) -> None:
        await self._runner.catch_up()

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    async def _http_fetch(self, url: str) -> bytes:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=20.0)
        return (await download(self._http, url, MAX_FEED_BYTES)).data

    def urls(self) -> list[str]:
        configured = [normalize(u) for u in self.settings.urls]
        return list(dict.fromkeys([*configured, *self.state.followed]))

    def title(self, url: str) -> str:
        return self.state.titles.get(url) or self.state.followed.get(url) or url

    async def _record(self, events: list[NewEvent]) -> None:
        await self._journal.append(events)
        await self.sync()

    async def follow(self, url: str, by: Provenance) -> str:
        """Check it is a feed, then follow it. Returns its title. Raises ValueError."""
        address = normalize(url)
        if not address:
            raise ValueError("not a web address")
        try:
            title, _ = parse(await self._fetch(address))
        except (FetchError, FeedFormatError) as exc:
            raise ValueError(str(exc)) from exc
        await self._record(
            [
                NewEvent(
                    kind=ADDED,
                    payload={"url": address, "title": title},
                    provenance=by,
                    trust=Trust.OWNER,
                )
            ]
        )
        await self.refresh(address)
        return title or address

    async def drop(self, url: str, by: Provenance) -> bool:
        await self.sync()
        address = normalize(url) or url
        if address not in self.state.followed:
            return False
        await self._record(
            [NewEvent(kind=DROPPED, payload={"url": address}, provenance=by, trust=Trust.OWNER)]
        )
        return True

    async def refresh(self, url: str) -> int:
        """Journal what is new in one feed; the number of new items (-1: unreadable)."""
        await self.sync()
        now = self.clock()
        try:
            title, items = parse(await self._fetch(url))
        except (FetchError, FeedFormatError) as exc:
            log.warning("feeds.unreadable", url=url, error=str(exc))
            return -1
        known = self.state.known(url)
        fresh = [i for i in items if i.id not in known]
        if not known:
            fresh = fresh[:FIRST_SYNC_ITEMS]
        provenance = Provenance(source=f"web:{url}", actor=f"feed:{url}")
        events = [
            NewEvent(
                kind=ITEM,
                payload={
                    "feed": url,
                    "id": item.id,
                    "title": item.title,
                    "link": item.url,
                    "published": item.published.isoformat() if item.published else None,
                    "summary": item.summary,
                    "at": now.isoformat(),
                },
                provenance=provenance,
                trust=Trust.WEB,
            )
            for item in reversed(fresh)  # oldest first
        ]
        events.append(
            NewEvent(
                kind=SYNCED,
                payload={"url": url, "title": title, "at": now.isoformat(), "new": len(fresh)},
                provenance=provenance,
                trust=Trust.WEB,
            )
        )
        await self._record(events)
        return len(fresh)

    async def run(self) -> None:
        while True:
            for url in self.urls():
                try:
                    await self.refresh(url)
                except Exception:
                    log.exception("feeds.refresh_failed", url=url)
            await asyncio.sleep(self.settings.refresh_hours * 3600)

    def recent(self, now: datetime, within: timedelta = NEW_FOR) -> list[Seen]:
        """What came in his feeds lately, newest first."""
        found = [s for url in self.urls() for s in self.state.items.get(url, [])
                 if now - s.at <= within]  # fmt: skip
        return sorted(found, key=lambda s: s.at, reverse=True)

    async def candidates(self, now: datetime) -> list[tuple[str, str, str]]:
        """(url, title, summary) of the week's items, for curiosity's judge."""
        await self.sync()
        return [
            (s.item.url, f"{s.item.title} ({self.title(s.feed)})", s.item.summary)
            for s in self.recent(now)
            if s.item.url
        ]


class AddInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(description="The feed's address (or the site's: try its /feed or /rss)")


class DropInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str


class ListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: int = Field(default=7, ge=1, le=30)


def feed_tools(feeds: Feeds) -> list[Tool]:
    def provenance(ctx: ToolContext) -> Provenance:
        return Provenance(
            source=f"thread:{ctx.thread_id}",
            actor="orion:core",
            derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
        )

    async def add(args: Any, ctx: ToolContext) -> ToolResult:
        try:
            title = await feeds.follow(args.url, provenance(ctx))
        except ValueError as exc:
            return ToolResult(f"Pas un flux lisible : {exc}", is_error=True)
        return ToolResult(f"Suivi : {title}.")

    async def drop(args: Any, ctx: ToolContext) -> ToolResult:
        if await feeds.drop(args.url, provenance(ctx)):
            return ToolResult("Plus suivi.")
        return ToolResult("Ce flux n'était pas suivi par message (orion.toml ?).", is_error=True)

    async def listing(args: Any, _ctx: ToolContext) -> ToolResult:
        await feeds.sync()
        now = feeds.clock()
        if not feeds.urls():
            return ToolResult("Il ne suit aucun flux.")
        seen = feeds.recent(now, timedelta(days=args.days))
        lines = [
            f"- {s.item.title} ({feeds.title(s.feed)}) {s.item.url}"
            + (f" : {s.item.summary[:160]}" if s.item.summary else "")
            for s in seen[:30]
        ]
        followed = ", ".join(feeds.title(u) for u in feeds.urls())
        body = "\n".join(lines) or "(rien de neuf)"
        return ToolResult(
            f"Ses flux : {followed}\n<lectures>\n{enclosed(body)}\n</lectures>", trust=Trust.WEB
        )

    tools = [
        Tool(
            name="feed_add",
            description="Follow a feed (a blog, a newsletter, a site).",
            input_model=AddInput,
            handler=add,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
        Tool(
            name="feed_drop",
            description="Stop following a feed.",
            input_model=DropInput,
            handler=drop,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
        Tool(
            name="feed_list",
            description="What came in his feeds lately.",
            input_model=ListInput,
            handler=listing,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        ),
    ]
    return [
        grouped(
            "feeds",
            "His reading: the feeds (RSS, Atom) of the blogs and sites he follows.",
            tools,
            {"feed_add": "add", "feed_drop": "drop", "feed_list": "list"},
        )
    ]


class FeedsModule(Module):
    name = "feeds"
    settings_model = FeedSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.feeds: Feeds | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        from orion.curiosity.module import CuriosityModule

        self.feeds = Feeds(plate.journal, self.settings, self.config.agent.timezone)
        await self.feeds.sync()
        curiosity = plate.module(CuriosityModule)
        if curiosity is not None and curiosity.curiosity is not None:
            curiosity.curiosity.reading = self.feeds.candidates
        return feed_tools(self.feeds)

    async def serve(self, daemon: Daemon) -> None:
        if self.feeds is not None:
            daemon.add_service("feeds", self.feeds.run)

    async def close(self) -> None:
        if self.feeds is not None:
            await self.feeds.aclose()

    def doctor(self) -> list[str]:
        settings: FeedSettings = self.settings
        return [f"{len(settings.urls)} feed(s) in orion.toml, more added by message"]
