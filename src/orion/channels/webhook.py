"""HTTP endpoint for Inkbox webhooks.

Fail closed: without a signing key every delivery is refused (503), a bad or stale signature
gets 401, a malformed body 400. A delivery is acknowledged only after `IMessageChannel.receive`
has journaled it; the turn itself runs later, off the request.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from orion.channels.inkbox import InkboxError, WebhookEvent, parse_webhook, verify_signature

log = structlog.get_logger(__name__)


def webhook_app(
    path: str,
    signing_key: str | None,
    handle: Callable[[WebhookEvent], Awaitable[None]],
    clock: Callable[[], float] | None = None,
) -> Starlette:
    async def endpoint(request: Request) -> Response:
        if not signing_key:
            log.error("webhook.no_signing_key")
            return JSONResponse({"error": "signing key not configured"}, status_code=503)
        body = await request.body()
        valid = verify_signature(
            signing_key,
            request.headers.get("x-inkbox-request-id", ""),
            request.headers.get("x-inkbox-timestamp", ""),
            request.headers.get("x-inkbox-signature", ""),
            body,
            now=clock() if clock else None,
        )
        if not valid:
            log.warning("webhook.bad_signature", client=request.client)
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        try:
            event = parse_webhook(body)
        except InkboxError as exc:
            log.warning("webhook.malformed", error=str(exc))
            return JSONResponse({"error": "malformed payload"}, status_code=400)
        await handle(event)
        return JSONResponse({"ok": True})

    async def health(_request: Request) -> Response:
        return JSONResponse({"ok": True})

    return Starlette(
        routes=[Route(path, endpoint, methods=["POST"]), Route("/health", health, methods=["GET"])]
    )
