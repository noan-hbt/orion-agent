"""Recherche et lecture web publiques, sans clé API.

La recherche utilise d'abord le flux RSS de Bing, nettement moins fragile que
le HTML d'un moteur. DuckDuckGo HTML et Lite servent de repli. Le module reste
en bibliothèque standard afin qu'un tool installé soit immédiatement utilisable.
"""

from __future__ import annotations

import copy
import http.client
import ipaddress
import json
import os
import socket
import ssl
import threading
import time
from collections import OrderedDict
from html import unescape
from html.parser import HTMLParser
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse
from urllib.request import Request, build_opener
from xml.etree import ElementTree


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36 Orion/1.0"
)
DEFAULT_ENGINES = ("bing_rss", "duckduckgo_html", "duckduckgo_lite")
DEFAULT_API_PROVIDER = "tavily"
DEFAULT_TAVILY_URL = "https://api.tavily.com/search"
_BLOCK_TAGS = {
    "p", "div", "li", "section", "article", "main", "br", "h1", "h2", "h3",
    "h4", "h5", "h6", "blockquote", "pre", "tr", "td", "figure",
}
_SKIP_TAGS = {
    "script", "style", "noscript", "template", "svg", "canvas", "iframe",
    "nav", "footer", "header", "aside", "form", "button", "dialog",
}


class _TTLCache:
    """Petit cache mémoire pour éviter les recherches identiques répétées."""

    def __init__(self) -> None:
        self._items: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at <= time.monotonic():
                self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            return copy.deepcopy(value)

    def put(self, key: str, value: Mapping[str, Any], *, ttl: int, size: int) -> None:
        if ttl <= 0 or size <= 0:
            return
        with self._lock:
            self._items[key] = (time.monotonic() + ttl, copy.deepcopy(dict(value)))
            self._items.move_to_end(key)
            while len(self._items) > size:
                self._items.popitem(last=False)


_SEARCH_CACHE = _TTLCache()
_FETCH_CACHE = _TTLCache()


class _PageParser(HTMLParser):
    """Extrait le texte lisible, en privilégiant article/main."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.description = ""
        self._all_parts: list[str] = []
        self._main_parts: list[str] = []
        self._skip_stack: list[str] = []
        self._main_depth = 0
        self._in_title = False

    def _append_break(self) -> None:
        self._all_parts.append("\n")
        if self._main_depth:
            self._main_parts.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {key.lower(): value or "" for key, value in attrs}
        role = values.get("role", "").lower()
        if tag in _SKIP_TAGS or role in {"navigation", "banner", "contentinfo", "complementary"}:
            self._skip_stack.append(tag)
            return
        if self._skip_stack:
            return
        if tag == "meta" and (
            values.get("name", "").lower() == "description"
            or values.get("property", "").lower() == "og:description"
        ):
            self.description = self.description or " ".join(values.get("content", "").split())
        if tag == "title":
            self._in_title = True
        if tag in {"main", "article"}:
            self._main_depth += 1
        if tag in _BLOCK_TAGS:
            self._append_break()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_stack:
            if tag == self._skip_stack[-1]:
                self._skip_stack.pop()
            return
        if tag == "title":
            self._in_title = False
        if tag in {"main", "article"} and self._main_depth:
            self._main_depth -= 1
        if tag in _BLOCK_TAGS:
            self._append_break()

    def handle_data(self, data: str) -> None:
        if self._skip_stack:
            return
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        if self._in_title:
            self.title_parts.append(cleaned)
            return
        self._all_parts.append(cleaned + " ")
        if self._main_depth:
            self._main_parts.append(cleaned + " ")

    @staticmethod
    def _normalise(parts: list[str]) -> str:
        lines = []
        for raw_line in "".join(parts).splitlines():
            line = " ".join(raw_line.split())
            if line and (not lines or line != lines[-1]):
                lines.append(line)
        return "\n\n".join(lines).strip()

    def result(self) -> tuple[str, str, str]:
        main_text = self._normalise(self._main_parts)
        all_text = self._normalise(self._all_parts)
        text = main_text if len(main_text) >= 400 else all_text
        return " ".join(self.title_parts).strip(), self.description, unescape(text)


class _DuckDuckGoParser(HTMLParser):
    """Parser tolérant aux versions HTML et Lite de DuckDuckGo."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []
        self._capture_snippet = False
        self._snippet_tag: str | None = None

    @staticmethod
    def _target(href: str) -> str:
        parsed = urlparse(href)
        if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
            return unquote(parse_qs(parsed.query).get("uddg", [""])[0])
        return unquote(href)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        classes = set(values.get("class", "").split())
        if tag == "a" and ({"result__a", "result-link"} & classes):
            target = self._target(values.get("href", ""))
            if target.startswith(("https://", "http://")):
                self._current = {"title": "", "url": target, "snippet": ""}
                self._title_parts = []
            return
        if {"result__snippet", "result-snippet"} & classes:
            self._capture_snippet = True
            self._snippet_tag = tag
            self._snippet_parts = []

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if not value:
            return
        if self._current is not None:
            self._title_parts.append(value)
        if self._capture_snippet:
            self._snippet_parts.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current is not None:
            self._current["title"] = " ".join(self._title_parts).strip()
            if self._current["title"]:
                self.results.append(self._current)
            self._current = None
            self._title_parts = []
        if self._capture_snippet and tag == self._snippet_tag:
            snippet = " ".join(self._snippet_parts).strip()
            if snippet and self.results and not self.results[-1].get("snippet"):
                self.results[-1]["snippet"] = snippet
            self._capture_snippet = False
            self._snippet_tag = None


def _settings(context: Any) -> Mapping[str, Any]:
    if context is None:
        return {}
    value = context.config.get("web", {})
    return value if isinstance(value, Mapping) else {}


def _as_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        return min(maximum, max(minimum, int(value)))
    except (TypeError, ValueError):
        return default


def _cache_key(kind: str, **values: Any) -> str:
    return kind + ":" + json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)


def _public_ip(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError as exc:
        raise ValueError(f"Adresse IP invalide : {address}") from exc
    if not ip.is_global:
        raise ValueError("L'accès aux adresses réseau privées est désactivé.")
    return ip


def _validated_addresses(
    hostname: str,
    port: int,
    *,
    allow_private: bool,
) -> list[tuple[int, str]]:
    """Resolve once, validate every result, and return addresses safe to dial.

    The returned IP literals are the exact endpoints used by the transport;
    callers must not pass ``hostname`` back to a resolver before connecting.
    """
    try:
        literal = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        if not allow_private:
            _public_ip(hostname)
        family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
        return [(family, hostname)]

    try:
        infos = socket.getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ValueError(f"Domaine introuvable : {hostname}") from exc

    addresses: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for family, socktype, _proto, _canonname, sockaddr in infos:
        if socktype not in {0, socket.SOCK_STREAM} or family not in {socket.AF_INET, socket.AF_INET6}:
            continue
        address = str(sockaddr[0])
        if not allow_private:
            _public_ip(address)
        item = (family, address)
        if item not in seen:
            seen.add(item)
            addresses.append(item)
    if not addresses:
        raise ValueError(f"Domaine introuvable : {hostname}")
    return addresses


def _validate_url(url: str, *, allow_private: bool) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("L'URL doit utiliser HTTP ou HTTPS.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Les identifiants intégrés à l'URL sont interdits.")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("Port URL invalide.") from exc
    _validated_addresses(parsed.hostname, port, allow_private=allow_private)


def _dial_validated_address(
    family: int,
    address: str,
    port: int,
    timeout: int,
) -> socket.socket:
    """Connect to an already validated numeric IP without DNS resolution."""
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    destination: tuple[Any, ...]
    if family == socket.AF_INET6:
        destination = (address, port, 0, 0)
    else:
        destination = (address, port)
    try:
        sock.connect(destination)
    except Exception:
        sock.close()
        raise
    return sock


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(
        self,
        hostname: str,
        *,
        port: int,
        family: int,
        address: str,
        timeout: int,
    ) -> None:
        super().__init__(hostname, port=port, timeout=timeout)
        self._orion_family = family
        self._orion_address = address

    def connect(self) -> None:
        self.sock = _dial_validated_address(
            self._orion_family,
            self._orion_address,
            int(self.port),
            int(self.timeout),
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        hostname: str,
        *,
        port: int,
        family: int,
        address: str,
        timeout: int,
    ) -> None:
        context = ssl.create_default_context()
        super().__init__(hostname, port=port, timeout=timeout, context=context)
        self._orion_family = family
        self._orion_address = address

    def connect(self) -> None:
        raw = _dial_validated_address(
            self._orion_family,
            self._orion_address,
            int(self.port),
            int(self.timeout),
        )
        try:
            # ``self.host`` is the original URL hostname, not the pinned IP.
            # This preserves both TLS SNI and certificate hostname checking.
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def _connection_for_address(
    *,
    scheme: str,
    hostname: str,
    port: int,
    family: int,
    address: str,
    timeout: int,
) -> http.client.HTTPConnection:
    cls = _PinnedHTTPSConnection if scheme == "https" else _PinnedHTTPConnection
    return cls(
        hostname,
        port=port,
        family=family,
        address=address,
        timeout=timeout,
    )


def _request_pinned(
    url: str,
    *,
    timeout: int,
    headers: Mapping[str, str],
    allow_private: bool,
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("L'URL doit utiliser HTTP ou HTTPS.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Les identifiants intégrés à l'URL sont interdits.")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("Port URL invalide.") from exc

    addresses = _validated_addresses(parsed.hostname, port, allow_private=allow_private)
    target = parsed.path or "/"
    if parsed.params:
        target += ";" + parsed.params
    if parsed.query:
        target += "?" + parsed.query

    last_error: BaseException | None = None
    for family, address in addresses:
        connection = _connection_for_address(
            scheme=parsed.scheme,
            hostname=parsed.hostname,
            port=port,
            family=family,
            address=address,
            timeout=timeout,
        )
        try:
            connection.request("GET", target, headers=dict(headers))
            return connection, connection.getresponse()
        except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException) as exc:
            last_error = exc
            connection.close()
    if last_error is not None:
        raise last_error
    raise OSError("Aucune adresse réseau utilisable.")


def _open_url(
    url: str,
    *,
    timeout: int,
    user_agent: str,
    max_bytes: int,
    allow_private: bool,
    accept: str,
) -> tuple[str, str, str | None, bytes, bool]:
    headers = {
        "User-Agent": user_agent,
        "Accept": accept,
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
    }
    current_url = url
    try:
        for _redirect_count in range(11):
            connection, response = _request_pinned(
                current_url,
                timeout=timeout,
                headers=headers,
                allow_private=allow_private,
            )
            try:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location:
                        raise RuntimeError(
                            f"Le site a répondu HTTP {response.status} sans destination de redirection."
                        )
                    # The next loop iteration resolves, validates, and pins the
                    # redirect target before any bytes are sent to it.
                    current_url = urljoin(current_url, location)
                    continue
                if response.status >= 400:
                    raise RuntimeError(f"Le site a répondu HTTP {response.status}.")
                body = response.read(max_bytes + 1)
                return (
                    current_url,
                    response.headers.get_content_type(),
                    response.headers.get_content_charset(),
                    body[:max_bytes],
                    len(body) > max_bytes,
                )
            finally:
                response.close()
                connection.close()
        raise RuntimeError("Trop de redirections HTTP.")
    except RuntimeError:
        raise
    except (URLError, TimeoutError, OSError, ssl.SSLError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"Site inaccessible : {reason}") from exc


def _decode(body: bytes, charset: str | None) -> str:
    return body.decode(charset or "utf-8", errors="replace")


def _strip_markup(value: str) -> str:
    parser = _PageParser()
    parser.feed(value)
    _, _, text = parser.result()
    return text or " ".join(unescape(value).split())


def _source(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


def _normalise_results(items: list[Mapping[str, str]], *, limit: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        url = str(item.get("url", "")).strip()
        title = " ".join(str(item.get("title", "")).split())
        if not title or not url.startswith(("https://", "http://")):
            continue
        normalized = url.rstrip("/").lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        results.append(
            {
                "title": title,
                "url": url,
                "source": _source(url),
                "snippet": " ".join(str(item.get("snippet", "")).split())[:700],
            }
        )
        if len(results) >= limit:
            break
    for position, result in enumerate(results, start=1):
        result["position"] = position  # type: ignore[assignment]
    return results


def _search_bing_rss(
    query: str,
    *,
    timeout: int,
    user_agent: str,
    max_bytes: int,
) -> list[dict[str, str]]:
    url = f"https://www.bing.com/search?format=rss&q={quote_plus(query)}"
    _, _, charset, body, _ = _open_url(
        url,
        timeout=timeout,
        user_agent=user_agent,
        max_bytes=max_bytes,
        allow_private=False,
        accept="application/rss+xml,application/xml,text/xml;q=0.9,*/*;q=0.5",
    )
    try:
        root = ElementTree.fromstring(_decode(body, charset))
    except ElementTree.ParseError as exc:
        raise RuntimeError("Flux RSS Bing invalide.") from exc
    results = []
    for item in root.findall(".//item"):
        title = " ".join((item.findtext("title") or "").split())
        link = " ".join((item.findtext("link") or "").split())
        description = _strip_markup(item.findtext("description") or "")
        results.append({"title": title, "url": link, "snippet": description})
    return results


def _search_duckduckgo(
    query: str,
    *,
    lite: bool,
    timeout: int,
    user_agent: str,
    max_bytes: int,
) -> list[dict[str, str]]:
    root = "https://lite.duckduckgo.com/lite/" if lite else "https://html.duckduckgo.com/html/"
    url = f"{root}?q={quote_plus(query)}"
    _, _, charset, body, _ = _open_url(
        url,
        timeout=timeout,
        user_agent=user_agent,
        max_bytes=max_bytes,
        allow_private=False,
        accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
    )
    parser = _DuckDuckGoParser()
    parser.feed(_decode(body, charset))
    return parser.results


def _search_tavily(
    query: str,
    *,
    max_results: int,
    timeout: int,
    user_agent: str,
    api_key: str,
    api_url: str,
    search_depth: str,
    topic: str,
) -> list[dict[str, str]]:
    """Recherche via Tavily, fournisseur conçu pour les agents IA."""
    parsed = urlparse(api_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("L'URL de l'API web doit utiliser HTTPS.")
    payload = json.dumps(
        {
            "api_key": api_key,
            "query": query,
            "search_depth": search_depth,
            "topic": topic,
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
    ).encode("utf-8")
    request = Request(
        api_url,
        data=payload,
        method="POST",
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with build_opener().open(request, timeout=timeout) as response:
            raw = response.read(2_000_000)
    except HTTPError as exc:
        # Ne jamais renvoyer la réponse brute : elle pourrait contenir des
        # détails de compte ou des informations inutiles au modèle.
        if exc.code in {401, 403}:
            raise RuntimeError("La clé API Tavily est absente ou invalide.") from exc
        if exc.code == 429:
            raise RuntimeError("Le quota du fournisseur web est momentanément atteint.") from exc
        raise RuntimeError(f"L'API web a répondu HTTP {exc.code}.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"API web inaccessible : {reason}") from exc
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise RuntimeError("Réponse JSON invalide du fournisseur web.") from exc
    if not isinstance(data, Mapping):
        raise RuntimeError("Réponse inattendue du fournisseur web.")
    raw_results = data.get("results", [])
    if not isinstance(raw_results, list):
        raise RuntimeError("Réponse de recherche sans liste de résultats.")
    return [
        {
            "title": str(item.get("title", "")),
            "url": str(item.get("url", "")),
            "snippet": str(item.get("content", "")),
        }
        for item in raw_results
        if isinstance(item, Mapping)
    ]


def _configured_engines(settings: Mapping[str, Any]) -> tuple[str, ...]:
    raw = settings.get("search_engines", DEFAULT_ENGINES)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return DEFAULT_ENGINES
    allowed = {"bing_rss", "duckduckgo_html", "duckduckgo_lite"}
    engines = tuple(str(item).lower() for item in raw if str(item).lower() in allowed)
    return engines or DEFAULT_ENGINES


def _configured_provider(settings: Mapping[str, Any]) -> str:
    value = str(settings.get("provider", "auto")).strip().lower()
    return value if value in {"auto", "tavily", "public"} else "auto"


def web_search(
    query: str,
    max_results: int = 5,
    domain: str | None = None,
    *,
    _context: Any = None,
) -> dict[str, Any]:
    """Recherche des sources publiques avec extraits, sans clé API."""
    if not query or not query.strip():
        raise ValueError("La requête ne peut pas être vide.")
    settings = _settings(_context)
    timeout = _as_int(settings.get("timeout", 20), 20, minimum=1, maximum=120)
    configured_max = _as_int(settings.get("max_results", 8), 8, minimum=1, maximum=10)
    max_results = min(_as_int(max_results, 5, minimum=1, maximum=10), configured_max)
    max_bytes = _as_int(settings.get("max_search_bytes", 1_500_000), 1_500_000, minimum=100_000, maximum=10_000_000)
    cache_ttl = _as_int(settings.get("cache_ttl", 300), 300, minimum=0, maximum=86_400)
    cache_size = _as_int(settings.get("cache_size", 128), 128, minimum=1, maximum=1_000)
    user_agent = str(settings.get("user_agent", DEFAULT_USER_AGENT))
    provider = _configured_provider(settings)
    api_provider = str(settings.get("api_provider", DEFAULT_API_PROVIDER)).strip().lower()
    api_key_env = str(settings.get("api_key_env", "TAVILY_API_KEY"))
    api_key = os.getenv(api_key_env, "").strip()
    requested_domain = (domain or "").strip().removeprefix("https://").removeprefix("http://").strip("/")
    if requested_domain and any(char.isspace() for char in requested_domain):
        raise ValueError("Le filtre domain doit être un nom de domaine, sans espace.")
    effective_query = query.strip()
    if requested_domain:
        effective_query = f"site:{requested_domain} {effective_query}"
    engines = _configured_engines(settings)
    cache_key = _cache_key(
        "search",
        query=effective_query,
        max_results=max_results,
        engines=engines,
        provider=provider,
        api_provider=api_provider,
    )
    cached = _SEARCH_CACHE.get(cache_key)
    if cached is not None:
        cached["cached"] = True
        return cached

    errors: list[dict[str, str]] = []
    if provider in {"auto", "tavily"} and api_provider == "tavily":
        if api_key:
            try:
                raw_results = _search_tavily(
                    effective_query,
                    max_results=max_results,
                    timeout=timeout,
                    user_agent=user_agent,
                    api_key=api_key,
                    api_url=str(settings.get("api_url", DEFAULT_TAVILY_URL)),
                    search_depth=str(settings.get("search_depth", "basic")),
                    topic=str(settings.get("topic", "general")),
                )
                results = _normalise_results(raw_results, limit=max_results)
                if results:
                    payload = {
                        "query": query.strip(),
                        "effective_query": effective_query,
                        "domain": requested_domain or None,
                        "engine": "tavily",
                        "results": results,
                        "cached": False,
                        "message": "Résultats web trouvés via Tavily. Lis les sources pertinentes avec web(action='fetch', ...) avant d'affirmer un fait important.",
                    }
                    _SEARCH_CACHE.put(cache_key, payload, ttl=cache_ttl, size=cache_size)
                    return payload
                errors.append({"engine": "tavily", "error": "Aucun résultat exploitable."})
            except (RuntimeError, ValueError) as exc:
                errors.append({"engine": "tavily", "error": str(exc)})
                if provider == "tavily":
                    # En mode explicite, l'erreur reste visible mais la
                    # recherche publique permet encore une réponse utile.
                    pass
        elif provider == "tavily":
            errors.append({"engine": "tavily", "error": f"Variable {api_key_env} non définie."})

    if provider != "tavily" or not api_key or errors:
        for engine in engines:
            try:
                if engine == "bing_rss":
                    raw_results = _search_bing_rss(
                        effective_query,
                        timeout=timeout,
                        user_agent=user_agent,
                        max_bytes=max_bytes,
                    )
                else:
                    raw_results = _search_duckduckgo(
                        effective_query,
                        lite=engine == "duckduckgo_lite",
                        timeout=timeout,
                        user_agent=user_agent,
                        max_bytes=max_bytes,
                    )
                results = _normalise_results(raw_results, limit=max_results)
                if results:
                    payload = {
                        "query": query.strip(),
                        "effective_query": effective_query,
                        "domain": requested_domain or None,
                        "engine": engine,
                        "results": results,
                        "cached": False,
                        "message": "Résultats publics trouvés. Lis les sources pertinentes avec web(action='fetch', ...) avant d'affirmer un fait important.",
                    }
                    _SEARCH_CACHE.put(cache_key, payload, ttl=cache_ttl, size=cache_size)
                    return payload
                errors.append({"engine": engine, "error": "Aucun résultat exploitable."})
            except (RuntimeError, ValueError) as exc:
                errors.append({"engine": engine, "error": str(exc)})

    payload = {
        "query": query.strip(),
        "effective_query": effective_query,
        "domain": requested_domain or None,
        "engine": None,
        "results": [],
        "cached": False,
        "errors": errors,
        "message": "Aucun moteur n'a fourni de résultat exploitable. Essaie une requête plus précise ou une source connue avec web(action='fetch', ...).",
    }
    _SEARCH_CACHE.put(cache_key, payload, ttl=min(cache_ttl, 60), size=cache_size)
    return payload


def _clip_text(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    candidate = text[:max_chars]
    boundary = max(candidate.rfind("\n"), candidate.rfind(". "), candidate.rfind(" "))
    if boundary >= max_chars // 2:
        candidate = candidate[: boundary + 1]
    return candidate.rstrip() + "…", True


def web_fetch(url: str, max_chars: int = 12000, *, _context: Any = None) -> dict[str, Any]:
    """Récupère le texte lisible d'une page HTTP ou HTTPS publique."""
    settings = _settings(_context)
    timeout = _as_int(settings.get("timeout", 20), 20, minimum=1, maximum=120)
    configured_max = _as_int(settings.get("max_chars", 16000), 16000, minimum=500, maximum=100_000)
    max_chars = min(_as_int(max_chars, 12000, minimum=500, maximum=100_000), configured_max)
    max_bytes = _as_int(settings.get("max_bytes", 2_000_000), 2_000_000, minimum=100_000, maximum=20_000_000)
    cache_ttl = _as_int(settings.get("cache_ttl", 300), 300, minimum=0, maximum=86_400)
    cache_size = _as_int(settings.get("cache_size", 128), 128, minimum=1, maximum=1_000)
    user_agent = str(settings.get("user_agent", DEFAULT_USER_AGENT))
    allow_private = bool(settings.get("allow_private", False))
    cache_key = _cache_key(
        "fetch",
        url=url.strip(),
        max_chars=max_chars,
        allow_private=allow_private,
    )
    cached = _FETCH_CACHE.get(cache_key)
    if cached is not None:
        cached["cached"] = True
        return cached

    final_url, content_type, charset, body, body_truncated = _open_url(
        url,
        timeout=timeout,
        user_agent=user_agent,
        max_bytes=max_bytes,
        allow_private=allow_private,
        accept=(
            "text/html,application/xhtml+xml,text/plain,application/json,"
            "application/xml,text/xml;q=0.9,*/*;q=0.2"
        ),
    )
    supported = {
        "text/html", "application/xhtml+xml", "text/plain", "application/json",
        "application/xml", "text/xml", "application/rss+xml",
    }
    if content_type not in supported:
        raise ValueError(
            f"Type de contenu non pris en charge : {content_type}. "
            "Cette version lit les pages HTML, texte, JSON et XML."
        )

    raw_text = _decode(body, charset)
    title = ""
    description = ""
    if "html" in content_type or "xhtml" in content_type:
        parser = _PageParser()
        parser.feed(raw_text)
        title, description, raw_text = parser.result()
    elif content_type == "application/json":
        try:
            raw_text = json.dumps(json.loads(raw_text), ensure_ascii=False, indent=2)
        except ValueError:
            pass

    text, text_truncated = _clip_text(raw_text, max_chars)
    payload: dict[str, Any] = {
        "url": url,
        "final_url": final_url,
        "source": _source(final_url),
        "content_type": content_type,
        "title": title,
        "description": description,
        "text": text,
        "truncated": body_truncated or text_truncated,
        "cached": False,
    }
    _FETCH_CACHE.put(cache_key, payload, ttl=cache_ttl, size=cache_size)
    return payload


def fetch_url(url: str, *, _context: Any = None) -> str:
    """Compatibilité avec l'ancien tool : retourne directement le texte utile."""
    payload = web_fetch(url, _context=_context)
    parts = []
    if payload.get("title"):
        parts.append(f"Titre : {payload['title']}")
    parts.append(f"URL : {payload.get('final_url', url)}")
    if payload.get("description"):
        parts.append(f"Description : {payload['description']}")
    parts.append(str(payload.get("text", "")))
    if payload.get("truncated"):
        parts.append("[Contenu tronqué selon la limite configurée.]")
    return "\n\n".join(part for part in parts if part.strip())


def fetch_json_api(url: str, *, _context: Any = None) -> str:
    """Récupère une API JSON publique et renvoie un résultat lisible."""
    settings = _settings(_context)
    timeout = _as_int(settings.get("timeout", 20), 20, minimum=1, maximum=120)
    max_bytes = _as_int(settings.get("max_bytes", 2_000_000), 2_000_000, minimum=100_000, maximum=20_000_000)
    user_agent = str(settings.get("user_agent", DEFAULT_USER_AGENT))
    allow_private = bool(settings.get("allow_private", False))
    _, content_type, charset, body, truncated = _open_url(
        url.strip(),
        timeout=timeout,
        user_agent=user_agent,
        max_bytes=max_bytes,
        allow_private=allow_private,
        accept="application/json,application/*+json,text/plain;q=0.8,*/*;q=0.2",
    )
    text = _decode(body, charset)
    try:
        text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except ValueError as exc:
        if "json" not in content_type and not text.lstrip().startswith(("{", "[")):
            raise ValueError(
                f"La réponse n'est pas du JSON (type : {content_type})."
            ) from exc
    if truncated:
        text += "\n[Réponse tronquée selon la limite configurée.]"
    return text


def web(
    action: str,
    *,
    query: str | None = None,
    url: str | None = None,
    max_results: int = 5,
    domain: str | None = None,
    max_chars: int = 12000,
    _context: Any = None,
) -> dict[str, Any] | str:
    """Single model-facing entrypoint for public web read operations."""
    selected = str(action or "").strip().lower()
    if selected == "search":
        if query is None or not str(query).strip():
            raise ValueError("L'action 'search' exige une requête non vide.")
        return web_search(
            str(query),
            max_results=max_results,
            domain=domain,
            _context=_context,
        )
    if selected == "fetch":
        if url is None or not str(url).strip():
            raise ValueError("L'action 'fetch' exige une URL non vide.")
        return web_fetch(str(url), max_chars=max_chars, _context=_context)
    if selected == "json":
        if url is None or not str(url).strip():
            raise ValueError("L'action 'json' exige une URL non vide.")
        return fetch_json_api(str(url), _context=_context)
    raise ValueError("Action web inconnue. Valeurs autorisées : search, fetch, json.")


TOOLS = [web]


def register(client: Any, context: Any = None) -> None:
    client.register_tool(
        "web",
        lambda action, query=None, url=None, max_results=5, domain=None, max_chars=12000: web(
            action,
            query=query,
            url=url,
            max_results=max_results,
            domain=domain,
            max_chars=max_chars,
            _context=context,
        ),
        description=(
            "Accéder au web public en lecture seule avec un seul point d'entrée. "
            "Contrats exacts : web(action='search', query=<texte>[, max_results, domain]); "
            "web(action='fetch', url=<https://...>[, max_chars]); "
            "web(action='json', url=<https://...>). "
            "N'utilise pas query avec fetch/json et n'invente pas d'autres actions."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "fetch", "json"],
                    "description": (
                        "Choisir exactement une action. search exige query; fetch/json exigent url."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": "Requis pour action='search' : question ou mots-clés précis.",
                },
                "url": {
                    "type": "string",
                    "description": "Requis pour action='fetch' ou action='json' : URL HTTP(S) publique.",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "Optionnel, uniquement pour action='search'.",
                },
                "domain": {"type": "string", "description": "Optionnel : limiter à un domaine, ex. who.int."},
                "max_chars": {
                    "type": "integer",
                    "minimum": 500,
                    "maximum": 100000,
                    "description": "Limite de texte pour action='fetch'.",
                },
            },
            "required": ["action"],
            "allOf": [
                {
                    "if": {"properties": {"action": {"const": "search"}}, "required": ["action"]},
                    "then": {"required": ["action", "query"]},
                },
                {
                    "if": {"properties": {"action": {"const": "fetch"}}, "required": ["action"]},
                    "then": {"required": ["action", "url"]},
                },
                {
                    "if": {"properties": {"action": {"const": "json"}}, "required": ["action"]},
                    "then": {"required": ["action", "url"]},
                },
            ],
            "additionalProperties": False,
        },
    )
