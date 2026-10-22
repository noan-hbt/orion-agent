"""Signing in from the owner's phone: a browser on the server, driven from Telegram (D112).

A session (D49) used to be made with `orion login` on a computer with a screen, so a second
install of Orion. Now Orion asks (`computer_login_ask`), and the guardian sends the owner a
Telegram button that opens its own Mini App: the picture of a browser the guardian runs, posing
as a phone. He taps, types, signs in as he would on his phone, SMS code and captcha included,
then « J'ai fini »: the guardian keeps the session (cookies) under the name Orion gave, as
`orion sessions add` would. The core never sits in front: what he types goes to the guardian,
over the guardian's own listener, and nothing of it is recorded.

Every action is checked against Telegram's signed launch data (the owner, from this bot). The
picture is served only once he started, by a one-time token; the browser closes when he is
done, gives up, or leaves it idle.
"""

from __future__ import annotations

import asyncio
import html
import json
import secrets as tokens
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import structlog
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from orion.channels.telegram import TelegramError, check_init_data
from orion.computer.browser import BrowserError, Session
from orion.guardian.logins import NAME, site_of
from orion.guardian.sessions import SessionError, add_session

if TYPE_CHECKING:
    from orion.guardian.service import GuardianService
    from orion.guardian.telegram import TelegramGate
    from orion.guardian.vault import Vault

log = structlog.get_logger(__name__)

ASK_SECONDS = 15 * 60.0
"""To open the button."""
IDLE_SECONDS = 10 * 60.0
"""Once open: the browser closes after this long without a touch."""
KEYS = ("Enter", "Backspace", "Tab", "Escape")


@dataclass
class Remote:
    name: str
    url: str
    site: str
    why: str
    until: float
    browser: Session | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RemoteLogin:
    def __init__(
        self,
        service: GuardianService,
        gate: TelegramGate,
        base: str,
        browsers: Callable[[], Session] | None = None,
    ) -> None:
        self.service = service
        self.gate = gate
        self.base = base.rstrip("/")
        self.browsers = browsers or (lambda: Session(read_only=False))
        self.remotes: dict[str, Remote] = {}
        self.now = time.time

    def install(self, vault: Vault) -> None:
        self.service.routes.append(Route("/v1/sessions/ask", self._ask, methods=["POST"]))
        vault.public_routes += [
            Route("/secrets/browse/", self._page, methods=["GET"]),
            Route("/secrets/browse/act", self._act, methods=["POST"]),
            Route("/secrets/browse/shot", self._shot, methods=["GET"]),
        ]

    async def _ask(self, request: Request) -> Response:
        body = cast(dict[str, Any], json.loads(await request.body() or b"{}"))
        name, url = str(body.get("name", "")).strip(), str(body.get("url", "")).strip()
        why = str(body.get("why", "")).strip()[:300]
        if "://" not in url:
            url = f"https://{url}"
        site = site_of(url)
        if not NAME.match(name) or not site or not url.startswith(("https://", "http://")):
            return await self.service.deny(
                f"session {name}", "a lowercase name (doctolib) and the sign-in page's address"
            )
        await self._expire()
        token = tokens.token_urlsafe(24)
        self.remotes[token] = Remote(name, url, site, why, self.now() + ASK_SECONDS)
        markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "Me connecter",
                        "web_app": {"url": f"{self.base}/secrets/browse/?t={token}"},
                    }
                ]
            ]
        }
        text = (
            f"Connecte-toi à ton compte {site}" + (f" ({why})" if why else "") + " : touche le "
            "bouton. Tu verras un navigateur qui tourne sur ton serveur, chez mon gardien : "
            "connecte-toi comme sur ton téléphone, puis « J'ai fini ». Je garderai la session, "
            "jamais ce que tu tapes. Valable 15 minutes."
        )
        try:
            await self.gate.client.call(
                "sendMessage", chat_id=self.gate.owner_id, text=text, reply_markup=markup
            )
        except TelegramError as exc:
            return JSONResponse({"error": f"guardian: Telegram unreachable: {exc}"}, 502)
        return JSONResponse({"asked": name})

    async def _page(self, request: Request) -> Response:
        from orion.guardian.vault import EXPIRED, PAGE

        token = request.query_params.get("t", "")
        remote = self.remotes.get(token)
        if remote is None or remote.until <= self.now():
            return HTMLResponse(PAGE.format(body=EXPIRED), status_code=410)
        body = _BROWSER.format(site=html.escape(remote.site), token=html.escape(token, quote=True))
        return HTMLResponse(PAGE.format(body=body), headers={"cache-control": "no-store"})

    async def _act(self, request: Request) -> Response:
        try:
            body = cast(dict[str, Any], json.loads(await request.body() or b"{}"))
        except json.JSONDecodeError:
            return JSONResponse({"error": "malformed"}, 400)
        user = check_init_data(str(body.get("init", "")), self.gate.client.token, self.now())
        if not self.gate.owner_id or user != self.gate.owner_id:
            await self.service.deny("remote browser", "not opened by the owner, from Telegram")
            return JSONResponse({"error": "Ouvre-le depuis Telegram."}, 403)
        await self._expire()
        token = str(body.get("token", ""))
        remote = self.remotes.get(token)
        if remote is None:
            return JSONResponse({"error": "Ce lien a expiré : redemande-le à Orion."}, 410)
        do = str(body.get("do", ""))
        async with remote.lock:
            try:
                said = await self._do(token, remote, do, body)
            except BrowserError as exc:
                return JSONResponse({"error": f"Pas pu : {exc}"}, 409)
        if said is not None:
            return JSONResponse({"done": said})
        remote.until = self.now() + IDLE_SECONDS
        page = remote.browser.page if remote.browser is not None else None
        return JSONResponse({"url": page.url if page is not None else ""})

    async def _do(self, token: str, remote: Remote, do: str, body: dict[str, Any]) -> str | None:
        """Does one action; returns what to tell the owner when it is over."""
        if do == "start":
            if remote.browser is None:
                width = min(max(int(body.get("width") or 390), 320), 480)
                height = min(max(int(body.get("height") or 640), 480), 1000)
                browser = self.browsers()
                browser.phone = {"width": width, "height": height}
                await browser.__aenter__()
                remote.browser = browser
                log.info("guardian.remote_started", name=remote.name, site=remote.site)
                await browser.goto(remote.url)
            return None
        browser = remote.browser
        if browser is None:
            raise BrowserError("the browser is not open")
        if do == "tap":
            await browser.tap(float(body.get("x", 0)), float(body.get("y", 0)))
            await browser.settle()
        elif do == "type":
            await browser.keys(text=str(body.get("text", ""))[:500])
        elif do == "key" and str(body.get("key")) in KEYS:
            await browser.keys(key=str(body["key"]))
            await browser.settle()
        elif do == "scroll":
            await browser.wheel(max(min(int(body.get("dy", 0)), 2000), -2000))
        elif do == "back":
            await browser.back()
        elif do == "done":
            state = await browser.saved_state()
            await self._close(token)
            try:
                await add_session(
                    self.service.config, self.service.ledger, remote.name, state, [remote.site]
                )
            except SessionError as exc:
                raise BrowserError(str(exc)) from exc
            text = (
                f"C'est gardé : ta session « {remote.name} » ({remote.site}). Je peux maintenant "
                "agir sur ton compte, avec ton accord à chaque fois."
            )
            await self.gate.tell(text)
            return "Session gardée. Tu peux fermer."
        elif do == "cancel":
            await self._close(token)
            return "Annulé : rien n'est gardé."
        else:
            raise BrowserError(f"unknown action {do!r}")
        return None

    async def _shot(self, request: Request) -> Response:
        remote = self.remotes.get(request.query_params.get("t", ""))
        if remote is None or remote.browser is None or remote.until <= self.now():
            return Response(status_code=404)
        async with remote.lock:
            try:
                picture = await remote.browser.screenshot(full=False)
            except BrowserError as exc:
                return Response(str(exc), status_code=409)
        return Response(picture, media_type="image/png", headers={"cache-control": "no-store"})

    async def _close(self, token: str) -> None:
        remote = self.remotes.pop(token, None)
        if remote is not None and remote.browser is not None:
            await remote.browser.__aexit__(None, None, None)

    async def _expire(self) -> None:
        for token in [t for t, r in self.remotes.items() if r.until <= self.now()]:
            await self._close(token)

    async def aclose(self) -> None:
        for token in list(self.remotes):
            await self._close(token)


_BROWSER = """<style>
#s{{width:100%;display:block;border:1px solid #8886;border-radius:8px;touch-action:none}}
.row{{display:flex;gap:6px;margin:8px 0}} .row button,.row input{{margin:0;flex:1;padding:10px}}
.row input{{flex:3}} h3{{margin:0 0 6px}}
</style>
<h3>Connexion à {site}</h3>
<p id="r" class="note">Ouverture du navigateur…</p>
<img id="s" alt="">
<div class="row"><input id="t" type="password" autocomplete="off" placeholder="Texte à taper">
<button id="eye" type="button">👁</button><button id="go" type="button">Taper</button></div>
<div class="row"><button data-k="Enter">⏎</button><button data-k="Backspace">⌫</button>
<button data-y="-500">▲</button><button data-y="500">▼</button><button id="back">←</button></div>
<div class="row"><button id="done">J'ai fini</button><button id="no">Annuler</button></div>
<p class="note">Touche l'image pour cliquer (un champ, un bouton), puis tape le texte dessous.
Ce navigateur tourne sur ton serveur, chez le gardien d'Orion : il garde la session, jamais ce
que tu tapes.</p>
<script>
const tg = window.Telegram && Telegram.WebApp; if (tg) {{ tg.ready(); tg.expand(); }}
const img = document.getElementById("s"), r = document.getElementById("r");
const field = document.getElementById("t");
let size = {{w: 390, h: 640}}, n = 0, busy = false, over = false;
const shot = () => {{ if (!over) img.src = "shot?t={token}&n=" + (n++); }};
async function act(body) {{
  if (busy || over) return;
  busy = true;
  try {{
    const sent = Object.assign({{init: tg ? tg.initData : "", token: "{token}"}}, body);
    const res = await fetch("act", {{method: "POST",
      headers: {{"content-type": "application/json"}}, body: JSON.stringify(sent)}});
    const data = await res.json();
    if (!res.ok) r.textContent = data.error || "Refusé.";
    else if (data.done) {{
      over = true; r.textContent = data.done; setTimeout(() => tg && tg.close(), 1500);
    }} else r.textContent = data.url || "";
  }} catch (e) {{ r.textContent = "Erreur : " + e; }}
  busy = false;
  setTimeout(shot, 400);
}}
img.addEventListener("click", (e) => act({{do: "tap",
  x: e.offsetX * size.w / img.clientWidth, y: e.offsetY * size.h / img.clientHeight}}));
const typed = () => {{
  if (field.value) act({{do: "type", text: field.value}});
  field.value = "";
}};
document.getElementById("go").onclick = typed;
field.addEventListener("keydown", (e) => {{
  if (e.key === "Enter") {{ e.preventDefault(); typed(); }}
}});
document.getElementById("eye").onclick = () => {{
  field.type = field.type === "password" ? "text" : "password";
}};
document.querySelectorAll("[data-k]").forEach(
  b => b.onclick = () => act({{do: "key", key: b.dataset.k}}));
document.querySelectorAll("[data-y]").forEach(
  b => b.onclick = () => act({{do: "scroll", dy: +b.dataset.y}}));
document.getElementById("back").onclick = () => act({{do: "back"}});
document.getElementById("done").onclick = () => act({{do: "done"}});
document.getElementById("no").onclick = () => act({{do: "cancel"}});
size = {{w: Math.min(Math.max(Math.round(innerWidth - 40), 320), 480),
        h: Math.min(Math.max(Math.round(innerHeight * 0.6), 480), 1000)}};
act({{do: "start", width: size.w, height: size.h}});
setInterval(() => {{ if (!busy && !document.hidden) shot(); }}, 2500);
</script>"""
