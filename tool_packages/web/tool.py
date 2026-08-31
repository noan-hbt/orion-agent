"""Recherche et lecture web sans service d'API payant.

La recherche utilise la page HTML de DuckDuckGo. La récupération de pages
utilise urllib et extrait le texte des pages HTML. Les limites sont là pour
éviter de remplir le contexte du modèle avec une page entière.
"""

from __future__ import annotations

import ipaddress
import socket
from html import unescape
from html.parser import HTMLParser
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self._skip_depth += 1
        elif tag == "title" and self._skip_depth == 0:
            self._in_title = True
        elif tag in {"p", "div", "li", "section", "article", "br", "h1", "h2", "h3"}:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in {"p", "div", "li", "section", "article", "br", "h1", "h2", "h3"}:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        if self._in_title:
            self.title_parts.append(cleaned)
        self.text_parts.append(cleaned)

    def result(self) -> tuple[str, str]:
        text = " ".join(" ".join(self.text_parts).split())
        return " ".join(self.title_parts).strip(), unescape(text).strip()


class _SearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._capture = False
        self._in_bing_result = False
        self._in_google_result = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = dict(attrs)
        if tag == "li" and "b_algo" in (values.get("class") or "").split():
            self._in_bing_result = True
            return
        if tag == "h3":
            self._in_google_result = True
            return
        if tag != "a":
            return
        classes = (values.get("class") or "").split()
        if "result__a" not in classes and not self._in_bing_result and not self._in_google_result:
            return
        href = values.get("href") or ""
        parsed = urlparse(href)
        if parsed.netloc.endswith("duckduckgo.com"):
            href = parse_qs(parsed.query).get("uddg", [href])[0]
        elif parsed.netloc.endswith("google.com") and parsed.path == "/url":
            href = parse_qs(parsed.query).get("q", [href])[0]
        self._current = {"title": "", "url": unquote(href)}
        self._capture = True

    def handle_data(self, data: str) -> None:
        if self._capture and self._current is not None:
            self._current["title"] += " ".join(data.split())

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self._capture and self._current is not None:
            if self._current["url"].startswith(("http://", "https://")):
                self.results.append(self._current)
            self._current = None
            self._capture = False
        elif tag == "li" and self._in_bing_result:
            self._in_bing_result = False
        elif tag == "h3":
            self._in_google_result = False


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allow_private: bool) -> None:
        super().__init__()
        self.allow_private = allow_private

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Request | None:
        _validate_url(newurl, allow_private=self.allow_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _settings(context: Any) -> Mapping[str, Any]:
    if context is None:
        return {}
    value = context.config.get("web", {})
    return value if isinstance(value, Mapping) else {}


def _validate_url(url: str, *, allow_private: bool) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("L'URL doit utiliser HTTP ou HTTPS.")
    if allow_private:
        return
    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        }
    except socket.gaierror as exc:
        raise ValueError(f"Domaine introuvable : {parsed.hostname}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("L'accès aux adresses réseau privées est désactivé.")


def _open_url(url: str, *, timeout: int, user_agent: str, max_bytes: int, allow_private: bool) -> tuple[str, str, bytes, bool]:
    _validate_url(url, allow_private=allow_private)
    opener = build_opener(_SafeRedirectHandler(allow_private))
    request = Request(url, headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml,text/plain,application/json"})
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
            return response.geturl(), response.headers.get_content_type(), body[:max_bytes], len(body) > max_bytes
    except HTTPError as exc:
        raise RuntimeError(f"La page a répondu HTTP {exc.code}.") from exc
    except URLError as exc:
        raise RuntimeError(f"Page inaccessible : {exc.reason}") from exc


def _decode(body: bytes, content_type: str, charset: str | None = None) -> str:
    encoding = charset or ("utf-8" if content_type in {"text/html", "text/plain", "application/json"} else "utf-8")
    return body.decode(encoding, errors="replace")


def web_search(query: str, max_results: int = 5, *, _context: Any = None) -> dict[str, Any]:
    """Recherche web HTML sans clé API."""
    if not query or not query.strip():
        raise ValueError("La requête ne peut pas être vide.")
    settings = _settings(_context)
    timeout = max(1, int(settings.get("timeout", 15)))
    max_results = min(max(1, int(max_results)), max(1, int(settings.get("max_results", 8))))
    user_agent = str(settings.get("user_agent", "Orion/1.0 (+https://duckduckgo.com/)"))
    encoded_query = quote_plus(query.strip())
    engines = (
        ("duckduckgo", f"https://html.duckduckgo.com/html/?q={encoded_query}"),
        ("bing", f"https://www.bing.com/search?q={encoded_query}"),
        ("google", f"https://www.google.com/search?q={encoded_query}"),
    )
    errors: list[dict[str, str]] = []
    truncated = False
    for engine, url in engines:
        try:
            _, content_type, body, response_truncated = _open_url(
                url,
                timeout=timeout,
                user_agent=user_agent,
                max_bytes=max(100_000, int(settings.get("max_search_bytes", 1_000_000))),
                allow_private=False,
            )
            parser = _SearchParser()
            parser.feed(_decode(body, content_type))
            truncated = truncated or response_truncated
            if parser.results:
                return {
                    "query": query.strip(),
                    "engine": engine,
                    "results": parser.results[:max_results],
                    "truncated": truncated,
                }
        except (RuntimeError, ValueError) as exc:
            errors.append({"engine": engine, "error": str(exc)})
    return {
        "query": query.strip(),
        "engine": None,
        "results": [],
        "truncated": truncated,
        "errors": errors,
        "message": "Les moteurs HTML n'ont fourni aucun résultat exploitable.",
    }


def web_fetch(url: str, max_chars: int = 12000, *, _context: Any = None) -> dict[str, Any]:
    """Récupère le texte lisible d'une page HTTP ou HTTPS."""
    settings = _settings(_context)
    timeout = max(1, int(settings.get("timeout", 15)))
    max_chars = min(max(500, int(max_chars)), max(500, int(settings.get("max_chars", 20000))))
    user_agent = str(settings.get("user_agent", "Orion/1.0"))
    allow_private = bool(settings.get("allow_private", False))
    final_url, content_type, body, truncated = _open_url(
        url,
        timeout=timeout,
        user_agent=user_agent,
        max_bytes=max(100_000, int(settings.get("max_bytes", 2_000_000))),
        allow_private=allow_private,
    )
    if content_type not in {"text/html", "application/xhtml+xml", "text/plain", "application/json", "application/xml", "text/xml"}:
        raise ValueError(f"Type de contenu non pris en charge : {content_type}")
    text = _decode(body, content_type)
    title = ""
    if "html" in content_type or "xhtml" in content_type:
        parser = _PageParser()
        parser.feed(text)
        title, text = parser.result()
    return {
        "url": url,
        "final_url": final_url,
        "content_type": content_type,
        "title": title,
        "text": text[:max_chars],
        "truncated": truncated or len(text) > max_chars,
    }


def register(client: Any, context: Any = None) -> None:
    client.register_tool(
        "web_search",
        lambda query, max_results=5: web_search(query, max_results, _context=context),
        description="Rechercher des pages web avec un moteur HTML, sans clé API.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 8},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    client.register_tool(
        "web_fetch",
        lambda url, max_chars=12000: web_fetch(url, max_chars, _context=context),
        description="Lire le texte utile d'une page HTTP ou HTTPS publique.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "max_chars": {"type": "integer", "minimum": 500, "maximum": 20000},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    )
