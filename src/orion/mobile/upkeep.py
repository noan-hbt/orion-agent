"""What used to need a shell on the server, from the app (D154): the core's part.

The owner never logs in to his server any more. What only `orion …` on the server did is here,
in the app's Serveur screen:

- **his misses** (D152): his 👎 and his corrections, with what Orion had answered; and the
  week's report (D156): turns stopped short, tool errors, corrections and cost;
- **the phone's token** (D153): a new one for the Shortcuts automations, with the addresses;
- **his ChatGPT plan** (D62): signed in or not, the sign-in itself (the address to open, then
  the one the browser was sent back to), signing out;
- **his notes** (`orion library import`): files picked on the phone, kept whole in the library;
- **a machine** (D1): the one-time code to pair it. A hand's answers count as his own, so this
  one is signed with Face ID for this very machine: a stolen session cannot add one.

The signature is checked where the passkeys are (`Signer`): the guardian run split, which only
signs purposes starting with `core:` for the core, so nothing the core asks for can stand for an
approval or a settings change. The guardian's own part (his secrets, the Orions he is paired
with, his logins imported) is at the app's edge (`orion.guardian.app`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo

import structlog

from orion.adaptation.misses import misses
from orion.adaptation.week import weeks
from orion.core.events import Event, Provenance
from orion.core.journal import EventStore
from orion.guardian.client import GuardianClient, GuardianUnavailableError
from orion.hands.desk import HandsDesk
from orion.library.module import DOCUMENT_TRUST, MAX_DOC_CHARS, Library
from orion.mobile.control import ControlError
from orion.mobile.devices import DeviceError, Devices
from orion.places.module import PATH, SENSE_PATH, Places

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.providers.chatgpt_auth import ChatGPTAuth

log = structlog.get_logger(__name__)

CORE_PURPOSE = "core:"
"""The only purposes the guardian signs for the core."""
MACHINE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
MISSES_SHOWN = 40
JOURNAL_PAGE = 5000


class UpkeepError(ControlError):
    """Cannot be done from here (off, unknown, refused)."""


class Signer(Protocol):
    async def options(self, purpose: str) -> tuple[str, dict[str, Any]]:
        """(ceremony, WebAuthn options) to sign `purpose` with a paired passkey."""
        ...

    async def verify(self, ceremony: str, purpose: str, credential: dict[str, Any]) -> str:
        """The device that signed this very purpose; UpkeepError otherwise."""
        ...


@dataclass
class LocalSigner:
    """One process: the passkeys are Orion's own."""

    devices: Devices

    async def options(self, purpose: str) -> tuple[str, dict[str, Any]]:
        try:
            ceremony, options = await self.devices.signing_options(purpose)
        except DeviceError as exc:
            raise UpkeepError(str(exc)) from exc
        return ceremony, json.loads(options)

    async def verify(self, ceremony: str, purpose: str, credential: dict[str, Any]) -> str:
        try:
            return await self.devices.verify(ceremony, purpose, credential)
        except DeviceError as exc:
            raise UpkeepError(str(exc)) from exc


@dataclass
class GuardianSigner:
    """Run split: the guardian holds the passkeys and checks the signature."""

    guardian: GuardianClient

    async def options(self, purpose: str) -> tuple[str, dict[str, Any]]:
        try:
            got = await self.guardian.post("/v1/app/sign", {"purpose": purpose})
        except GuardianUnavailableError as exc:
            raise UpkeepError(str(exc)) from exc
        return str(got["ceremony"]), dict(got["options"])

    async def verify(self, ceremony: str, purpose: str, credential: dict[str, Any]) -> str:
        body = {"ceremony": ceremony, "purpose": purpose, "credential": credential}
        try:
            got = await self.guardian.post("/v1/app/signed", body)
        except GuardianUnavailableError as exc:
            raise UpkeepError(str(exc)) from exc
        return str(got["device"])


def machine_purpose(name: str) -> str:
    if not MACHINE_NAME.fullmatch(name):
        raise UpkeepError("a machine's name: lowercase letters, digits and dashes")
    return f"{CORE_PURPOSE}hand:{name}"


@dataclass
class Upkeep:
    journal: EventStore
    config: OrionConfig
    url: str
    """The app's address, where the phone and the machines reach Orion."""
    hands: Callable[[], HandsDesk | None] = lambda: None
    places: Callable[[], Places | None] = lambda: None
    library: Callable[[], Library | None] = lambda: None
    signer: Signer | None = None

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.config.agent.timezone)

    # -- his misses (D152) --------------------------------------------------------------------

    async def _events(self) -> list[Event]:
        events: list[Event] = []
        after = 0
        while page := await self.journal.read(after, JOURNAL_PAGE):
            events.extend(page)
            after = page[-1].seq
        return events

    async def week(self) -> dict[str, object]:
        this, before = weeks(await self._events(), self.zone, datetime.now(self.zone))
        return this.payload(before)

    async def misses(self) -> list[dict[str, Any]]:
        events = await self._events()
        found = misses(events, self.zone, datetime.now(self.zone))[-MISSES_SHOWN:]
        return [
            {
                "at": miss.at.isoformat(),
                "how": miss.how,
                "asked": miss.asked[:400],
                "answered": miss.answered[:600],
                "said": miss.said[:400],
            }
            for miss in reversed(found)
        ]

    # -- the phone's senses (D153) ------------------------------------------------------------

    async def phone_token(self) -> dict[str, str]:
        places = self.places()
        if places is None:
            raise UpkeepError("the places module is off")
        token = await places.new_token()
        log.info("upkeep.phone_token")
        return {"token": token, "event_url": self.url + PATH, "sense_url": self.url + SENSE_PATH}

    # -- his notes, into the library ---------------------------------------------------------

    async def library_import(self, notes: list[tuple[str, str]]) -> dict[str, Any]:
        """(name, text) of notes he picked on his phone: kept whole, as content (never orders).
        A title already in the library is left as it is."""
        library = self.library()
        if library is None:
            raise UpkeepError("the library is off")
        await library.sync()
        known = {title for _, title, _, _ in await library.store.docs()}
        added: list[str] = []
        skipped: list[str] = []
        for name, text in notes:
            if name in known:
                skipped.append(f"{name} (déjà dans la bibliothèque)")
            elif not 10 <= len(text.strip()) <= MAX_DOC_CHARS:
                skipped.append(f"{name} (vide, ou trop long)")
            else:
                provenance = Provenance(source=f"app:{name}", actor="owner:app")
                await library.add(name, text, provenance, DOCUMENT_TRUST, None)
                known.add(name)
                added.append(name)
        log.info("upkeep.library_import", added=len(added), skipped=len(skipped))
        return {"added": added, "skipped": skipped}

    # -- his ChatGPT plan (D62) ---------------------------------------------------------------

    def _chatgpt(self) -> ChatGPTAuth:
        from orion.providers.chatgpt_auth import ChatGPTAuth

        return ChatGPTAuth(self.config.chatgpt_path)

    def chatgpt(self) -> dict[str, Any]:
        from orion.providers.chatgpt_auth import ChatGPTAuthError

        try:
            known = self._chatgpt().load()
        except ChatGPTAuthError as exc:
            return {"enabled": self.config.providers.chatgpt.enabled, "error": str(exc)}
        return {
            "enabled": self.config.providers.chatgpt.enabled,
            "signed_in": known is not None,
            "who": (known.email or known.subject) if known else "",
            "plan": bool(known and known.can_use_plan),
        }

    def chatgpt_start(self) -> str:
        return self._chatgpt().start()

    async def chatgpt_finish(self, redirected: str) -> dict[str, Any]:
        from orion.providers.chatgpt_auth import ChatGPTAuthError

        try:
            await self._chatgpt().finish(redirected)
        except ChatGPTAuthError as exc:
            raise UpkeepError(str(exc)) from exc
        log.info("upkeep.chatgpt_signed_in")
        return self.chatgpt()

    def chatgpt_logout(self) -> bool:
        return self._chatgpt().sign_out()

    # -- a machine, signed with Face ID (D1) --------------------------------------------------

    def _desk(self) -> HandsDesk:
        desk = self.hands()
        if desk is None:
            raise UpkeepError("hands are off")
        return desk

    async def machine_options(self, name: str) -> tuple[str, dict[str, Any]]:
        purpose = machine_purpose(name)
        await self._desk().start()
        if name in self._desk().state.devices:
            raise UpkeepError("a machine has that name already")
        if self.signer is None:
            raise UpkeepError("no passkey here to sign with")
        return await self.signer.options(purpose)

    async def machine_pair(
        self, name: str, ceremony: str, credential: dict[str, Any]
    ) -> dict[str, str]:
        purpose = machine_purpose(name)
        if self.signer is None:
            raise UpkeepError("no passkey here to sign with")
        signer = await self.signer.verify(ceremony, purpose, credential)
        code = await self._desk().new_code(name)
        log.info("upkeep.machine_code", machine=name, device=signer)
        return {
            "code": code,
            "command": f"orion hand pair {self.url} {code} --root ~/Documents",
        }
