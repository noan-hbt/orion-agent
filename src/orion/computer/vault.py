"""The owner's logins without the guardian (D120): Orion keeps the vault itself.

The guardian, run apart (D28), keeps the logins out of Orion's process (D111). Run as one
process, Orion keeps them: the same files (`guardian/logins/*.json`, 0600) and the same ledger,
so `orion logins add` and `orion logins import` (a KeePass, Bitwarden or browser CSV export)
fill it either way. What does not change: the model never sees a password. The code types it
into the field Orion names, on the login's sites only, the password into a password field only
(`browser` fill); every page Orion reads has it masked, and no journal holds it.

What it costs: a password is read from a file of Orion's own process, not lent by another one.
A flaw in Orion's code could reach it; the model still could not.

    guardian.login.added  guardian.login.lent  guardian.login.code  (in the guardian's ledger)
"""

from __future__ import annotations

import secrets as tokens
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Protocol

from orion.core.hlc import HybridClock
from orion.core.journal import SqliteJournal
from orion.guardian.ledger import LOGIN_CODE, LOGIN_LENT, Ledger
from orion.guardian.logins import (
    LEND_SECONDS,
    Loan,
    LoginError,
    import_logins,
    keep_login,
    listing,
    logins_dir,
    read_login,
    remove_login,
    totp,
)

if TYPE_CHECKING:
    from orion.config import OrionConfig


class VaultError(RuntimeError):
    """The vault could not do it (no such login, no loan under way)."""


class LoginKeeper(Protocol):
    """Who keeps the logins: the guardian's client (D111), or Orion itself (`LocalVault`)."""

    async def accounts(self) -> dict[str, Any]: ...

    async def lend_login(
        self, name: str, params: dict[str, Any], grant: str | None
    ) -> dict[str, Any]: ...

    async def login_code(self, name: str, loan: str) -> str: ...

    async def keep(
        self, site: str, username: str, password: str, url: str = ""
    ) -> dict[str, Any]: ...


class LocalVault:
    """The vault in Orion's own process, when the guardian does not run apart."""

    def __init__(
        self,
        config: OrionConfig,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._monotonic = monotonic
        self._clock = clock
        self._loans: dict[str, Loan] = {}
        """Loan id → which login, until when; in memory: a restart ends them."""

    @asynccontextmanager
    async def _ledger(self) -> AsyncGenerator[Ledger]:
        path = self.config.guardian_ledger_path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        clock = HybridClock(f"{self.config.instance}-guardian")
        async with SqliteJournal(path, clock) as journal:
            ledger = Ledger(journal)
            await ledger.catch_up()
            yield ledger

    async def accounts(self) -> dict[str, Any]:
        folder = logins_dir(self.config)
        kept: list[dict[str, Any]] = []
        for path in sorted(folder.glob("*.json"), key=lambda p: p.stem) if folder.is_dir() else []:
            login = read_login(self.config, path.stem)
            if login is not None:
                kept.append({"name": login.name, "sites": login.sites, "totp": bool(login.totp)})
        return {"logins": kept}

    async def lend_login(
        self, name: str, params: dict[str, Any], grant: str | None
    ) -> dict[str, Any]:
        del params, grant  # lent when Orion's browser asks (D115), as the guardian does
        login = read_login(self.config, name)
        if login is None:
            raise VaultError(f"no login {name!r} in the vault")
        loan = tokens.token_urlsafe(18)
        now = self._monotonic()
        self._loans = {k: v for k, v in self._loans.items() if v.until > now}
        self._loans[loan] = Loan(name, now + LEND_SECONDS)
        async with self._ledger() as ledger:
            await ledger.record(LOGIN_LENT, {"name": name, "how": "asked"})
        return {
            "username": login.username,
            "password": login.password,
            "sites": login.sites,
            "url": login.url,
            "totp": bool(login.totp),
            "loan": loan,
        }

    async def login_code(self, name: str, loan: str) -> str:
        found = self._loans.get(loan)
        if found is None or found.name != name or found.until <= self._monotonic():
            raise VaultError("no loan of this login under way")
        login = read_login(self.config, name)
        if login is None or not login.totp:
            raise VaultError(f"no 2FA secret for {name!r}")
        async with self._ledger() as ledger:
            await ledger.record(LOGIN_CODE, {"name": name})
        return totp(login.totp, self._clock())

    async def keep(self, site: str, username: str, password: str, url: str = "") -> dict[str, Any]:
        """A login the owner gave, kept in the vault (`keep_login`). Its name and sites."""
        try:
            async with self._ledger() as ledger:
                login = await keep_login(self.config, ledger, site, username, password, url)
        except LoginError as exc:
            raise VaultError(str(exc)) from exc
        return {"name": login.name, "sites": login.sites}

    # -- the vault in the owner's app (D142) ----------------------------------------------------

    async def listing(self) -> list[dict[str, Any]]:
        return listing(self.config)

    async def save(
        self, site: str, username: str, password: str, totp_value: str = "", url: str = ""
    ) -> dict[str, Any]:
        """A login he typed in the app's vault; LoginError (a ValueError) if it is not one."""
        async with self._ledger() as ledger:
            login = await keep_login(self.config, ledger, site, username, password, url, totp_value)
        return {"name": login.name, "sites": login.sites}

    async def remove(self, name: str) -> bool:
        async with self._ledger() as ledger:
            return await remove_login(self.config, ledger, name)

    async def import_export(self, text: str) -> dict[str, Any]:
        async with self._ledger() as ledger:
            return await import_logins(self.config, ledger, text)
