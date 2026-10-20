"""Telegram as a module: `[channels.telegram]` in orion.toml (off by default; D88).

Create a bot with @BotFather, put its token in the environment (`TELEGRAM_BOT_TOKEN`), write
to it once: Orion logs your user id (`telegram.stranger`), which goes in `owner_id`. Beside
iMessage or instead of it: Orion answers where you wrote from.

Not yet through the guardian process (D28): with `[guardian] enabled`, the token would sit in
the core, so the module refuses to load.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from orion.channels.telegram import TelegramChannel, TelegramClient, TelegramSettings
from orion.modules.base import Daemon, Module, ModuleError, Plate

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.tools.base import Tool

log = structlog.get_logger(__name__)


class TelegramModule(Module):
    name = "telegram"
    channel = True

    def __init__(self, config: OrionConfig, client: TelegramClient | None = None) -> None:
        super().__init__(config)
        self._client = client
        self.telegram: TelegramChannel | None = None

    @property
    def enabled(self) -> bool:
        return self.config.channels.telegram.enabled

    def offers(self) -> list[str]:
        if not self.enabled:
            return []
        return [
            "Tant qu'il te parle par Telegram, le gardien séparé ne peut pas tourner : tout ce "
            "qui en dépend (une session prêtée pour agir sur ses comptes, les autres Orion) "
            "reste impossible, même une fois le module activé. Dis-le plutôt que de le promettre."
        ]

    async def assemble(self, plate: Plate) -> list[Tool]:
        if self.config.guardian.enabled:
            raise ModuleError(
                "channels.telegram does not go through the guardian process yet: "
                "turn one of them off"
            )
        if self._client is None:
            try:
                token = self.config.channels.telegram.bot_token.resolve()
            except LookupError as exc:
                raise ModuleError(f"Telegram: {exc}") from exc
            self._client = TelegramClient(token.get_secret_value())
        return []

    async def serve(self, daemon: Daemon) -> None:
        assert self._client is not None
        cfg = self.config.channels.telegram
        me = await self._client.me()
        channel = TelegramChannel(
            self._client,
            daemon.plate.journal,
            daemon.inbox,
            TelegramSettings(cfg.owner_id, cfg.poll_seconds, cfg.approval_timeout_seconds),
        )
        await channel.start()
        daemon.plate.approver.target = daemon.inbox  # it asks where he last wrote from
        self.telegram = channel
        daemon.add_service("telegram.poll", channel.poll_forever)
        if cfg.owner_id == 0:
            log.warning(
                "telegram.owner_unknown",
                bot=me.get("username"),
                hint="write to the bot, then put the id Orion logs in [channels.telegram] owner_id",
            )
        elif not await channel.available():
            log.warning("telegram.owner_not_connected", hint=f"write to @{me.get('username')}")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def doctor(self) -> list[str]:
        cfg = self.config.channels.telegram
        try:
            cfg.bot_token.resolve()
        except LookupError as exc:
            return [str(exc)]
        owner = cfg.owner_id or "unknown: write to the bot"
        return [f"bot token present, owner id {owner}"]
