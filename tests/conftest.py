from __future__ import annotations

import ipaddress
import os
import socket
import sqlite3
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest


class OfflineNetworkError(RuntimeError):
    """Raised when an offline test attempts to reach a non-local network."""


def _is_loopback_host(host: object) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False
    normalized = host.rstrip(".").lower()
    if normalized in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_local_socket_address(family: int, address: object) -> bool:
    if family not in {socket.AF_INET, socket.AF_INET6}:
        return True
    if not isinstance(address, tuple) or not address:
        return False
    return _is_loopback_host(address[0])


@pytest.fixture(autouse=True)
def _offline_network_guard(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail fast on accidental external network access in offline CI tests.

    Loopback and non-IP sockets remain available for deterministic local test
    servers/IPC.  A test that intentionally exercises the real network must be
    marked ``@pytest.mark.allow_network`` and should not run in the normal CI
    offline suite unless explicitly selected.
    """

    if os.getenv("ORION_TEST_OFFLINE") != "1" or request.node.get_closest_marker("allow_network"):
        yield
        return

    real_socket = socket.socket
    real_getaddrinfo = socket.getaddrinfo
    real_create_connection = socket.create_connection

    def blocked(target: object) -> OfflineNetworkError:
        return OfflineNetworkError(
            f"network access blocked by ORION_TEST_OFFLINE=1: {target!r}; "
            "mark the test with @pytest.mark.allow_network only when real network access is intentional"
        )

    class GuardedSocket(real_socket):
        def connect(self, address: object) -> None:
            if not _is_local_socket_address(self.family, address):
                raise blocked(address)
            return super().connect(address)

        def connect_ex(self, address: object) -> int:
            if not _is_local_socket_address(self.family, address):
                raise blocked(address)
            return super().connect_ex(address)

        def sendto(self, data: bytes, *args: object) -> int:
            # sendto(data, address) or sendto(data, flags, address)
            address = args[-1] if args else None
            if address is not None and not _is_local_socket_address(self.family, address):
                raise blocked(address)
            return super().sendto(data, *args)

    def guarded_getaddrinfo(host: object, *args: object, **kwargs: object):
        if not _is_loopback_host(host):
            raise blocked(host)
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_create_connection(address: tuple[object, ...], *args: object, **kwargs: object):
        host = address[0] if address else None
        if not _is_loopback_host(host):
            raise blocked(address)
        return real_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket, "socket", GuardedSocket)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    yield


@pytest.fixture(autouse=True)
def _close_sqlite_connections(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Close SQLite handles forgotten by a test after its assertions finish."""

    real_connect = sqlite3.connect
    opened: list[tuple[sqlite3.Connection, int]] = []

    def tracked_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)
        opened.append((connection, threading.get_ident()))
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    yield
    current_thread = threading.get_ident()
    for connection, creator_thread in reversed(opened):
        if creator_thread != current_thread:
            continue
        try:
            connection.close()
        except sqlite3.Error:
            pass


@pytest.fixture
def cleanup_resources() -> Iterator[Callable[[Any], Any]]:
    """Register closeable/stoppable test resources for deterministic teardown."""

    resources: list[Any] = []

    def register(resource: Any) -> Any:
        resources.append(resource)
        return resource

    yield register
    for resource in reversed(resources):
        stop = getattr(resource, "stop", None)
        if callable(stop):
            stop()
        close = getattr(resource, "close", None)
        if callable(close):
            close()


@pytest.fixture
def cleanup_threads() -> Iterator[Callable[[threading.Thread, threading.Event | None], threading.Thread]]:
    """Track test-owned threads, signal their stop event, and require teardown."""

    tracked: list[tuple[threading.Thread, threading.Event | None]] = []

    def register(thread: threading.Thread, stop_event: threading.Event | None = None) -> threading.Thread:
        tracked.append((thread, stop_event))
        return thread

    yield register
    for _thread, stop_event in reversed(tracked):
        if stop_event is not None:
            stop_event.set()
    alive: list[str] = []
    for thread, _stop_event in reversed(tracked):
        thread.join(timeout=2.0)
        if thread.is_alive():
            alive.append(thread.name)
    if alive:
        pytest.fail(f"test-owned threads did not stop: {', '.join(alive)}")
