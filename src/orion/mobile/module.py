"""The app as a module: `[modules.mobile]` in orion.toml (off by default; D125).

Orion's own app on the owner's iPhone, a web app added to his home screen: the conversation,
approvals signed with Face ID, notifications. Served by the core on localhost, behind the HTTPS
proxy at `public_url` (a domain: a passkey belongs to a domain, never to an address).

    orion app pair iphone     a one-time code, to type in the app
    orion app devices         the paired devices
    orion app revoke iphone   cut one off

With `[guardian] enabled` (D28), the guardian must serve the app and check the signatures
itself (D125): until it does, the module refuses to load.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import uvicorn
from pydantic import Field

from orion.awaiting.module import AwaitingModule
from orion.initiative.engine import InitiativeSettings
from orion.initiative.planned import everything_planned
from orion.mobile.channel import MobileChannel
from orion.mobile.control import Control
from orion.mobile.devices import Devices, session_key
from orion.mobile.push import Pusher, Vapid
from orion.mobile.server import PREFIX, mobile_app
from orion.modules.base import Daemon, Module, ModuleError, ModuleSettings, Plate
from orion.providers.spending import Spending
from orion.tasks.module import TasksModule

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.tools.base import Tool


class MobileSettings(ModuleSettings):
    host: str = "127.0.0.1"
    port: int = 8795
    public_url: str = Field(
        default="",
        description="Where the phone reaches the app (https://<domain>; a passkey needs a "
        "domain); http://localhost only to try it on this machine",
    )
    approval_timeout_seconds: float = Field(default=900.0, gt=0)
    session_days: int = Field(default=30, ge=1, le=365)
    notifications: bool = Field(default=True, description="Web Push when the app is closed")


def check_url(url: str) -> str:
    """The app's address, or why it cannot be one."""
    url = url.rstrip("/")
    if url.startswith("https://") or url.startswith(("http://localhost", "http://127.0.0.1")):
        return url
    raise ModuleError(
        "modules.mobile: public_url must be https://<a domain> (a passkey belongs to a domain)"
    )


class MobileModule(Module):
    name = "mobile"
    channel = True
    instructions = "app.md"
    offer = (
        "Une application à toi sur son iPhone (écran d'accueil), qui remplace Telegram : "
        "conversation, accords signés avec Face ID, notifications. `[modules.mobile] enabled = "
        "true` et `public_url` (son domaine en https), puis `orion app pair iphone`."
    )
    settings_model = MobileSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.devices: Devices | None = None
        self.app: MobileChannel | None = None
        self.control: Control | None = None
        self._http: httpx.AsyncClient | None = None

    @property
    def folder(self) -> Path:
        return self.config.data_dir / "mobile"

    def url(self) -> str:
        settings: MobileSettings = self.settings
        return check_url(settings.public_url or f"http://localhost:{settings.port}")

    async def assemble(self, plate: Plate) -> list[Tool]:
        if self.config.guardian.enabled:
            raise ModuleError(
                "modules.mobile: with [guardian] enabled, the guardian must serve the app and "
                "check its signatures itself (D125), which it does not do yet"
            )
        settings: MobileSettings = self.settings
        self.devices = Devices(
            plate.journal, self.url(), session_key(self.folder), settings.session_days
        )
        config = self.config
        tasks = plate.module(TasksModule)
        waits = plate.module(AwaitingModule)
        self.control = Control(
            plate.journal,
            plate.memory,
            InitiativeSettings(
                timezone=config.agent.timezone,
                quiet_start=config.initiative.quiet_start,
                quiet_end=config.initiative.quiet_end,
                daily_budget=config.initiative.daily_budget,
            ),
            autonomy=plate.autonomy,
            spending=plate.provider if isinstance(plate.provider, Spending) else None,
            planned=lambda now, until: everything_planned(plate.modules, plate.timers, now, until),
            tasks=lambda: tasks.tasks if tasks is not None else None,
            awaiting=lambda: waits.awaiting if waits is not None else None,
        )
        return []

    async def serve(self, daemon: Daemon) -> None:
        assert self.devices is not None
        settings: MobileSettings = self.settings
        pusher: Pusher | None = None
        vapid: Vapid | None = None
        if settings.notifications:
            vapid = Vapid(self.folder / "vapid.pem", self.url())
            self._http = httpx.AsyncClient(timeout=20.0)
            pusher = Pusher(vapid, self._http)
        channel = MobileChannel(
            daemon.plate.journal,
            daemon.inbox,
            self.devices,
            self.folder,
            pusher,
            settings.approval_timeout_seconds,
        )
        await channel.start()
        daemon.plate.approver.target = daemon.inbox  # it asks where he last wrote from
        self.app = channel
        server = uvicorn.Server(
            uvicorn.Config(
                mobile_app(
                    channel,
                    vapid,
                    self.url().startswith("https://"),
                    settings.session_days,
                    self.control,
                ),
                host=settings.host,
                port=settings.port,
                lifespan="off",
                log_config=None,
                access_log=False,
                proxy_headers=True,
            )
        )
        daemon.add_service("mobile", server.serve)

    async def close(self) -> None:
        if self.app is not None:
            await self.app.close()
        if self._http is not None:
            await self._http.aclose()

    def doctor(self) -> list[str]:
        try:
            return [f"served at {self.url()}{PREFIX}/"]
        except ModuleError as exc:
            return [str(exc)]
