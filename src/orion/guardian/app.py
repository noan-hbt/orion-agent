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

import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import structlog
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from orion.core.events import Event, Trust
from orion.core.projections import ProjectionRunner
from orion.guardian.federation import ADMIN_HEADER, admin_token
from orion.guardian.ledger import APP_ANSWER, APPROVAL_REQUESTED
from orion.guardian.service import APP_CONVERSATION
from orion.mobile.channel import SUBSCRIBED, UNSUBSCRIBED
from orion.mobile.devices import Devices, session_key
from orion.mobile.module import MobileSettings, check_url
from orion.mobile.push import Pusher, PushError, Subscription, Vapid, allowed_endpoint
from orion.mobile.server import edge_app

if TYPE_CHECKING:
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
            ]
        )
        app = edge_app(
            self.devices,
            self,
            self,
            self.vapid,
            self.url.startswith("https://"),
            self.settings.session_days,
        )
        self.service.listeners.append(("app", app, self.settings.host, self.settings.port))

    async def sync(self) -> None:
        await self._runner.catch_up()
        await self.service.ledger.catch_up()

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
