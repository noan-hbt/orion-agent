"""The app's HTTP side (D125), under `/app/`, in two parts.

**The edge** (`edge_routes`): the app's own files, pairing, signing in, the approvals, the
notifications, the phones (another one added, or one let go, each signed with Face ID), and his
vault of logins (D142).
Run split (D28), the guardian serves it on the public port: the passkeys, the text of a
question and the signature that answers it never pass through the core, which could otherwise
show one action and get another signed.

**The conversation** (`core_routes`, under `/app/api/`): messages, files, the live stream, the
controls. The core serves it; run split, on its own port, checking each session with the
guardian, which signed it.

Run as one process, both parts are one app (`mobile_app`). Everything but pairing and signing
in needs a session (a cookie, HttpOnly, Secure, SameSite=Strict, for `/app/` only). A request
that changes something must come from the app's own origin. Uploads come as JSON (base64): no
form parser, nothing else to trust.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING, Any, Protocol, cast

import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Route

from orion.mobile.channel import MAX_FILE_BYTES, MobileChannel
from orion.mobile.control import Control, ControlError
from orion.mobile.devices import DeviceError, Devices
from orion.mobile.notebook import Notebook
from orion.mobile.push import Vapid

if TYPE_CHECKING:
    from orion.mobile.upkeep import Upkeep

log = structlog.get_logger(__name__)

PREFIX = "/app"
COOKIE = "orion_app"
HEARTBEAT_SECONDS = 20.0
MAX_FAILURES = 10
"""Wrong pairing codes before pairing stops until a restart."""
DEVICE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
"""A phone's name, as `orion app pair <name>` takes it."""
MAX_BODY = 40 * 1024 * 1024
"""Base64 makes a 25 MB file 34 MB."""
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; "
    "media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'"
)
STATIC = {
    "": ("index.html", "text/html; charset=utf-8"),
    "manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "app.js": ("app.js", "text/javascript; charset=utf-8"),
    "app.css": ("app.css", "text/css; charset=utf-8"),
    "icon.svg": ("icon.svg", "image/svg+xml"),
    "orion-avatar.svg": ("orion-avatar.svg", "image/svg+xml"),
    "icon-180.png": ("icon-180.png", "image/png"),
    "icon-512.png": ("icon-512.png", "image/png"),
}
API_HEADERS = {"cache-control": "no-store", "referrer-policy": "no-referrer"}

Handler = Callable[[Request], Awaitable[Response]]
Guarded = Callable[[Request, str], Awaitable[Response]]
Session = Callable[[str | None], Awaitable[str | None]]
"""The session cookie → the device it was opened for, or None."""


class Desk(Protocol):
    """The approvals the app shows and answers: the channel itself in one process, the guardian
    run split."""

    async def pending(self) -> list[dict[str, Any]]: ...

    async def options(self, approval_id: str, answer: str) -> tuple[str, str]:
        """(ceremony, WebAuthn options) to sign this answer; LookupError if not pending."""
        ...

    async def answer(
        self, approval_id: str, yes: bool, ceremony: str, credential: dict[str, Any]
    ) -> str:
        """The device that signed (LookupError, DeviceError otherwise)."""
        ...

    async def refuse(self, approval_id: str, device: str) -> None: ...


class Settings(Protocol):
    """Who writes orion.toml for a change he signed in the app (D135): the guardian run
    split, the configure module in one process. The model is never in between."""

    async def check(self, changes: list[dict[str, Any]]) -> str | None:
        """What is wrong with these changes (the whole file checked), before he signs."""
        ...

    async def write(self, changes: list[dict[str, Any]], device: str) -> bool:
        """Write them; whether anything changed. Raises ValueError when it cannot."""
        ...


def settings_purpose(changes: list[dict[str, Any]]) -> str:
    """The signature answers these exact changes, and nothing else."""
    canonical = json.dumps(changes, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "config:" + hashlib.sha256(canonical.encode()).hexdigest()


class Logins(Protocol):
    """His vault (D142), where it is kept: Orion's own in one process, the guardian's run split.
    Only the edge reaches it: a password typed in the app never passes through the core."""

    async def listing(self) -> list[dict[str, Any]]:
        """Each login's name, sites, username and whether it has 2FA: never a password."""
        ...

    async def save(
        self, site: str, username: str, password: str, totp_value: str = "", url: str = ""
    ) -> dict[str, Any]:
        """Kept (the same account's password replaced): its name and sites. ValueError if it
        is not a login."""
        ...

    async def remove(self, name: str) -> bool: ...

    async def import_export(self, text: str) -> dict[str, Any]:
        """A password manager's CSV export: `kept` (names, sites) and `skipped` (why)."""
        ...


class Keeper(Protocol):
    """What used to need the guardian's shell (D154): his secrets and the Orions he is paired
    with. Each change is signed with his passkey for that very change (`purpose`)."""

    async def kept(self) -> dict[str, Any]:
        """The secrets' names and where each is set, those Orion asked for, the paired Orions."""
        ...

    def purpose(self, what: str, args: dict[str, Any]) -> str:
        """ValueError for an unknown change or wrong arguments."""
        ...

    async def keep(self, what: str, args: dict[str, Any], device: str) -> dict[str, Any]:
        """Done; ValueError or LookupError when it cannot be."""
        ...


class Subscriber(Protocol):
    """Where a phone's push subscription is kept: beside the key that signs the pushes."""

    async def subscribe(self, device: str, endpoint: str, p256dh: str, auth: str) -> None: ...


def _refused(why: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": why}, status_code=status, headers=API_HEADERS)


def _ok(body: object) -> JSONResponse:
    return JSONResponse(body, headers=API_HEADERS)


async def body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise ValueError("too large")
    parsed: object = json.loads(raw or b"{}")
    if not isinstance(parsed, dict):
        raise TypeError("not an object")
    return cast(dict[str, Any], parsed)


@dataclass(frozen=True)
class Gate:
    """Who may call: the app's own origin, and for most routes a session."""

    origin: str
    session: Session

    def same_origin(self, request: Request) -> bool:
        origin = request.headers.get("origin")
        return origin is None or origin == self.origin

    def public(self, handler: Handler) -> Handler:
        async def check(request: Request) -> Response:
            if request.method == "POST" and not self.same_origin(request):
                return _refused("cross-site request refused", 403)
            try:
                return await handler(request)
            except (ValueError, KeyError, TypeError) as exc:
                log.info("mobile.bad_request", path=request.url.path, error=str(exc))
                return _refused("malformed request")

        return check

    def guarded(self, handler: Guarded) -> Handler:
        async def check(request: Request) -> Response:
            device = await self.session(request.cookies.get(COOKIE))
            if device is None:
                return _refused("sign in", 401)
            if request.method == "POST" and not self.same_origin(request):
                return _refused("cross-site request refused", 403)
            try:
                return await handler(request, device)
            except (ValueError, KeyError, TypeError) as exc:
                log.info("mobile.bad_request", path=request.url.path, error=str(exc))
                return _refused("malformed request")

        return check


# -- the edge: files, pairing, sessions, approvals, notifications --------------------------------


def edge_routes(
    devices: Devices,
    desk: Desk,
    subscriber: Subscriber,
    vapid: Vapid | None,
    secure_cookie: bool,
    session_days: int,
    settings: Settings | None = None,
    logins: Callable[[], Logins | None] = lambda: None,
    keeper: Keeper | None = None,
) -> list[BaseRoute]:
    gate = Gate(devices.origin, devices.session_device)
    failures = [0]

    def opened(device: str) -> Response:
        response = _ok({"device": device})
        response.set_cookie(
            COOKIE,
            devices.session(device),
            max_age=session_days * 86400,
            httponly=True,
            secure=secure_cookie,
            samesite="strict",
            path=PREFIX,
        )
        return response

    async def static(request: Request) -> Response:
        found = STATIC.get(request.path_params.get("name", ""))
        if found is None:
            return Response("not found", status_code=404)
        name, content_type = found
        data = resources.files("orion.mobile").joinpath("static", name).read_bytes()
        headers = {
            "content-security-policy": CSP,
            "referrer-policy": "no-referrer",
            "x-content-type-options": "nosniff",
            "cache-control": "no-cache",
        }
        if name == "sw.js":
            headers["service-worker-allowed"] = f"{PREFIX}/"
        return Response(data, media_type=content_type, headers=headers)

    async def pair_options(request: Request) -> Response:
        if failures[0] >= MAX_FAILURES:
            return _refused("too many wrong codes: restart Orion to pair again", 429)
        sent = await body(request)
        try:
            ceremony, options = await devices.pairing_options(str(sent["code"]))
        except DeviceError as exc:
            failures[0] += 1
            log.warning("mobile.pair_refused", error=str(exc), failures=failures[0])
            return _refused(str(exc), 403)
        return _ok({"ceremony": ceremony, "options": json.loads(options)})

    async def pair(request: Request) -> Response:
        sent = await body(request)
        try:
            device = await devices.pair(str(sent["ceremony"]), dict(sent["credential"]))
        except DeviceError as exc:
            log.warning("mobile.pair_refused", error=str(exc))
            return _refused(str(exc), 403)
        log.info("mobile.paired", device=device)
        return opened(device)

    async def login_options(_request: Request) -> Response:
        try:
            ceremony, options = await devices.signing_options("session")
        except DeviceError as exc:
            return _refused(str(exc), 409)
        return _ok({"ceremony": ceremony, "options": json.loads(options)})

    async def login(request: Request) -> Response:
        sent = await body(request)
        try:
            device = await devices.verify(
                str(sent["ceremony"]), "session", dict(sent["credential"])
            )
        except DeviceError as exc:
            log.warning("mobile.login_refused", error=str(exc))
            return _refused(str(exc), 403)
        return opened(device)

    async def logout(_request: Request, _device: str) -> Response:
        response = _ok({})
        response.delete_cookie(COOKIE, path=PREFIX)
        return response

    async def me(_request: Request, device: str) -> Response:
        return _ok({"device": device, "push_key": vapid.public_key if vapid else None})

    async def approvals(_request: Request, _device: str) -> Response:
        return _ok({"approvals": await desk.pending()})

    async def approval_options(request: Request, _device: str) -> Response:
        approval_id = str(request.path_params["approval"])
        answer = str((await body(request))["answer"])
        if answer not in ("yes", "no"):
            return _refused("yes or no")
        try:
            ceremony, options = await desk.options(approval_id, answer)
        except LookupError:
            return _refused("no such question", 404)
        return _ok({"ceremony": ceremony, "options": json.loads(options)})

    async def approval_refuse(request: Request, device: str) -> Response:
        approval_id = str(request.path_params["approval"])
        try:
            await desk.refuse(approval_id, device)
        except LookupError:
            return _refused("no such question pending", 404)
        log.info("mobile.answered", approval=approval_id, answer="no", device=device)
        return _ok({})

    async def approval_answer(request: Request, _device: str) -> Response:
        approval_id = str(request.path_params["approval"])
        sent = await body(request)
        answer = str(sent["answer"])
        if answer not in ("yes", "no"):
            return _refused("yes or no")
        try:
            device = await desk.answer(
                approval_id, answer == "yes", str(sent["ceremony"]), dict(sent["credential"])
            )
        except LookupError:
            return _refused("no such question pending", 404)
        except DeviceError as exc:
            log.warning("mobile.answer_refused", approval=approval_id, error=str(exc))
            return _refused(str(exc), 403)
        log.info("mobile.answered", approval=approval_id, answer=answer, device=device)
        return _ok({})

    # -- the phones: listed, and another one added or let go, each signed with Face ID -------

    async def phones(_request: Request, device: str) -> Response:
        await devices.sync()
        return _ok({"devices": sorted(devices.state.devices), "this": device})

    def purpose(request: Request, device: str) -> str:
        """`pair:<name>` or `revoke:<name>`: a signature answers that one change only."""
        how, name = str(request.path_params["how"]), str(request.path_params["name"])
        if how not in ("pair", "revoke") or not DEVICE_NAME.fullmatch(name):
            raise ValueError("pair or revoke, and a name of lowercase letters and digits")
        if how == "revoke" and name == device:
            raise ValueError("this phone: sign out instead")
        return f"{how}:{name}"

    async def phone_options(request: Request, device: str) -> Response:
        try:
            ceremony, options = await devices.signing_options(purpose(request, device))
        except DeviceError as exc:
            return _refused(str(exc), 409)
        return _ok({"ceremony": ceremony, "options": json.loads(options)})

    async def phone_change(request: Request, device: str) -> Response:
        wanted = purpose(request, device)
        sent = await body(request)
        try:
            signer = await devices.verify(str(sent["ceremony"]), wanted, dict(sent["credential"]))
        except DeviceError as exc:
            log.warning("mobile.phone_refused", change=wanted, error=str(exc))
            return _refused(str(exc), 403)
        how, name = wanted.split(":", 1)
        log.info("mobile.phone_changed", change=wanted, device=signer)
        if how == "pair":
            if name in devices.state.devices:
                return _refused("a phone has that name already", 409)
            return _ok({"code": await devices.new_code(name), "device": name})
        if not await devices.revoke(name):
            return _refused("no such phone", 404)
        return _ok({})

    # -- orion.toml, changed by him from the app, signed with Face ID ------------------------

    def changes_of(sent: dict[str, Any]) -> list[dict[str, Any]]:
        changes = cast(list[dict[str, Any]], sent["changes"])
        if not isinstance(changes, list) or not 1 <= len(changes) <= 10:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError("one to ten changes")
        return [
            {"path": str(c["path"]), "value": c.get("value"), "op": str(c.get("op") or "set")}
            for c in changes
        ]

    async def settings_options(request: Request, _device: str) -> Response:
        if settings is None:
            return _refused("settings are changed by message here", 404)
        changes = changes_of(await body(request))
        wrong = await settings.check(changes)
        if wrong is not None:
            return _refused(wrong, 422)
        try:
            ceremony, options = await devices.signing_options(settings_purpose(changes))
        except DeviceError as exc:
            return _refused(str(exc), 409)
        return _ok({"ceremony": ceremony, "options": json.loads(options)})

    async def settings_write(request: Request, _device: str) -> Response:
        if settings is None:
            return _refused("settings are changed by message here", 404)
        sent = await body(request)
        changes = changes_of(sent)
        try:
            signer = await devices.verify(
                str(sent["ceremony"]), settings_purpose(changes), dict(sent["credential"])
            )
        except DeviceError as exc:
            log.warning("mobile.settings_refused", error=str(exc))
            return _refused(str(exc), 403)
        try:
            changed = await settings.write(changes, signer)
        except ValueError as exc:
            return _refused(str(exc), 422)
        log.info("mobile.settings_written", paths=[c["path"] for c in changes], device=signer)
        return _ok({"changed": changed})

    # -- his vault: listed, a login kept or let go (D142) ------------------------------------

    async def vault_list(_request: Request, _device: str) -> Response:
        vault = logins()
        if vault is None:
            return _refused("the vault is off", 404)
        return _ok({"logins": await vault.listing()})

    async def vault_save(request: Request, device: str) -> Response:
        vault = logins()
        if vault is None:
            return _refused("the vault is off", 404)
        sent = await body(request)
        fields = ("site", "username", "password", "totp", "url")
        site, username, password, totp_value, url = (str(sent.get(f) or "") for f in fields)
        if len(password) > 500 or len(username) > 200 or len(site) + len(url) > 2000:
            return _refused("too long")
        try:
            kept = await vault.save(site, username, password, totp_value, url)
        except ValueError as exc:  # what is wrong with the login, never what was typed
            return _refused(str(exc), 422)
        log.info("mobile.login_kept", name=kept["name"], device=device)
        return _ok(kept)

    async def vault_remove(request: Request, device: str) -> Response:
        vault = logins()
        if vault is None:
            return _refused("the vault is off", 404)
        name = str(request.path_params["name"])
        if not await vault.remove(name):
            return _refused("no such login", 404)
        log.info("mobile.login_removed", name=name, device=device)
        return _ok({})

    async def vault_import(request: Request, device: str) -> Response:
        vault = logins()
        if vault is None:
            return _refused("the vault is off", 404)
        text = str((await body(request)).get("text") or "")
        if not text.strip() or len(text) > 5_000_000:
            return _refused("an export, 5 MB at most")
        done = await vault.import_export(text)
        log.info("mobile.logins_imported", kept=len(done["kept"]), device=device)
        return _ok(done)

    # -- his secrets and his paired Orions, signed with Face ID (D154) -----------------------

    async def keep_list(_request: Request, _device: str) -> Response:
        if keeper is None:
            return _refused("not available here", 404)
        return _ok(await keeper.kept())

    async def keep_options(request: Request, _device: str) -> Response:
        if keeper is None:
            return _refused("not available here", 404)
        sent = await body(request)
        try:
            wanted = keeper.purpose(str(request.path_params["what"]), dict(sent.get("args") or {}))
            ceremony, options = await devices.signing_options(wanted)
        except (ValueError, DeviceError) as exc:  # DeviceError is a ValueError too
            return _refused(str(exc), 422)
        return _ok({"ceremony": ceremony, "options": json.loads(options)})

    async def keep_change(request: Request, _device: str) -> Response:
        if keeper is None:
            return _refused("not available here", 404)
        what = str(request.path_params["what"])
        sent = await body(request)
        args = dict(sent.get("args") or {})
        try:
            wanted = keeper.purpose(what, args)
        except ValueError as exc:
            return _refused(str(exc), 422)
        try:
            signer = await devices.verify(str(sent["ceremony"]), wanted, dict(sent["credential"]))
        except DeviceError as exc:
            log.warning("mobile.keep_refused", what=what, error=str(exc))
            return _refused(str(exc), 403)
        try:
            done = await keeper.keep(what, args, signer)
        except LookupError as exc:
            return _refused(str(exc), 404)
        except ValueError as exc:
            return _refused(str(exc), 422)
        log.info("mobile.kept", what=what, device=signer)  # never the arguments: a secret
        return _ok(done)

    async def subscribe(request: Request, device: str) -> Response:
        if vapid is None:
            return _refused("notifications are off", 409)
        sent = await body(request)
        keys = cast(dict[str, Any], sent["keys"])
        await subscriber.subscribe(
            device, str(sent["endpoint"]), str(keys["p256dh"]), str(keys["auth"])
        )
        return _ok({})

    return [
        Route(f"{PREFIX}/pair/options", gate.public(pair_options), methods=["POST"]),
        Route(f"{PREFIX}/pair", gate.public(pair), methods=["POST"]),
        Route(f"{PREFIX}/login/options", gate.public(login_options), methods=["POST"]),
        Route(f"{PREFIX}/login", gate.public(login), methods=["POST"]),
        Route(f"{PREFIX}/logout", gate.guarded(logout), methods=["POST"]),
        Route(f"{PREFIX}/me", gate.guarded(me)),
        Route(f"{PREFIX}/approvals", gate.guarded(approvals)),
        Route(
            f"{PREFIX}/approvals/{{approval}}/options",
            gate.guarded(approval_options),
            methods=["POST"],
        ),
        Route(
            f"{PREFIX}/approvals/{{approval}}/refuse",
            gate.guarded(approval_refuse),
            methods=["POST"],
        ),
        Route(f"{PREFIX}/approvals/{{approval}}", gate.guarded(approval_answer), methods=["POST"]),
        Route(f"{PREFIX}/push/subscribe", gate.guarded(subscribe), methods=["POST"]),
        Route(f"{PREFIX}/settings/options", gate.guarded(settings_options), methods=["POST"]),
        Route(f"{PREFIX}/settings", gate.guarded(settings_write), methods=["POST"]),
        Route(f"{PREFIX}/devices", gate.guarded(phones)),
        Route(
            f"{PREFIX}/devices/{{how}}/{{name}}/options",
            gate.guarded(phone_options),
            methods=["POST"],
        ),
        Route(f"{PREFIX}/devices/{{how}}/{{name}}", gate.guarded(phone_change), methods=["POST"]),
        Route(f"{PREFIX}/logins", gate.guarded(vault_list)),
        Route(f"{PREFIX}/logins", gate.guarded(vault_save), methods=["POST"]),
        Route(f"{PREFIX}/logins/{{name}}/remove", gate.guarded(vault_remove), methods=["POST"]),
        Route(f"{PREFIX}/logins/import", gate.guarded(vault_import), methods=["POST"]),
        Route(f"{PREFIX}/keep", gate.guarded(keep_list)),
        Route(f"{PREFIX}/keep/{{what}}/options", gate.guarded(keep_options), methods=["POST"]),
        Route(f"{PREFIX}/keep/{{what}}", gate.guarded(keep_change), methods=["POST"]),
        Route(f"{PREFIX}/", static),
        Route(f"{PREFIX}/{{name}}", static),
    ]


# -- the conversation and the controls, under /app/api/ -----------------------------------------


def core_routes(
    channel: MobileChannel,
    gate: Gate,
    control: Control | None = None,
    notebook: Notebook | None = None,
) -> list[BaseRoute]:
    async def messages(request: Request, _device: str) -> Response:
        """The latest messages after `after`, or the ones just before `before` (scrolling
        back), `limit` at most; `more` says whether older ones remain."""
        after = int(request.query_params.get("after", "0"))
        before = int(request.query_params.get("before", "0")) or None
        limit = min(int(request.query_params.get("limit", "100")), 500)
        await channel.sync()
        state = channel.state
        said = [s for s in state.said if s.seq > after and (before is None or s.seq < before)]
        shown = said[-limit:]
        return _ok({"messages": [state.public(s) for s in shown], "more": len(said) > len(shown)})

    async def send(request: Request, device: str) -> Response:
        sent = await body(request)
        text = str(sent.get("text") or "").strip()
        files: list[tuple[str, str, bytes]] = []
        for item in cast(list[dict[str, Any]], sent.get("files") or []):
            try:
                data = base64.b64decode(str(item["data"]), validate=True)
            except binascii.Error as exc:
                raise ValueError(f"not base64: {exc}") from exc
            if len(data) > MAX_FILE_BYTES:
                return _refused(f"{item.get('name')}: too large (25 MB at most)", 413)
            files.append((str(item.get("name") or "fichier"), str(item.get("type") or ""), data))
        if not text and not files:
            return _refused("nothing to send")
        said = await channel.receive(device, text, files)
        return _ok({"message": said.public()})

    async def stream(_request: Request, _device: str) -> Response:
        async def events() -> AsyncGenerator[str]:
            async with channel.hub.listen() as queue:
                ready = {"type": "ready", "typing": channel.typing, "work": channel.work}
                yield f"data: {json.dumps(ready, ensure_ascii=False)}\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
                    except TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={**API_HEADERS, "x-accel-buffering": "no"},
        )

    async def file(request: Request, _device: str) -> Response:
        await channel.sync()
        said = channel.state.find(str(request.path_params["ref"]))
        index = int(request.path_params["index"])
        if said is None or not 0 <= index < len(said.files):
            return _refused("no such file", 404)
        kept = said.files[index]
        path = channel.path(kept).resolve()
        if not path.is_relative_to(channel.folder.resolve()) or not path.is_file():
            return _refused("no such file", 404)
        return FileResponse(
            path, media_type=kept.content_type or None, filename=kept.name, headers=API_HEADERS
        )

    async def stop(_request: Request, _device: str) -> Response:
        return _ok({"stopping": channel.stop()})

    async def react(request: Request, device: str) -> Response:
        sent = await body(request)
        done = await channel.react(device, str(sent["ref"]), str(sent["emoji"])[:8])
        return _ok({}) if done else _refused("no such message of Orion's", 404)

    def controls(handler: Callable[[Request, Control], Awaitable[object]]) -> Guarded:
        async def run(request: Request, _device: str) -> Response:
            if control is None:
                return _refused("the controls are not available here", 404)
            try:
                return _ok(await handler(request, control))
            except ControlError as exc:
                return _refused(str(exc), 404)

        return run

    async def overview(_request: Request, c: Control) -> object:
        return await c.overview()

    async def pause(request: Request, c: Control) -> object:
        minutes = (await body(request)).get("minutes")
        return {"paused_until": await c.pause(int(minutes) if minutes is not None else None)}

    async def change(request: Request, c: Control) -> object:
        await c.change(await body(request))
        return await c.overview()

    async def memory(request: Request, c: Control) -> object:
        query = request.query_params.get("q", "")
        facts = await c.facts(query, request.query_params.get("past") == "1")
        searching = bool(query.strip())
        return {
            "facts": facts,
            "portrait": None if searching else await c.portrait(),
            "situations": [] if searching else await c.situations(),
        }

    async def forget(request: Request, c: Control) -> object:
        await c.forget(str(request.path_params["id"]))
        return {}

    async def commitments(_request: Request, c: Control) -> object:
        return {"commitments": await c.commitments()}

    async def close(request: Request, c: Control) -> object:
        how = str(request.path_params["how"])
        if how not in ("done", "cancel"):
            raise ControlError("done or cancel")
        await c.close(str(request.path_params["id"]), how == "done")
        return {}

    async def rules(_request: Request, c: Control) -> object:
        return {"rules": await c.rules()}

    async def retire(request: Request, c: Control) -> object:
        await c.retire(str(request.path_params["id"]))
        return {}

    async def autonomy(_request: Request, c: Control) -> object:
        return {"actions": await c.autonomy_state()}

    async def take_back(request: Request, c: Control) -> object:
        await c.take_back(str(request.path_params["action"]))
        return {}

    async def activity(_request: Request, c: Control) -> object:
        return {"activity": await c.activity()}

    async def upcoming(_request: Request, c: Control) -> object:
        return {"planned": await c.upcoming()}

    async def for_you(_request: Request, c: Control) -> object:
        return await c.for_you()

    async def stop_task(request: Request, c: Control) -> object:
        await c.stop_task(str(request.path_params["id"]))
        return {}

    async def drop_wait(request: Request, c: Control) -> object:
        await c.drop_wait(str(request.path_params["id"]))
        return {}

    async def server(_request: Request, c: Control) -> object:
        return await c.server()

    async def missions(_request: Request, c: Control) -> object:
        return {"missions": await c.mission_list()}

    async def mission_now(request: Request, c: Control) -> object:
        return {"left": await c.mission_now(str(request.path_params["id"]))}

    async def mission_pace(request: Request, c: Control) -> object:
        hours = int((await body(request))["every_hours"])
        await c.mission_pace(str(request.path_params["id"]), hours)
        return {}

    async def mission_reject(request: Request, c: Control) -> object:
        sent = await body(request)
        await c.mission_reject(
            str(request.path_params["id"]), str(sent["key"]), str(sent.get("why") or "")
        )
        return {}

    async def mission_close(request: Request, c: Control) -> object:
        await c.mission_close(str(request.path_params["id"]))
        return {}

    async def machines(_request: Request, c: Control) -> object:
        return {"machines": await c.machines()}

    async def machine_revoke(request: Request, c: Control) -> object:
        await c.machine_revoke(str(request.path_params["name"]))
        return {}

    # -- what used to need the server's shell (D154) -------------------------------------------

    def kept(c: Control) -> Upkeep:
        if c.upkeep is None:
            raise ControlError("not available here")
        return c.upkeep

    async def missed(_request: Request, c: Control) -> object:
        return {"misses": await kept(c).misses()}

    async def week(_request: Request, c: Control) -> object:
        return await kept(c).week()

    async def phone_token(_request: Request, c: Control) -> object:
        return await kept(c).phone_token()

    async def chatgpt(_request: Request, c: Control) -> object:
        return kept(c).chatgpt()

    async def chatgpt_start(_request: Request, c: Control) -> object:
        return {"url": kept(c).chatgpt_start()}

    async def chatgpt_finish(request: Request, c: Control) -> object:
        return await kept(c).chatgpt_finish(str((await body(request))["address"]))

    async def chatgpt_logout(_request: Request, c: Control) -> object:
        return {"signed_out": kept(c).chatgpt_logout()}

    async def library_import(request: Request, c: Control) -> object:
        sent = cast(list[dict[str, Any]], (await body(request)).get("notes") or [])
        if not 1 <= len(sent) <= 200:
            raise ControlError("one to two hundred notes")
        return await kept(c).library_import([(str(n["name"])[:200], str(n["text"])) for n in sent])

    async def machine_options(request: Request, c: Control) -> object:
        ceremony, options = await kept(c).machine_options(str(request.path_params["name"]))
        return {"ceremony": ceremony, "options": options}

    async def machine_pair(request: Request, c: Control) -> object:
        sent = await body(request)
        return await kept(c).machine_pair(
            str(request.path_params["name"]), str(sent["ceremony"]), dict(sent["credential"])
        )

    def shelf(handler: Callable[[Request, Notebook], Awaitable[Response | object]]) -> Guarded:
        async def run(request: Request, _device: str) -> Response:
            if notebook is None:
                return _refused("the notebook is not available here", 404)
            try:
                done = await handler(request, notebook)
            except ControlError as exc:
                return _refused(str(exc), 404)
            return done if isinstance(done, Response) else _ok(done)

        return run

    async def papers(request: Request, n: Notebook) -> object:
        return {"papers": await n.paper_list(request.query_params.get("q", ""))}

    async def paper(request: Request, n: Notebook) -> Response:
        path, content_type, name = await n.paper_file(str(request.path_params["id"]))
        return FileResponse(
            path,
            media_type=content_type or None,
            filename=name,
            content_disposition_type="inline",
            headers=API_HEADERS,
        )

    async def people(_request: Request, n: Notebook) -> object:
        return {"people": await n.people()}

    async def saw(request: Request, n: Notebook) -> object:
        await n.saw(str((await body(request))["name"]))
        return {}

    async def pages(_request: Request, n: Notebook) -> object:
        return {"pages": await n.pages()}

    async def agenda(_request: Request, n: Notebook) -> object:
        return {"events": n.agenda()}

    async def mails(_request: Request, n: Notebook) -> object:
        return {"mails": n.mails()}

    async def nights(_request: Request, n: Notebook) -> object:
        return await n.nights()

    async def handovers(_request: Request, n: Notebook) -> object:
        return {"handovers": n.handovers()}

    async def browser_shot(request: Request, n: Notebook) -> Response:
        picture = await n.shot(str(request.path_params["token"]))
        return Response(picture, media_type="image/png", headers=API_HEADERS)

    async def browser_act(request: Request, n: Notebook) -> object:
        # What he types goes into the page only: never logged, even on a bad request.
        return await n.act(str(request.path_params["token"]), await body(request))

    async def logins_asked(_request: Request, n: Notebook) -> object:
        return {"asked": n.logins_asked()}

    async def login_answer(request: Request, n: Notebook) -> object:
        sent = await body(request)
        return await n.login_answer(
            str(request.path_params["token"]), str(sent["answer"]), str(sent.get("name") or "")
        )

    def api(path: str, handler: Guarded, post: bool = False) -> Route:
        return Route(
            f"{PREFIX}/api/{path}", gate.guarded(handler), methods=["POST"] if post else ["GET"]
        )

    return [
        api("messages", messages),
        api("send", send, post=True),
        api("stream", stream),
        api("files/{ref}/{index:int}", file),
        api("react", react, post=True),
        api("stop", stop, post=True),
        api("control", controls(overview)),
        api("control/pause", controls(pause), post=True),
        api("control/settings", controls(change), post=True),
        api("memory", controls(memory)),
        api("memory/{id}/forget", controls(forget), post=True),
        api("commitments", controls(commitments)),
        api("commitments/{id}/{how}", controls(close), post=True),
        api("rules", controls(rules)),
        api("rules/{id}/retire", controls(retire), post=True),
        api("autonomy", controls(autonomy)),
        api("autonomy/{action}/revoke", controls(take_back), post=True),
        api("activity", controls(activity)),
        api("planned", controls(upcoming)),
        api("for-you", controls(for_you)),
        api("tasks/{id}/stop", controls(stop_task), post=True),
        api("waits/{id}/drop", controls(drop_wait), post=True),
        api("server", controls(server)),
        api("missions", controls(missions)),
        api("missions/{id}/now", controls(mission_now), post=True),
        api("missions/{id}/pace", controls(mission_pace), post=True),
        api("missions/{id}/reject", controls(mission_reject), post=True),
        api("missions/{id}/close", controls(mission_close), post=True),
        api("machines", controls(machines)),
        api("machines/{name}/revoke", controls(machine_revoke), post=True),
        api("machines/{name}/pair/options", controls(machine_options), post=True),
        api("machines/{name}/pair", controls(machine_pair), post=True),
        api("misses", controls(missed)),
        api("week", controls(week)),
        api("library/import", controls(library_import), post=True),
        api("phone/token", controls(phone_token), post=True),
        api("chatgpt", controls(chatgpt)),
        api("chatgpt/start", controls(chatgpt_start), post=True),
        api("chatgpt/finish", controls(chatgpt_finish), post=True),
        api("chatgpt/logout", controls(chatgpt_logout), post=True),
        api("papers", shelf(papers)),
        api("papers/{id}/file", shelf(paper)),
        api("people", shelf(people)),
        api("people/saw", shelf(saw), post=True),
        api("chronicle", shelf(pages)),
        api("agenda", shelf(agenda)),
        api("mails", shelf(mails)),
        api("nights", shelf(nights)),
        api("browser", shelf(handovers)),
        api("browser/{token}/shot", shelf(browser_shot)),
        api("browser/{token}/act", shelf(browser_act), post=True),
        api("logins/asked", shelf(logins_asked)),
        api("logins/asked/{token}", shelf(login_answer), post=True),
    ]


def mobile_app(
    channel: MobileChannel,
    vapid: Vapid | None,
    secure_cookie: bool,
    session_days: int,
    control: Control | None = None,
    notebook: Notebook | None = None,
    settings: Settings | None = None,
    logins: Callable[[], Logins | None] = lambda: None,
) -> Starlette:
    """The whole app, in one process: the channel is its own desk, its devices its own gate."""
    devices = channel.devices
    if devices is None:
        raise ValueError("one process: the channel keeps the devices itself")
    gate = Gate(devices.origin, devices.session_device)
    return Starlette(
        routes=[
            *core_routes(channel, gate, control, notebook),
            *edge_routes(
                devices, channel, channel, vapid, secure_cookie, session_days, settings, logins
            ),
        ]
    )


def core_app(
    channel: MobileChannel,
    gate: Gate,
    control: Control | None = None,
    notebook: Notebook | None = None,
) -> Starlette:
    """The conversation alone (run split): sessions are checked with the guardian."""
    return Starlette(routes=core_routes(channel, gate, control, notebook))


def edge_app(
    devices: Devices,
    desk: Desk,
    subscriber: Subscriber,
    vapid: Vapid | None,
    secure_cookie: bool,
    session_days: int,
    settings: Settings | None = None,
    logins: Callable[[], Logins | None] = lambda: None,
    keeper: Keeper | None = None,
) -> Starlette:
    """The edge alone (run split, served by the guardian)."""
    return Starlette(
        routes=edge_routes(
            devices, desk, subscriber, vapid, secure_cookie, session_days, settings, logins, keeper
        )
    )
