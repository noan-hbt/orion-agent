from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from tool_packages.web import tool as web_tool


class _ToolClient:
    def __init__(self) -> None:
        self.registered: dict[str, tuple[object, dict]] = {}

    def register_tool(self, name, callback, **kwargs):
        self.registered[str(name)] = (callback, kwargs)


class _Headers:
    def __init__(self, content_type: str = "text/plain", charset: str | None = "utf-8") -> None:
        self._content_type = content_type
        self._charset = charset

    def get_content_type(self) -> str:
        return self._content_type

    def get_content_charset(self) -> str | None:
        return self._charset


class _Response:
    def __init__(
        self,
        status: int = 200,
        *,
        body: bytes = b"ok",
        location: str | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self._location = location
        self.headers = _Headers()
        self.closed = False

    def getheader(self, name: str) -> str | None:
        if name.lower() == "location":
            return self._location
        return None

    def read(self, _limit: int) -> bytes:
        return self._body

    def close(self) -> None:
        self.closed = True


class _Connection:
    def __init__(self, response: _Response, calls: list[tuple[str, str, int, int, str]]) -> None:
        self._response = response
        self._calls = calls
        self.closed = False

    def request(self, method: str, target: str, *, headers: dict[str, str]) -> None:
        self._calls.append((method, target, 0, 0, headers.get("Host", "")))

    def getresponse(self) -> _Response:
        return self._response

    def close(self) -> None:
        self.closed = True


def _fake_factory(responses, connections):
    iterator = iter(responses)

    def factory(*, scheme, hostname, port, family, address, timeout):
        response = next(iterator)
        calls: list[tuple[str, str, int, int, str]] = []
        connection = _Connection(response, calls)
        connections.append(
            {
                "scheme": scheme,
                "hostname": hostname,
                "port": port,
                "family": family,
                "address": address,
                "timeout": timeout,
                "connection": connection,
                "calls": calls,
            }
        )
        return connection

    return factory


def test_dns_rebinding_connects_only_to_validated_ip(monkeypatch):
    resolutions = [
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    ]
    resolve_calls = []

    def getaddrinfo(*args, **kwargs):
        resolve_calls.append((args, kwargs))
        return resolutions[len(resolve_calls) - 1]

    connections = []
    monkeypatch.setattr(web_tool.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(
        web_tool,
        "_connection_for_address",
        _fake_factory([_Response(body=b"public")], connections),
    )

    _, _, _, body, _ = web_tool._open_url(
        "https://example.com/data",
        timeout=5,
        user_agent="test",
        max_bytes=100,
        allow_private=False,
        accept="text/plain",
    )

    assert body == b"public"
    assert len(resolve_calls) == 1
    assert connections[0]["address"] == "93.184.216.34"
    assert connections[0]["hostname"] == "example.com"


def test_redirect_to_private_address_is_rejected_before_connect(monkeypatch):
    connections = []
    monkeypatch.setattr(
        web_tool.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))
        ],
    )
    monkeypatch.setattr(
        web_tool,
        "_connection_for_address",
        _fake_factory([_Response(status=302, location="http://127.0.0.1/admin")], connections),
    )

    with pytest.raises(ValueError, match="privées"):
        web_tool._open_url(
            "http://example.com/start",
            timeout=5,
            user_agent="test",
            max_bytes=100,
            allow_private=False,
            accept="text/plain",
        )

    assert len(connections) == 1


@pytest.mark.parametrize(
    ("family", "address"),
    [
        (socket.AF_INET, "93.184.216.34"),
        (socket.AF_INET6, "2606:2800:220:1:248:1893:25c8:1946"),
    ],
)
def test_public_ipv4_and_ipv6_are_pinned(monkeypatch, family, address):
    connections = []
    monkeypatch.setattr(
        web_tool.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(family, socket.SOCK_STREAM, 6, "", (address, 443))],
    )
    monkeypatch.setattr(
        web_tool,
        "_connection_for_address",
        _fake_factory([_Response(body=b"ok")], connections),
    )

    web_tool._open_url(
        "https://example.com/",
        timeout=5,
        user_agent="test",
        max_bytes=100,
        allow_private=False,
        accept="text/plain",
    )

    assert connections[0]["family"] == family
    assert connections[0]["address"] == address
    assert connections[0]["hostname"] == "example.com"


def test_normal_public_host_redirect_revalidates_each_hop(monkeypatch):
    answers = {
        "one.example": "93.184.216.34",
        "two.example": "142.250.74.14",
    }
    resolve_calls = []

    def getaddrinfo(host, port, **_kwargs):
        resolve_calls.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[host], port))]

    connections = []
    monkeypatch.setattr(web_tool.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(
        web_tool,
        "_connection_for_address",
        _fake_factory(
            [
                _Response(status=301, location="https://two.example/final"),
                _Response(body=b"done"),
            ],
            connections,
        ),
    )

    final_url, _, _, body, _ = web_tool._open_url(
        "https://one.example/start",
        timeout=5,
        user_agent="test",
        max_bytes=100,
        allow_private=False,
        accept="text/plain",
    )

    assert final_url == "https://two.example/final"
    assert body == b"done"
    assert resolve_calls == [("one.example", 443), ("two.example", 443)]
    assert [item["address"] for item in connections] == [
        "93.184.216.34",
        "142.250.74.14",
    ]


def test_https_connection_uses_original_hostname_for_sni(monkeypatch):
    class _RawSocket:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class _Context:
        def __init__(self):
            self.server_hostname = None

        def wrap_socket(self, raw, *, server_hostname):
            self.server_hostname = server_hostname
            return raw

    raw = _RawSocket()
    context = _Context()
    monkeypatch.setattr(web_tool.ssl, "create_default_context", lambda: context)
    monkeypatch.setattr(web_tool, "_dial_validated_address", lambda *_args: raw)

    connection = web_tool._PinnedHTTPSConnection(
        "example.com",
        port=443,
        family=socket.AF_INET,
        address="93.184.216.34",
        timeout=5,
    )
    connection.connect()

    assert context.server_hostname == "example.com"
    assert connection.sock is raw


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.10.20",
        "::1",
        "fc00::1",
        "fe80::1",
    ],
)
def test_non_global_ipv4_and_ipv6_literals_are_rejected(address):
    with pytest.raises(ValueError, match="privées"):
        web_tool._validated_addresses(address, 443, allow_private=False)


def test_pinned_http_connection_preserves_original_host_header(monkeypatch):
    class _Socket:
        def __init__(self):
            self.sent = bytearray()

        def sendall(self, data):
            self.sent.extend(data)

        def close(self):
            return None

    sock = _Socket()
    monkeypatch.setattr(web_tool, "_dial_validated_address", lambda *_args: sock)
    connection = web_tool._PinnedHTTPConnection(
        "example.com",
        port=80,
        family=socket.AF_INET,
        address="93.184.216.34",
        timeout=5,
    )

    connection.request("GET", "/resource", headers={"User-Agent": "test"})

    request_bytes = bytes(sock.sent)
    assert b"GET /resource HTTP/1.1\r\n" in request_bytes
    assert b"Host: example.com\r\n" in request_bytes


def test_web_registers_only_unified_model_visible_tool():
    client = _ToolClient()
    web_tool.register(client, SimpleNamespace(config={"web": {}}))

    assert set(client.registered) == {"web"}
    _callback, metadata = client.registered["web"]
    parameters = metadata["parameters"]
    assert parameters["properties"]["action"]["enum"] == ["search", "fetch", "json"]
    assert parameters["required"] == ["action"]
    rules = {
        item["if"]["properties"]["action"]["const"]: item["then"]["required"]
        for item in parameters["allOf"]
    }
    assert rules == {
        "search": ["action", "query"],
        "fetch": ["action", "url"],
        "json": ["action", "url"],
    }


def test_web_dispatches_search_fetch_and_json_without_bypassing_helpers(monkeypatch):
    calls = []
    monkeypatch.setattr(
        web_tool,
        "web_search",
        lambda query, max_results=5, domain=None, *, _context=None: calls.append(
            ("search", query, max_results, domain, _context)
        ) or {"kind": "search"},
    )
    monkeypatch.setattr(
        web_tool,
        "web_fetch",
        lambda url, max_chars=12000, *, _context=None: calls.append(
            ("fetch", url, max_chars, _context)
        ) or {"kind": "fetch"},
    )
    monkeypatch.setattr(
        web_tool,
        "fetch_json_api",
        lambda url, *, _context=None: calls.append(("json", url, _context)) or "{}",
    )
    context = object()

    assert web_tool.web(
        "search", query="orion", max_results=3, domain="example.com", _context=context
    ) == {"kind": "search"}
    assert web_tool.web("fetch", url="https://example.com", max_chars=900, _context=context) == {
        "kind": "fetch"
    }
    assert web_tool.web("json", url="https://example.com/api", _context=context) == "{}"
    assert calls == [
        ("search", "orion", 3, "example.com", context),
        ("fetch", "https://example.com", 900, context),
        ("json", "https://example.com/api", context),
    ]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"action": "search"}, "requête"),
        ({"action": "fetch"}, "URL"),
        ({"action": "json"}, "URL"),
        ({"action": "shorten"}, "Action web inconnue"),
    ],
)
def test_web_dispatch_rejects_missing_or_removed_actions(kwargs, message):
    with pytest.raises(ValueError, match=message):
        web_tool.web(**kwargs)
