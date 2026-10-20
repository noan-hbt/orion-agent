"""`orion guardian`: the guardian process (D28).

It serves the core on a Unix socket (mode 0660: the core's user must share the socket's group,
nobody else can connect), keeps its ledger in its own directory (mode 0700), and, when the
federation is on, answers other Orions on its public listener.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from pathlib import Path

import structlog
import uvicorn
from starlette.types import ASGIApp

from orion.channels.inkbox import InkboxClient
from orion.config import OrionConfig
from orion.core.hlc import HybridClock
from orion.core.journal import SqliteJournal
from orion.federation.crypto import Identity
from orion.guardian.federation import FederationEdge, admin_token
from orion.guardian.ledger import Ledger
from orion.guardian.service import GuardianError, GuardianService
from orion.guardian.telegram import TelegramGate

log = structlog.get_logger(__name__)


def inkbox_client(config: OrionConfig) -> InkboxClient | None:
    cfg = config.channels.imessage
    if not cfg.enabled:
        return None
    try:
        key = cfg.api_key.resolve()
    except LookupError as exc:
        raise GuardianError(f"Inkbox: {exc}") from exc
    return InkboxClient(key.get_secret_value(), cfg.base_url)


def telegram_gate(service: GuardianService) -> TelegramGate:
    """The bot's token lives here, never in the core (D89)."""
    from orion.channels.telegram import TelegramClient

    try:
        token = service.config.channels.telegram.bot_token.resolve()
    except LookupError as exc:
        raise GuardianError(f"Telegram: {exc}") from exc
    gate = TelegramGate(service, TelegramClient(token.get_secret_value()))
    gate.install()
    if not service.config.channels.telegram.owner_id:
        log.warning("guardian.telegram_owner_unknown", hint="nothing goes out until owner_id")
    return gate


def _server(app: ASGIApp, host: str = "127.0.0.1", port: int = 0) -> uvicorn.Server:
    return uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            lifespan="off",
            log_config=None,
            access_log=False,
        )
    )


def _bind(path: Path) -> socket.socket:
    """Bound and set to 0660 before it accepts anyone (uvicorn would make it 0666)."""
    if not path.parent.exists():
        path.parent.mkdir(parents=True)
        path.parent.chmod(0o750)  # the core's user, in the group, reaches the socket
    with contextlib.suppress(FileNotFoundError):
        path.unlink()  # a stale socket from a previous run
    bound = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bound.bind(str(path))
    path.chmod(0o660)
    return bound


async def _serve_socket(app: ASGIApp, path: Path) -> None:
    bound = _bind(path)
    try:
        log.info("guardian.listening", socket=str(path))
        await _server(app).serve(sockets=[bound])
    finally:
        bound.close()


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def federation_edge(service: GuardianService) -> FederationEdge:
    settings = service.config.federation
    if not settings.public_url:
        raise GuardianError("federation needs `public_url`: the address other Orions reach")
    try:
        identity = Identity(settings.key.resolve().get_secret_value())
    except LookupError as exc:
        raise GuardianError(
            f"federation: {exc} (create one with `orion federation keygen`)"
        ) from exc
    except ValueError as exc:
        raise GuardianError(f"federation: {exc}") from exc
    edge = FederationEdge(service, identity, admin_token(service.config.guardian.data_dir))
    edge.install()
    log.info(
        "guardian.federation", instance=service.config.instance, fingerprint=identity.fingerprint
    )
    return edge


async def run(config: OrionConfig) -> None:
    os.umask(0o077)  # the ledger and everything else the guardian writes: its own only
    _private_dir(config.guardian.data_dir)
    clock = HybridClock(f"{config.instance}-guardian")
    async with SqliteJournal(config.guardian_ledger_path, clock) as journal:
        service = GuardianService(config, Ledger(journal), inkbox_client(config))
        edge = federation_edge(service) if config.federation.enabled else None
        gate = telegram_gate(service) if config.channels.telegram.enabled else None
        try:
            await service.start()
            async with asyncio.TaskGroup() as group:
                group.create_task(_serve_socket(service.app(), config.guardian.socket))
                group.create_task(service.watch_core())
                for name, app, host, port in service.listeners:
                    log.info("guardian.public_listener", name=name, host=host, port=port)
                    group.create_task(_server(app, host=host, port=port).serve())
        finally:
            if edge is not None:
                await edge.aclose()
            if gate is not None:
                await gate.aclose()
            await service.aclose()
