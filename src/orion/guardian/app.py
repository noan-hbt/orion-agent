"""Orion's app through the guardian process (D125): run split, the guardian holds its edge.

The core cannot be trusted with what makes an approval worth something, so the guardian keeps:

- **the passkeys**: pairing codes (only from its own user, `orion-admin app pair iphone`),
  registration, sessions (signed with its key), revocation;
- **the approvals**: it writes the question, the app reads it here, the owner's signature on
  this approval and this answer comes here, and only an answer it verified lets the action
  through (`owner_said_yes`). The core waits, asks whether he answered, and presents the grant;
- **the notifications**: the VAPID key, the phones' subscriptions, and the rate limit.

It serves the app's files, pairing, signing in, the approvals and the subscriptions on its
public port (`[modules.mobile] port`), and tells the core, on its socket, whether a session is
valid, whether a phone is paired, and how an approval was answered. The conversation itself is
the core's (`[modules.mobile] core_port`).

    guardian.app.answer  mobile.code.created  mobile.paired  mobile.signed_in  mobile.revoked
    mobile.subscribed  mobile.unsubscribed
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import httpx
import structlog
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from orion.core.events import Event, Trust
from orion.core.projections import ProjectionRunner
from orion.guardian.federation import ADMIN_HEADER, admin_token
from orion.guardian.ledger import APP_ANSWER, APPROVAL_REQUESTED
from orion.guardian.logins import import_logins, keep_login, listing, remove_login
from orion.guardian.service import APP_CONVERSATION
from orion.guardian.vault import (
    CORE_SECRETS,
    Vault,
    core_vault_path,
    vault_path,
)
from orion.guardian.vault import (
    NAME as SECRET_NAME,
)
from orion.mobile.channel import SUBSCRIBED, UNSUBSCRIBED
from orion.mobile.devices import DeviceError, Devices, session_key
from orion.mobile.module import MobileSettings, check_url
from orion.mobile.push import Pusher, PushError, Subscription, Vapid, allowed_endpoint
from orion.mobile.server import edge_app
from orion.mobile.upkeep import CORE_PURPOSE

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.guardian.federation import FederationEdge
    from orion.guardian.ledger import PendingApproval
    from orion.guardian.service import GuardianService

log = structlog.get_logger(__name__)

CONVERSATION = APP_CONVERSATION
PUSH_PREVIEW = 180


@dataclass
class EdgeState:
    """What the guardian's ledger says about the app: questions, answers, subscriptions."""

    name: str = "guardian-app"
    questions: dict[str, tuple[str, str]] = field(default_factory=dict[str, tuple[str, str]])
    """approval id → (the question it wrote, why the core asks)."""
    answers: dict[str, tuple[bool, str]] = field(default_factory=dict[str, tuple[bool, str]])
    """approval id → (yes, how: `passkey:<device>` or `session:<device>`)."""
    subscriptions: dict[str, tuple[str, Subscription]] = field(
        default_factory=dict[str, tuple[str, Subscription]]
    )
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.questions.clear()
        self.answers.clear()
        self.subscriptions.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        p: dict[str, Any] = event.payload
        if event.kind == APPROVAL_REQUESTED and str(p.get("conversation_id")) == CONVERSATION:
            self.questions[str(p["approval_id"])] = (
                str(p.get("question", "")),
                str(p.get("reason", "")),
            )
        elif event.kind == APP_ANSWER:
            self.answers.setdefault(
                str(p["approval_id"]), (bool(p["approved"]), str(p["how"]))
            )  # the first answer is the answer
        elif event.kind == SUBSCRIBED:
            subscription = Subscription(str(p["endpoint"]), str(p["p256dh"]), str(p["auth"]))
            self.subscriptions[subscription.endpoint] = (str(p["device"]), subscription)
        elif event.kind == UNSUBSCRIBED:
            self.subscriptions.pop(str(p["endpoint"]), None)


class AppEdge:
    def __init__(
        self,
        service: GuardianService,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.service = service
        config = service.config
        self.settings = MobileSettings.model_validate(config.modules.get("mobile", {}))
        url = check_url(self.settings.public_url or f"http://localhost:{self.settings.port}")
        self.url = url
        folder = config.guardian.data_dir / "mobile"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        journal = service.ledger.journal
        self.devices = Devices(journal, url, session_key(folder), self.settings.session_days)
        self.vapid = Vapid(folder / "vapid.pem", url) if self.settings.notifications else None
        self._http = http
        self._owns_http = False
        self.state = EdgeState()
        self._runner = ProjectionRunner(journal, [self.state])
        self.admin_token = admin_token(config.guardian.data_dir)
        self.vault: Vault | None = None
        """His secrets (D94), set from the app too (D154)."""
        self.federation: FederationEdge | None = None
        """The Orions he is paired with (D29), when federation is on."""

    def install(self) -> None:
        self.service.app_edge = self
        self.service.routes.extend(
            [
                Route("/v1/app/paired", self._paired, methods=["GET"]),
                Route("/v1/app/session", self._session, methods=["POST"]),
                Route("/v1/app/notify", self._notify, methods=["POST"]),
                Route("/v1/app/approvals/{approval}", self._verdict, methods=["GET"]),
                Route("/v1/app/pair-code", self._pair_code, methods=["POST"]),
                Route("/v1/app/devices", self._devices, methods=["GET"]),
                Route("/v1/app/revoke", self._revoke, methods=["POST"]),
                Route("/v1/app/sign", self._sign, methods=["POST"]),
                Route("/v1/app/signed", self._signed, methods=["POST"]),
            ]
        )
        app = edge_app(
            self.devices,
            self,
            self,
            self.vapid,
            self.url.startswith("https://"),
            self.settings.session_days,
            self,
            lambda: self,
            self,
        )
        self.service.listeners.append(("app", app, self.settings.host, self.settings.port))

    async def sync(self) -> None:
        await self._runner.catch_up()
        await self.service.ledger.catch_up()

    # -- orion.toml, as he changed it in the app (D135) ----------------------------------------

    async def check(self, changes: list[dict[str, Any]]) -> str | None:
        from orion.configure.module import Change, rewrite
        from orion.configure.paths import ConfigureError

        source = self.service.config.source
        if source is None:
            return "Orion runs without an orion.toml"
        try:
            await asyncio.to_thread(rewrite, source, [Change.model_validate(c) for c in changes])
        except (ConfigureError, ValidationError) as exc:
            return str(exc)
        return None

    async def write(self, changes: list[dict[str, Any]], device: str) -> bool:
        """His own change, signed with his passkey for these exact changes: written as he
        asked, his settings included (the signature is his yes to this change)."""
        from orion.configure.module import Change
        from orion.configure.paths import ConfigureError

        try:
            parsed = [Change.model_validate(c) for c in changes]
            done = await self.service.write_config(
                parsed,
                {
                    "changes": [c.described() for c in parsed],
                    "why": "changé dans l'app",
                    "signed_by": f"passkey:{device}",
                },
            )
        except (ConfigureError, ValidationError) as exc:
            raise ValueError(str(exc)) from exc
        return bool(done["changed"])

    # -- his vault, in the app (D142): the core never sees a password -------------------------

    async def listing(self) -> list[dict[str, Any]]:
        return listing(self.service.config)

    async def save(
        self, site: str, username: str, password: str, totp_value: str = "", url: str = ""
    ) -> dict[str, Any]:
        await self.service.ledger.catch_up()
        login = await keep_login(
            self.service.config, self.service.ledger, site, username, password, url, totp_value
        )
        log.info("guardian.login_kept", name=login.name, how="app")
        return {"name": login.name, "sites": login.sites}

    async def remove(self, name: str) -> bool:
        await self.service.ledger.catch_up()
        return await remove_login(self.service.config, self.service.ledger, name)

    async def import_export(self, text: str) -> dict[str, Any]:
        await self.service.ledger.catch_up()
        done = await import_logins(self.service.config, self.service.ledger, text)
        log.info("guardian.logins_imported", kept=len(done["kept"]), how="app")
        return done

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()

    # -- asking and checking ------------------------------------------------------------------

    async def ask(self, approval_id: str, question: str) -> str:
        """A question the guardian wrote: the app shows it from here; the phone is told."""
        await self.push(
            {
                "title": "Orion demande ton accord",
                "body": question[:PUSH_PREVIEW],
                "tag": approval_id,
                "kind": "approval",
            }
        )
        return approval_id

    async def owner_said_yes(self, approval: PendingApproval) -> bool:
        """Only a yes signed by a paired passkey, for this approval and this answer, counts."""
        await self.sync()
        found = self.state.answers.get(approval.id)
        return found is not None and found[0] and found[1].startswith("passkey:")

    def _open(self, approval_id: str) -> PendingApproval | None:
        approval = self.service.ledger.state.approvals.get(approval_id)
        if (
            approval is None
            or approval.conversation_id != CONVERSATION
            or approval.used
            or approval_id in self.state.answers
            or approval.expires_at <= self.service.clock()
        ):
            return None
        return approval

    # -- the desk: what the app reads and answers here ----------------------------------------

    async def pending(self) -> list[dict[str, Any]]:
        await self.sync()
        shown: list[dict[str, Any]] = []
        for approval_id in self.state.questions:
            approval = self._open(approval_id)
            if approval is None:
                continue
            question, reason = self.state.questions[approval_id]
            shown.append(
                {
                    "id": approval.id,
                    "question": question,
                    "tool": approval.action,
                    "arguments": approval.params,
                    "reason": reason,
                    "expires_at": approval.expires_at.isoformat(),
                }
            )
        return shown

    async def options(self, approval_id: str, answer: str) -> tuple[str, str]:
        await self.sync()
        if self._open(approval_id) is None:
            raise LookupError("no such question pending")
        return await self.devices.signing_options(f"approval:{approval_id}:{answer}")

    async def answer(
        self, approval_id: str, yes: bool, ceremony: str, credential: dict[str, Any]
    ) -> str:
        await self.sync()
        if self._open(approval_id) is None:
            raise LookupError("no such question pending")
        device = await self.devices.verify(
            ceremony, f"approval:{approval_id}:{'yes' if yes else 'no'}", credential
        )
        await self._answered(approval_id, yes, f"passkey:{device}")
        return device

    async def refuse(self, approval_id: str, device: str) -> None:
        await self.sync()
        if self._open(approval_id) is None:
            raise LookupError("no such question pending")
        await self._answered(approval_id, False, f"session:{device}")

    async def _answered(self, approval_id: str, yes: bool, how: str) -> None:
        await self.service.ledger.record(
            APP_ANSWER,
            {"approval_id": approval_id, "approved": yes, "how": how},
            Trust.OWNER,
            source="app",
        )
        await self.sync()

    # -- notifications ------------------------------------------------------------------------

    async def subscribe(self, device: str, endpoint: str, p256dh: str, auth: str) -> None:
        if not allowed_endpoint(endpoint):
            raise ValueError("not a known push service")
        await self.service.ledger.record(
            SUBSCRIBED,
            {"device": device, "endpoint": endpoint, "p256dh": p256dh, "auth": auth},
            Trust.OWNER,
            source="app",
        )
        await self.sync()

    async def tell(self, what: str, text: str) -> bool:
        """The guardian's own word to the owner (the core is down, or back), in full: the
        core cannot add it to the conversation. False if no phone took it."""
        return await self.push({"title": "Orion", "body": text, "tag": what, "kind": "message"})

    async def push(self, message: dict[str, object]) -> bool:
        """To every paired phone, within the rate limit; False if nothing could be sent."""
        if self.vapid is None:
            return False
        await self.sync()
        if not self.state.subscriptions:
            return False
        if not self.service.may_send():
            log.warning("guardian.app_push_throttled")
            return False
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=20.0)
            self._owns_http = True
        pusher = Pusher(self.vapid, self._http)
        sent = False
        for endpoint, (device, subscription) in list(self.state.subscriptions.items()):
            try:
                await pusher.send(subscription, message)
                sent = True
            except PushError as exc:
                log.warning("guardian.app_push_failed", device=device, error=str(exc))
                if exc.gone:
                    await self.service.ledger.record(
                        UNSUBSCRIBED, {"endpoint": endpoint, "why": str(exc)}
                    )
        return sent

    # -- core-facing (Unix socket) ------------------------------------------------------------

    def _admin(self, request: Request) -> bool:
        given = request.headers.get(ADMIN_HEADER, "")
        return bool(given) and secrets.compare_digest(given.encode(), self.admin_token.encode())

    async def _paired(self, _request: Request) -> Response:
        return JSONResponse({"paired": await self.devices.any_paired()})

    async def _session(self, request: Request) -> Response:
        cookie = (await request.json()).get("cookie")
        device = await self.devices.session_device(str(cookie) if cookie else None)
        return JSONResponse({"device": device})

    async def _notify(self, request: Request) -> Response:
        sent: dict[str, Any] = await request.json()
        message: dict[str, object] = {
            "title": str(sent.get("title") or "Orion")[:80],
            "body": str(sent.get("body") or "")[:PUSH_PREVIEW],
            "tag": str(sent.get("tag") or "")[:80],
            # What the tap opens in the app (D140); anything else is a plain message.
            "kind": "approval" if sent.get("kind") == "approval" else "message",
        }
        return JSONResponse({"sent": await self.push(message)})

    async def _verdict(self, request: Request) -> Response:
        await self.sync()
        approval_id = str(request.path_params["approval"])
        found = self.state.answers.get(approval_id)
        if found is None:
            return JSONResponse({"answered": False})
        approved, how = found
        return JSONResponse({"answered": True, "approved": approved, "how": how})

    async def _pair_code(self, request: Request) -> Response:
        if not self._admin(request):
            return await self.service.deny("app pairing code", "only the guardian's own user")
        device = str((await request.json()).get("device") or "").strip()
        if not device:
            return JSONResponse({"error": "name the device"}, status_code=400)
        return JSONResponse({"code": await self.devices.new_code(device), "url": self.url})

    async def _devices(self, request: Request) -> Response:
        if not self._admin(request):
            return await self.service.deny("app devices", "only the guardian's own user")
        await self.devices.sync()
        return JSONResponse({"devices": sorted(self.devices.state.devices)})

    async def _revoke(self, request: Request) -> Response:
        if not self._admin(request):
            return await self.service.deny("app revoke", "only the guardian's own user")
        device = str((await request.json()).get("device") or "")
        return JSONResponse({"revoked": await self.devices.revoke(device)})

    # -- what used to need the guardian's shell (D154): his secrets, his paired Orions -------

    async def kept(self) -> dict[str, Any]:
        from orion.setup import read_env

        config = self.service.config
        in_vault = set(read_env(vault_path(config)))
        core_path = core_vault_path(config)
        in_core: set[str] = set(read_env(core_path)) if core_path is not None else set()
        asked = self.vault.asked_now() if self.vault is not None else []
        names = secret_names(config) | in_vault | {a["name"] for a in asked}
        secrets_shown: list[dict[str, str]] = []
        for name in sorted(names):
            if name in CORE_SECRETS:
                where = "app" if name in in_core else "server"
            elif name in in_vault:
                where = "app"
            else:
                where = "server" if os.environ.get(name) else "missing"
            secrets_shown.append({"name": name, "where": where})
        peers: dict[str, Any] | None = None
        if self.federation is not None:
            await self.service.ledger.catch_up()
            peers = {
                "instance": self.federation.card.instance,
                "fingerprint": self.federation.identity.fingerprint,
                "peers": [
                    {"label": p.label, "instance": p.instance}
                    for p in self.federation.state.peers.values()
                    if p.active
                ],
            }
        return {
            "secrets": secrets_shown,
            "asked": asked,
            "vault": self.vault is not None,
            "federation": peers,
            "federation_key": bool(os.environ.get(config.federation.key.env)),
        }

    def purpose(self, what: str, args: dict[str, Any]) -> str:
        """What his signature answers: this very change, nothing else."""
        if what == "secret":
            value = str(args.get("value") or "").strip()
            return f"secret:{args.get('name')}:{_sha(value)}"
        if what == "invite":
            return f"invite:{_label(args)}"
        if what == "accept":
            return f"accept:{_sha(str(args.get('code') or '').strip())}:{_label(args)}"
        if what == "unpair":
            return f"unpair:{str(args.get('peer') or '').strip()[:80]}"
        if what == "federation":
            return "federation:on"
        raise ValueError("unknown change")

    async def keep(self, what: str, args: dict[str, Any], device: str) -> dict[str, Any]:
        if what == "secret":
            if self.vault is None:
                raise LookupError("no vault here")
            name = str(args.get("name") or "")
            where = await self.vault.set_from_app(name, str(args.get("value") or ""), device)
            return {"set": name, "where": where}
        if what == "federation":
            return await self._federation_on(device)
        if self.federation is None:
            raise LookupError("federation is off")
        if what == "invite":
            done = await self._federation_admin("/v1/federation/invites", {"label": _label(args)})
        elif what == "accept":
            done = await self._federation_admin(
                "/v1/federation/accept",
                {"code": str(args.get("code") or "").strip(), "label": _label(args)},
            )
        else:
            done = await self._federation_admin(
                "/v1/federation/revoke", {"peer": str(args.get("peer") or "").strip()}
            )
        log.info("guardian.federation_from_app", what=what, device=device)
        return done

    async def _federation_admin(self, path: str, sent: dict[str, Any]) -> dict[str, Any]:
        """The same admin routes `orion federation` uses, called in this process."""
        transport = httpx.ASGITransport(app=self.service.app())
        async with httpx.AsyncClient(transport=transport, base_url="http://guardian") as http:
            response = await http.post(path, json=sent, headers={ADMIN_HEADER: self.admin_token})
        found: dict[str, Any] = response.json()
        if response.status_code != 200:
            raise ValueError(str(found.get("error") or f"refused ({response.status_code})"))
        return found

    async def _federation_on(self, device: str) -> dict[str, Any]:
        """His Orion's key, kept in the vault, then federation turned on: the guardian
        restarts with it."""
        from orion.configure.module import Change
        from orion.configure.paths import ConfigureError
        from orion.federation.crypto import Identity, generate_secret

        config = self.service.config
        if self.vault is None:
            raise LookupError("no vault here")
        name = config.federation.key.env
        if not os.environ.get(name):
            secret = generate_secret()
            await self.vault.set_from_app(name, secret, device)
        fingerprint = Identity(os.environ[name]).fingerprint
        changes = [Change(path="federation.enabled", value=True)]
        if not config.federation.public_url:
            changes.append(Change(path="federation.public_url", value=self.url))
        try:
            await self.service.write_config(
                changes,
                {
                    "changes": [c.described() for c in changes],
                    "why": "relier d'autres Orions, depuis l'app",
                    "signed_by": f"passkey:{device}",
                },
            )
        except ConfigureError as exc:
            raise ValueError(str(exc)) from exc
        return {"fingerprint": fingerprint}

    # -- signatures for the core's own changes (D154): `core:` purposes only ----------------

    async def _sign(self, request: Request) -> Response:
        purpose = str((await request.json()).get("purpose") or "")
        if not purpose.startswith(CORE_PURPOSE):
            return await self.service.deny(f"app signature {purpose[:60]}", "not the core's")
        try:
            ceremony, options = await self.devices.signing_options(purpose)
        except DeviceError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse({"ceremony": ceremony, "options": json.loads(options)})

    async def _signed(self, request: Request) -> Response:
        sent: dict[str, Any] = await request.json()
        purpose = str(sent.get("purpose") or "")
        if not purpose.startswith(CORE_PURPOSE):
            return await self.service.deny(f"app signature {purpose[:60]}", "not the core's")
        try:
            device = await self.devices.verify(
                str(sent.get("ceremony") or ""), purpose, dict(sent.get("credential") or {})
            )
        except DeviceError as exc:
            log.warning("guardian.app_signature_refused", purpose=purpose, error=str(exc))
            return JSONResponse({"error": str(exc)}, status_code=403)
        log.info("guardian.app_signed", purpose=purpose, device=device)
        return JSONResponse({"device": device})


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _label(args: dict[str, Any]) -> str:
    label = str(args.get("label") or "").strip()
    if not 1 <= len(label) <= 60:
        raise ValueError("a name for the person, 60 characters at most")
    return label


def secret_names(config: OrionConfig) -> set[str]:
    """Every variable orion.toml names for a secret (`{ env = "…" }`), with its defaults."""
    found: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, dict):
            table = cast(dict[str, object], value)
            env = table.get("env")
            if isinstance(env, str) and len(table) == 1 and SECRET_NAME.fullmatch(env):
                found.add(env)
            for item in table.values():
                walk(item)
        elif isinstance(value, list):
            for item in cast(list[object], value):
                walk(item)

    walk(config.model_dump(mode="json"))
    return found
