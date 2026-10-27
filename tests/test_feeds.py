"""His reading (D160): the feeds he follows, read every few hours; their new items weighed by
curiosity's judge, never seen by the lead."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from orion.core.events import Provenance
from orion.core.journal import SqliteJournal
from orion.curiosity.module import FOUND, Curiosity, CuriositySettings
from orion.feeds.module import Feeds, FeedSettings, feed_tools
from orion.feeds.parse import FeedFormatError, parse
from orion.tools.base import ToolContext
from orion.web.fetch import FetchError
from tests.fakes import ScriptedProvider, reply
from tests.test_curiosity import LEAD, FakeWeb

PARIS = ZoneInfo("Europe/Paris")
BY = Provenance(source="thread:owner", actor="orion:core")


def rss(*items: tuple[str, str]) -> bytes:
    body = "".join(
        f"<item><title>{t}</title><link>https://blog.example/{i}</link><guid>{i}</guid>"
        f"<pubDate>Mon, 26 Oct 2026 08:00:00 +0100</pubDate>"
        f"<description>&lt;p&gt;Sur {t.lower()}&lt;/p&gt;</description></item>"
        for i, t in items
    )
    channel = f"<channel><title>Le blog</title>{body}</channel>"
    return f'<?xml version="1.0"?><rss version="2.0">{channel}</rss>'.encode()


ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title>
<entry><id>urn:1</id><title>Premier</title><link rel="alternate" href="https://a.example/1"/>
<updated>2026-10-26T08:00:00Z</updated><summary>Court.</summary></entry></feed>"""


def test_rss_and_atom_are_read_and_a_dtd_refused() -> None:
    title, (item,) = parse(rss(("p1", "Le sommeil")))
    assert title == "Le blog" and item.url == "https://blog.example/p1"
    assert item.summary == "Sur le sommeil" and item.published is not None
    title, (entry,) = parse(ATOM)
    assert (title, entry.id, entry.url) == ("Atom", "urn:1", "https://a.example/1")
    with pytest.raises(FeedFormatError):
        parse(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaa">]><rss/>')
    with pytest.raises(FeedFormatError):
        parse(b"<html><body>pas un flux</body></html>")


async def test_a_feed_is_followed_and_only_what_is_new_is_kept(journal: SqliteJournal) -> None:
    served = {
        "https://blog.example/feed": rss(*[(f"p{n}", f"Billet {n}") for n in range(9, 0, -1)])
    }
    now = [datetime(2026, 10, 27, 9, 0, tzinfo=PARIS)]

    async def fetch(url: str) -> bytes:
        if url not in served:
            raise FetchError(f"{url} answered 404")
        return served[url]

    feeds = Feeds(journal, FeedSettings(), "Europe/Paris", fetch, lambda: now[0])
    assert await feeds.follow("https://blog.example/feed", BY) == "Le blog"
    assert len(feeds.recent(now[0])) == 5  # its archive is not news
    served["https://blog.example/feed"] = rss(("p10", "Billet 10"), ("p9", "Billet 9"))
    now[0] += timedelta(hours=3)
    assert await feeds.refresh("https://blog.example/feed") == 1
    assert feeds.recent(now[0])[0].item.title == "Billet 10"

    (tool,) = feed_tools(feeds)
    shown = await tool.handler(tool.input_model(action="list"), ToolContext("owner", "c"))
    assert "Billet 10 (Le blog)" in shown.content and "<lectures>" in shown.content
    bad = await tool.handler(
        tool.input_model(action="add", url="https://nope.example/"), ToolContext("owner", "c")
    )
    assert bad.is_error
    assert await feeds.drop("https://blog.example/feed", BY)
    assert not feeds.urls()


async def test_curiositys_judge_weighs_his_reading_the_lead_never_sees_it(
    journal: SqliteJournal,
) -> None:
    now = datetime(2026, 10, 24, 6, 5, tzinfo=PARIS)

    async def fetch(_url: str) -> bytes:
        return rss(("p1", "Dormir par siestes : ce que disent les ultra-traileurs"))

    feeds = Feeds(journal, FeedSettings(), "Europe/Paris", fetch, lambda: now)
    await feeds.follow("https://blog.example/feed", BY)
    provider = ScriptedProvider(
        reply(json.dumps({**LEAD, "recherches": []}, ensure_ascii=False)),
        reply(json.dumps({"garder": True, "page": 0, "question": "q", "pour_lui": "il court"})),
    )
    web = FakeWeb()
    curiosity = Curiosity(
        journal, provider, web, None, CuriositySettings(), "Europe/Paris", lambda: now
    )
    curiosity.reading = feeds.candidates
    hunt = await curiosity.hunt(now)
    assert hunt.outcome == "found" and web.read_urls == ["https://blog.example/p1"]
    lead_seen = str(provider.requests[0].messages[-1]["content"])
    judged = str(provider.requests[1].messages[-1]["content"])
    assert "siestes" not in lead_seen and "siestes" in judged
    assert any(e.kind == FOUND for e in await journal.read(0, 1000))
