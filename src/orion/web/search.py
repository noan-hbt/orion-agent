"""Searching the web, and the pages Orion reads itself (D114).

There used to be a clerk: a separate model that read each page and answered Orion's question,
so that Orion's own model never read the web. The owner wanted no other mind between him and
Orion: Orion now reads the pages itself (`Reading`, the page's text, part after part), and a
search returns only what was found (addresses and titles), never a summary written by another
model. What a page says stays web content (`Trust.WEB`): information, never instructions, and
anything acting on the world after it is put to the owner (D26).

The search engine is the provider's web search, run with Orion's own model; only the pages it
cited come back, so an address can never be made up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from orion.providers.base import ModelProvider, ModelRequest, ProviderError
from orion.web.fetch import Link, Page, normalize

PAGE_CHARS = 60_000
MAX_LINKS = 150

SEARCH_PROMPT = """Search the web for the query. Then reply with only the word OK: the pages
you read are what matters, not your words."""


@dataclass(frozen=True)
class Result:
    url: str
    title: str


@dataclass(frozen=True)
class Findings:
    results: list[Result]


@dataclass(frozen=True)
class Reading:
    url: str
    title: str
    text: str
    """The page's own text, as it is (up to `PAGE_CHARS`)."""
    links: list[Link] = field(default_factory=list[Link])
    images: list[Link] = field(default_factory=list[Link])


class SearchError(RuntimeError):
    """The search engine could not be reached."""


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def reading(page: Page) -> Reading:
    return Reading(
        page.url,
        _clip(page.title, 200),
        page.text[:PAGE_CHARS],
        page.links[:MAX_LINKS],
        page.images,
    )


class WebSearch:
    def __init__(self, provider: ModelProvider, model: str, max_results: int = 6) -> None:
        self._provider = provider
        self._model = model
        self._max_results = max_results

    async def search(self, query: str) -> Findings:
        try:
            response = await self._provider.complete(
                ModelRequest(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": SEARCH_PROMPT},
                        {"role": "user", "content": _clip(query, 300)},
                    ],
                    session_id="orion:web-search",
                    temperature=0,
                    web_search=self._max_results,
                )
            )
        except ProviderError as exc:
            raise SearchError(f"the search engine is unreachable: {exc}") from exc
        results: list[Result] = []
        for cited in response.citations:
            url = normalize(cited.url)
            if url and url not in {r.url for r in results}:
                results.append(Result(url, _clip(cited.title, 120)))
        return Findings(results[: self._max_results + 4])
