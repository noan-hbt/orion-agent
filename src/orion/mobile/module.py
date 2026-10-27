"""The app as a module: `[modules.mobile]` in orion.toml (off by default; D125).

Orion's own app on the owner's iPhone, a web app added to his home screen: the conversation,
approvals signed with Face ID, notifications. Served on localhost, behind the HTTPS proxy at
`public_url` (a domain: a passkey belongs to a domain, never to an address).

    orion app pair iphone     a one-time code, to type in the app
    orion app devices         the paired devices
    orion app revoke iphone   cut one off

Run as one process, the core serves all of it on `port` (and on `core_port` too, so the same
proxy rules work). Run split (D28), the guardian serves the app's files, the passkeys, the
approvals and the notifications on `port` (`orion.guardian.app`), and the core only the
conversation, `/app/api/`, on `core_port`, checking each session with the guardian; the
pairing commands then run as the guardian's user (`orion-admin app pair iphone`).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx
import structlog
import uvicorn
from pydantic import Field

import orion
from orion.awaiting.module import AwaitingModule
from orion.calendar.module import CalendarModule
from orion.chronicle.module import ChronicleModule
from orion.computer.module import ComputerModule
from orion.configure.module import ConfigureModule, SignedSettings
from orion.guardian.client import GuardianClient, GuardianUnavailableError
from orion.hands.module import HandsModule
from orion.initiative.engine import InitiativeSettings
from orion.initiative.planned import everything_planned
from orion.mail.module import MailModule
from orion.missions.module import MissionsModule
from orion.mobile.channel import MobileChannel
from orion.mobile.control import Control
from orion.mobile.devices import Devices, session_key
from orion.mobile.notebook import Notebook
from orion.mobile.push import Pusher, Vapid
from orion.mobile.server import PREFIX, Gate, core_app, mobile_app
from orion.modules.base import Daemon, Module, ModuleError, ModuleSettings, Plate
from orion.papers.module import PapersModule
from orion.providers.spending import Spending
from orion.relations.module import RelationsModule
from orion.tasks.module import TasksModule
from orion.thinking.module import ThinkingModule

log = structlog.get_logger(__name__)

CODE = Path(orion.__file__).resolve().parents[2]
"""Orion's checkout (`/srv/orion/app` on the server): what the settings show as running."""

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.tools.base import Tool


class MobileSettings(ModuleSettings):
    host: str = "127.0.0.1"
    port: int = 8795
    core_port: int = Field(
        default=8796, description="The conversation, /app/api/ (run split: the core's own port)"
    )
    public_url: str = Field(
        default="",
        description="Where the phone reaches the app (https://<domain>; a passkey needs a "
        "domain); http://localhost only to try it on this machine",
    )
    approval_timeout_seconds: float = Field(default=900.0, gt=0)
    session_days: int = Field(default=30, ge=1, le=365)
    notifications: bool = Field(default=True, description="Web Push when the app is closed")


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class GuardianSessions:
    """Run split: the guardian signed the session, so it says whose it is. Remembered a
    little, so the live stream and each message do not all ask."""

    def __init__(self, guardian: GuardianClient, keep_seconds: float = 30.0) -> None:
        self._guardian = guardian
        self._keep = keep_seconds
        self._known: dict[str, tuple[str | None, float]] = {}

    async def __call__(self, cookie: str | None) -> str | None:
        if not cookie:
            return None
        now = time.monotonic()
        known = self._known.get(cookie)
        if known is not None and now - known[1] < self._keep:
            return known[0]
        try:
            device = (await self._guardian.post("/v1/app/session", {"cookie": cookie}))["device"]
        except GuardianUnavailableError:
            log.warning("mobile.session_unchecked", exc_info=True)
            return None  # fail closed
        found = str(device) if device else None
        if len(self._known) > 256:
            self._known.clear()
        self._known[cookie] = (found, now)
        return found


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
        self.notebook: Notebook | None = None
        self._http: httpx.AsyncClient | None = None
        self._guardian: GuardianClient | None = None

    @property
    def folder(self) -> Path:
        return self.config.data_dir / "mobile"

    def url(self) -> str:
        settings: MobileSettings = self.settings
        return check_url(settings.public_url or f"http://localhost:{settings.port}")

    async def assemble(self, plate: Plate) -> list[Tool]:
        settings: MobileSettings = self.settings
        if self.config.guardian.enabled:
            if plate.guardian is None:
                raise ModuleError("modules.mobile: run split, the guardian must be reachable")
            self._guardian = plate.guardian
        else:
            self.devices = Devices(
                plate.journal, self.url(), session_key(self.folder), settings.session_days
            )
        config = self.config
        tasks = plate.module(TasksModule)
        waits = plate.module(AwaitingModule)
        missions = plate.module(MissionsModule)
        hands = plate.module(HandsModule)
        papers = plate.module(PapersModule)
        relations = plate.module(RelationsModule)
        chronicle = plate.module(ChronicleModule)
        computer = plate.module(ComputerModule)
        calendar = plate.module(CalendarModule)
        mail = plate.module(MailModule)
        thinking = plate.module(ThinkingModule)
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
            config=config,
            missions=lambda: missions.missions if missions is not None else None,
            hands=lambda: hands.desk if hands is not None else None,
            code=CODE,
        )
        self.notebook = Notebook(
            plate.journal,
            self.control.now,
            papers=lambda: papers.vault if papers is not None else None,
            relations=lambda: relations.relations if relations is not None else None,
            chronicle=lambda: chronicle.chronicle if chronicle is not None else None,
            browser=lambda: computer.desk if computer is not None else None,
            calendar=lambda: calendar.calendar if calendar is not None else None,
            mailbox=lambda: mail.mailbox if mail is not None else None,
            thinking=lambda: thinking.thinking if thinking is not None else None,
        )
        return []

    async def serve(self, daemon: Daemon) -> None:
        settings: MobileSettings = self.settings
        if self._guardian is not None:
            channel = MobileChannel(
                daemon.plate.journal,
                daemon.inbox,
                None,
                self.folder,
                approval_timeout_seconds=settings.approval_timeout_seconds,
                guardian=self._guardian,
            )
            gate = Gate(origin_of(self.url()), GuardianSessions(self._guardian))
            apps = [
                ("mobile", core_app(channel, gate, self.control, self.notebook), settings.core_port)
            ]
        else:
            assert self.devices is not None
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
            configure = daemon.plate.module(ConfigureModule)
            signed = (
                SignedSettings(configure.configurator)
                if configure is not None and configure.configurator is not None
                else None
            )
            app = mobile_app(
                channel,
                vapid,
                self.url().startswith("https://"),
                settings.session_days,
                self.control,
                self.notebook,
                signed,
            )
            apps = [("mobile", app, settings.port), ("mobile-api", app, settings.core_port)]
        await channel.start()
        desk = self.notebook.browser() if self.notebook is not None else None
        if desk is not None:
            desk.in_app = True  # a hand-over shows in Maintenant (D135)
        daemon.plate.approver.target = daemon.inbox  # it asks where he last wrote from
        self.app = channel
        daemon.add_service("mobile.changes", channel.changes)  # the open app follows (D140)
        for name, app, port in apps:
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host=settings.host,
                    port=port,
                    lifespan="off",
                    log_config=None,
                    access_log=False,
                    proxy_headers=True,
                )
            )
            daemon.add_service(name, server.serve)

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
