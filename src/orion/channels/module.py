"""The iMessage channel as a module: Orion's main way to reach the owner, through Inkbox.

It owns the Inkbox client and the inbound side (polling or the webhook server). Modules built
on the same Inkbox line (voice) subscribe to its events with `on_event`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import structlog
import uvicorn

from orion.channels.imessage import IMessageChannel, IMessageSettings
from orion.channels.inbox import OwnerInbox
from orion.channels.inkbox import InkboxClient, WebhookEvent
from orion.channels.webhook import webhook_app
from orion.modules.base import Daemon, Module, ModuleError, Plate

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.guardian.client import GuardianClient
    from orion.tools.base import Tool

log = structlog.get_logger(__name__)

EventHandler = Callable[[WebhookEvent], Awaitable[None]]


class IMessageModule(Module):
    name = "imessage"
    channel = True

    def __init__(self, config: OrionConfig, client: InkboxClient | None = None) -> None:
        super().__init__(config)
        self._client = client
        self._owns_client = client is None
        self._handlers: list[tuple[str, EventHandler]] = []
        self._guardian: GuardianClient | None = None
        self.imessage: IMessageChannel | None = None
        self.identity_id: str | None = None

    @property
    def enabled(self) -> bool:
        return self.config.channels.imessage.enabled

    async def assemble(self, plate: Plate) -> list[Tool]:
        self._guardian = plate.guardian
        return []

    @property
    def client(self) -> InkboxClient:
        """Inkbox directly, or through the guardian process, which holds the key (D28)."""
        if self._client is None and self._guardian is not None:
            self._client = self._guardian.inkbox()
        if self._client is None:
            cfg = self.config.channels.imessage
            try:
                key = cfg.api_key.resolve()
            except LookupError as exc:
                raise ModuleError(f"Inkbox: {exc}") from exc
            self._client = InkboxClient(key.get_secret_value(), cfg.base_url)
        return self._client

    def on_event(self, prefix: str, handler: EventHandler) -> None:
        """Inkbox events whose type starts with `prefix` go to `handler` instead of iMessage."""
        self._handlers.append((prefix, handler))

    async def dispatch(self, event: WebhookEvent) -> None:
        for prefix, handler in self._handlers:
            if event.event_type.startswith(prefix):
                await handler(event)
                return
        if self.imessage is not None:
            await self.imessage.handle_webhook(event)

    async def connect(self, plate: Plate, inbox: OwnerInbox) -> IMessageChannel:
        """Check the Inkbox identity, start the channel, make it the approver."""
        cfg = self.config.channels.imessage
        if not cfg.identity or not cfg.owner_number:
            raise ModuleError("channels.imessage needs `identity` and `owner_number` in orion.toml")
        identity = await self.client.identity(cfg.identity)
        if identity is None:
            raise ModuleError(
                f"Inkbox identity {cfg.identity!r} not found: run `orion inkbox setup`"
            )
        if not identity.imessage_enabled:
            raise ModuleError(
                f"iMessage is not enabled on {cfg.identity!r}: run `orion inkbox setup`"
            )
        channel = IMessageChannel(
            self.client,
            plate.journal,
            inbox,
            IMessageSettings(
                owner_number=cfg.owner_number,
                identity_id=identity.id,
                approval_timeout_seconds=cfg.approval_timeout_seconds,
            ),
            desk=self._guardian,
        )
        await channel.start()
        plate.approver.target = channel
        self.imessage = channel
        self.identity_id = identity.id
        return channel

    async def serve(self, daemon: Daemon) -> None:
        cfg = self.config.channels.imessage
        channel = await self.connect(daemon.plate, daemon.inbox)
        if cfg.mode == "poll":
            daemon.add_service("imessage.poll", lambda: channel.poll_forever(cfg.poll_seconds))
        else:
            signing_key = (
                self._guardian.verify_webhook
                if self._guardian is not None
                else cfg.signing_key.resolve().get_secret_value()
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    webhook_app(cfg.webhook_path, signing_key, self.dispatch),
                    host=cfg.webhook_host,
                    port=cfg.webhook_port,
                    log_config=None,
                    access_log=False,
                )
            )
            daemon.add_service("imessage.webhook", server.serve)
        if not await channel.available():
            triage = await self.client.triage_number()
            log.warning(
                "imessage.owner_not_connected",
                hint=f"text '{triage.connect_command}' to {triage.number} from {cfg.owner_number}",
            )

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()

    def doctor(self) -> list[str]:
        cfg = self.config.channels.imessage
        if self.config.guardian.enabled:
            return [f"through the guardian, identity @{cfg.identity or '?'}, {cfg.mode} mode"]
        try:
            cfg.api_key.resolve()
        except LookupError as exc:
            return [str(exc)]
        return [f"api key present, identity @{cfg.identity or '?'}, {cfg.mode} mode"]
