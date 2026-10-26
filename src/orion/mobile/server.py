"""The app's HTTP side (D125), under `/app/`: its files, pairing, sessions, the conversation.

Everything but pairing and signing in needs a session (a cookie Orion signed, HttpOnly, Secure,
SameSite=Strict, for `/app/` only). A request that changes something must come from the app's
own origin. Uploads come as JSON (base64): no form parser, nothing else to trust.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from importlib import resources
from typing import Any, cast

import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from orion.mobile.channel import MAX_FILE_BYTES, MobileChannel
from orion.mobile.devices import DeviceError
from orion.mobile.push import Vapid

log = structlog.get_logger(__name__)

PREFIX = "/app"
COOKIE = "orion_app"
HEARTBEAT_SECONDS = 20.0
MAX_FAILURES = 10
"""Wrong pairing codes before pairing stops until a restart."""
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
}
API_HEADERS = {"cache-control": "no-store", "referrer-policy": "no-referrer"}

Handler = Callable[[Request], Awaitable[Response]]


def _refused(why: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": why}, status_code=status, headers=API_HEADERS)


def _ok(body: object) -> JSONResponse:
    return JSONResponse(body, headers=API_HEADERS)


def mobile_app(
    channel: MobileChannel,
    vapid: Vapid | None,
    secure_cookie: bool,
    session_days: int,
) -> Starlette:
    devices = channel.devices
    failures = [0]

    def same_origin(request: Request) -> bool:
        origin = request.headers.get("origin")
        return origin is None or origin == devices.origin

    async def body(request: Request) -> dict[str, Any]:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise ValueError("too large")
        parsed: object = json.loads(raw or b"{}")
        if not isinstance(parsed, dict):
            raise TypeError("not an object")
        return cast(dict[str, Any], parsed)

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

    def public(handler: Handler) -> Handler:
        async def check(request: Request) -> Response:
            if request.method == "POST" and not same_origin(request):
                return _refused("cross-site request refused", 403)
            try:
                return await handler(request)
            except (ValueError, KeyError, TypeError) as exc:
                log.info("mobile.bad_request", path=request.url.path, error=str(exc))
                return _refused("malformed request")

        return check

    def guarded(handler: Callable[[Request, str], Awaitable[Response]]) -> Handler:
        async def check(request: Request) -> Response:
            device = await devices.session_device(request.cookies.get(COOKIE))
            if device is None:
                return _refused("sign in", 401)
            if request.method == "POST" and not same_origin(request):
                return _refused("cross-site request refused", 403)
            try:
                return await handler(request, device)
            except (ValueError, KeyError, TypeError) as exc:
                log.info("mobile.bad_request", path=request.url.path, error=str(exc))
                return _refused("malformed request")

        return check

    # -- the app's own files ------------------------------------------------------------------

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

    # -- pairing, signing in ------------------------------------------------------------------

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

    # -- the conversation ---------------------------------------------------------------------

    async def me(_request: Request, device: str) -> Response:
        return _ok({"device": device, "push_key": vapid.public_key if vapid else None})

    async def messages(request: Request, _device: str) -> Response:
        after = int(request.query_params.get("after", "0"))
        limit = min(int(request.query_params.get("limit", "100")), 500)
        await channel.sync()
        said = [s for s in channel.state.said if s.seq > after]
        return _ok({"messages": [s.public() for s in said[-limit:]]})

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
                pending = [channel.public(a) for a in channel.asked.values()]
                yield f"data: {json.dumps({'type': 'ready', 'approvals': pending})}\n\n"
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

    async def react(request: Request, device: str) -> Response:
        sent = await body(request)
        done = await channel.react(device, str(sent["ref"]), str(sent["emoji"])[:8])
        return _ok({}) if done else _refused("no such message of Orion's", 404)

    # -- approvals ----------------------------------------------------------------------------

    async def approvals(_request: Request, _device: str) -> Response:
        return _ok({"approvals": [channel.public(a) for a in channel.asked.values()]})

    async def approval_options(request: Request, _device: str) -> Response:
        approval_id = str(request.path_params["approval"])
        answer = str((await body(request))["answer"])
        if answer not in ("yes", "no") or approval_id not in channel.asked:
            return _refused("no such question", 404)
        ceremony, options = await devices.signing_options(f"approval:{approval_id}:{answer}")
        return _ok({"ceremony": ceremony, "options": json.loads(options)})

    async def approval_answer(request: Request, _device: str) -> Response:
        approval_id = str(request.path_params["approval"])
        sent = await body(request)
        answer = str(sent["answer"])
        if answer not in ("yes", "no"):
            return _refused("yes or no")
        try:
            device = await channel.answer(
                approval_id, answer == "yes", str(sent["ceremony"]), dict(sent["credential"])
            )
        except LookupError:
            return _refused("no such question pending", 404)
        except DeviceError as exc:
            log.warning("mobile.answer_refused", approval=approval_id, error=str(exc))
            return _refused(str(exc), 403)
        log.info("mobile.answered", approval=approval_id, answer=answer, device=device)
        return _ok({})

    # -- notifications ------------------------------------------------------------------------

    async def subscribe(request: Request, device: str) -> Response:
        if vapid is None:
            return _refused("notifications are off", 409)
        sent = await body(request)
        keys = cast(dict[str, Any], sent["keys"])
        await channel.subscribe(
            device, str(sent["endpoint"]), str(keys["p256dh"]), str(keys["auth"])
        )
        return _ok({})

    return Starlette(
        routes=[
            Route(f"{PREFIX}/pair/options", public(pair_options), methods=["POST"]),
            Route(f"{PREFIX}/pair", public(pair), methods=["POST"]),
            Route(f"{PREFIX}/login/options", public(login_options), methods=["POST"]),
            Route(f"{PREFIX}/login", public(login), methods=["POST"]),
            Route(f"{PREFIX}/api/logout", guarded(logout), methods=["POST"]),
            Route(f"{PREFIX}/api/me", guarded(me)),
            Route(f"{PREFIX}/api/messages", guarded(messages)),
            Route(f"{PREFIX}/api/send", guarded(send), methods=["POST"]),
            Route(f"{PREFIX}/api/stream", guarded(stream)),
            Route(f"{PREFIX}/api/files/{{ref}}/{{index:int}}", guarded(file)),
            Route(f"{PREFIX}/api/react", guarded(react), methods=["POST"]),
            Route(f"{PREFIX}/api/approvals", guarded(approvals)),
            Route(
                f"{PREFIX}/api/approvals/{{approval}}/options",
                guarded(approval_options),
                methods=["POST"],
            ),
            Route(
                f"{PREFIX}/api/approvals/{{approval}}", guarded(approval_answer), methods=["POST"]
            ),
            Route(f"{PREFIX}/api/push/subscribe", guarded(subscribe), methods=["POST"]),
            Route(f"{PREFIX}/", static),
            Route(f"{PREFIX}/{{name}}", static),
        ]
    )
