"""Mail as a module: `[modules.mail]` in orion.toml."""

from __future__ import annotations

from typing import TYPE_CHECKING

from orion.mail.service import Fetch, Mailbox, MailSettings
from orion.mail.smtp import Postman, Send
from orion.mail.tidy import local_draft, local_tidy
from orion.mail.tools import mail_tools
from orion.modules.base import Daemon, Module, Plate

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.tools.base import Tool


class MailModule(Module):
    name = "mail"
    offer = (
        "Lire ses mails (Gmail, iCloud), lui signaler l'important et répondre avec son "
        "accord : un mot de passe d'application dans `[modules.mail]`."
    )
    instructions = "mail.md"
    settings_model = MailSettings

    def __init__(
        self, config: OrionConfig, fetch: Fetch | None = None, send: Send | None = None
    ) -> None:
        super().__init__(config)
        self._fetch = fetch
        self._send = send
        self.mailbox: Mailbox | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        settings: MailSettings = self.settings
        self.mailbox = Mailbox(
            plate.journal,
            settings,
            self.config.agent.timezone,
            self._fetch or (plate.guardian.mail if plate.guardian else None),
            search=plate.guardian.mail_search if plate.guardian else None,
        )
        await self.mailbox.start()
        postman = Postman(
            plate.journal,
            self.mailbox,
            memory=plate.memory,
            send=self._send or (plate.guardian.mail_send if plate.guardian else None),
        )
        from orion.files.module import FilesModule

        files = plate.module(FilesModule)

        def lookup(file_id: str) -> tuple[str, str, bytes] | None:
            store = files.store if files is not None else None
            made = store.get(file_id) if store is not None else None
            if made is None:
                return None
            return made.name, made.content_type, made.path.read_bytes()

        guardian = plate.guardian
        return mail_tools(
            self.mailbox,
            postman,
            files=lookup,
            tidy=guardian.mail_tidy if guardian else local_tidy,
            draft=guardian.mail_draft if guardian else local_draft,
        )

    async def serve(self, daemon: Daemon) -> None:
        mailbox = self.mailbox
        if mailbox is None:  # pragma: no cover - assemble always runs first
            return
        daemon.add_service("mail.sync", mailbox.run)
        daemon.alerts.append(mailbox)
        daemon.digests.append(mailbox)

    def doctor(self) -> list[str]:
        settings: MailSettings = self.settings
        if not settings.accounts:
            return ["no account: add [[modules.mail.accounts]]"]
        if self.config.guardian.enabled:
            return [f"{len(settings.accounts)} account(s), read by the guardian"]
        lines: list[str] = []
        for account in settings.accounts:
            try:
                account.password.resolve()
                lines.append(f"{account.name} ({account.username} on {account.host}) ok")
            except LookupError as exc:
                lines.append(f"{account.name}: {exc}")
        return lines
