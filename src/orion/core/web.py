"""The HTTP servers the core runs under its supervisor.

uvicorn catches SIGTERM itself and returns from `serve()`. Under the supervisor a returning
service looks like a crash: each of the core's servers restarted in a loop while systemd stopped
Orion, until one exhausted its restart budget and the core exited in failure. Here the servers
leave signals alone; `serve.serve` handles them once for the whole process.
"""

from __future__ import annotations

import contextlib
from collections.abc import Generator

import uvicorn


class Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield
