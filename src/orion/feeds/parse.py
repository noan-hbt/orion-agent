"""Reading an RSS 2.0, RSS 1.0 or Atom feed: its items' id, title, link, date and summary.

Feeds are third-party content. A document that declares entities is refused (no expansion,
no external entity, fail closed); summaries lose their HTML and are clipped.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

SUMMARY_CHARS = 400
MAX_ITEMS = 50
_TAGS = re.compile(r"<[^>]+>")


class FeedFormatError(ValueError):
    """Not a feed Orion can read."""


@dataclass(frozen=True)
class Item:
    id: str
    title: str
    url: str
    published: datetime | None
    summary: str


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child(element: ElementTree.Element, *names: str) -> ElementTree.Element | None:
    return next((c for c in element if _local(c.tag) in names), None)


def _text(element: ElementTree.Element | None) -> str:
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())


def _plain(text: str) -> str:
    text = " ".join(html.unescape(_TAGS.sub(" ", text)).split())
    return text if len(text) <= SUMMARY_CHARS else text[: SUMMARY_CHARS - 1] + "…"


def _date(text: str) -> datetime | None:
    text = text.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None


def _link(item: ElementTree.Element) -> str:
    for child in item:
        if _local(child.tag) != "link":
            continue
        href = child.get("href")
        if href is None:  # RSS: the address is the text
            return _text(child)
        if child.get("rel", "alternate") == "alternate":
            return href.strip()
    return ""


def parse(data: bytes) -> tuple[str, list[Item]]:
    """The feed's title and its items, newest first as given (at most `MAX_ITEMS`)."""
    if b"<!ENTITY" in data[:4096].upper() or b"<!DOCTYPE" in data[:4096].upper():
        raise FeedFormatError("a feed declaring a DTD or entities is not read")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise FeedFormatError(f"not XML: {exc}") from exc
    kind = _local(root.tag)
    if kind == "rss":
        channel = _child(root, "channel")
        if channel is None:
            raise FeedFormatError("an RSS feed without a channel")
        title, entries = (
            _text(_child(channel, "title")),
            [c for c in channel if _local(c.tag) == "item"],
        )
    elif kind == "rdf":  # RSS 1.0: items beside the channel
        channel = _child(root, "channel")
        title = _text(_child(channel if channel is not None else root, "title"))
        entries = [c for c in root if _local(c.tag) == "item"]
    elif kind == "feed":
        title, entries = _text(_child(root, "title")), [c for c in root if _local(c.tag) == "entry"]
    else:
        raise FeedFormatError(f"not a feed (<{kind}>)")
    items: list[Item] = []
    for entry in entries[:MAX_ITEMS]:
        url = _link(entry)
        item_id = _text(_child(entry, "guid", "id")) or url
        heading = _text(_child(entry, "title"))
        if not item_id or not (heading or url):
            continue
        summary = _child(entry, "summary", "description", "content", "encoded")
        published = _child(entry, "pubdate", "published", "updated", "date")
        items.append(
            Item(
                id=item_id[:500],
                title=_plain(heading)[:200],
                url=url[:1000],
                published=_date(_text(published)),
                summary=_plain(_text(summary) if summary is not None else ""),
            )
        )
    return title[:200], items
