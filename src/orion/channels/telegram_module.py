"""Telegram as a module: `[channels.telegram]` in orion.toml (off by default; D88).

Create a bot with @BotFather, put its token in the environment (`TELEGRAM_BOT_TOKEN`), write
to it once: Orion logs your user id (`telegram.stranger`), which goes in `owner_id`. Beside
iMessage or instead of it: Orion answers where you wrote from.

With `[guardian] enabled` (D28), the core reaches Telegram through the guardian process, which
holds the token and asks for approvals itself (D89): run `orion serve` without
`TELEGRAM_BOT_TOKEN`.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

from orion.channels.imessage import ApprovalDesk
from orion.channels.telegram import (
    TelegramChannel,
    TelegramClient,
    TelegramError,
    TelegramSettings,
    check_init_data,
)
from orion.modules.base import Daemon, Module, ModuleError, Plate

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.guardian.client import GuardianClient
    from orion.tools.base import Tool

log = structlog.get_logger(__name__)


class TelegramModule(Module):
    name = "telegram"
    channel = True

    def __init__(self, config: OrionConfig, client: TelegramClient | None = None) -> None:
        super().__init__(config)
        self._client = client
        self._desk: ApprovalDesk | None = None
        self._guardian: GuardianClient | None = None
        self.telegram: TelegramChannel | None = None

    @property
    def enabled(self) -> bool:
        return self.config.channels.telegram.enabled

    async def assemble(self, plate: Plate) -> list[Tool]:
        if self.config.guardian.enabled:
            if plate.guardian is None:
                raise ModuleError("channels.telegram: the guardian process is not reachable")
            # The guardian holds the token, and writes and checks the approvals itself (D89).
            self._client = self._client or plate.guardian.telegram()
            self._desk = plate.guardian
            self._guardian = plate.guardian
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
            desk=self._desk,
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
        await self._console(daemon)

    async def _console(self, daemon: Daemon) -> None:
        """The console in his chat, as a Mini App (D91): Telegram tells who opened it."""
        from orion.console.app import PREFIX
        from orion.console.module import ConsoleModule

        found = daemon.plate.module(ConsoleModule)
        owner = self.config.channels.telegram.owner_id
        if found is None or found.console is None or not owner or self._client is None:
            return
        client, guardian = self._client, self._guardian

        async def login(init_data: str) -> int | None:
            if guardian is not None:
                return await guardian.telegram_login(init_data)
            return check_init_data(init_data, client.token, time.time())

        found.console.telegram_login = login
        found.console.owner_telegram = owner
        url = f"{found.base_url()}{PREFIX}/telegram"
        if not url.startswith("https://"):
            log.info("telegram.console_button_skipped", hint="needs [modules.console] public_url")
            return
        try:
            await client.menu_button(owner, "Console", url)
        except TelegramError:
            log.warning("telegram.console_button_failed", exc_info=True)

    async def web_app_button(self, text: str, url: str) -> None:
        """A message to the owner with a button opening a Mini App (Orion's browser, D116)."""
        owner = self.config.channels.telegram.owner_id
        if self._client is None or not owner:
            raise TelegramError("Telegram is not connected to the owner")
        markup = {"inline_keyboard": [[{"text": "Ouvrir", "web_app": {"url": url}}]]}
        await self._client.call("sendMessage", chat_id=owner, text=text, reply_markup=markup)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def doctor(self) -> list[str]:
        cfg = self.config.channels.telegram
        owner = cfg.owner_id or "unknown: write to the bot"
        if self.config.guardian.enabled:
            return [f"bot token held by the guardian, owner id {owner}"]
        try:
            cfg.bot_token.resolve()
        except LookupError as exc:
            return [str(exc)]
        return [f"bot token present, owner id {owner}"]
