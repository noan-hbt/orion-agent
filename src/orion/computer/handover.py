"""The owner takes Orion's browser in hand, from Telegram (D116).

Like the agent browsers of ChatGPT: when a page needs him (signing in, a code sent by SMS, a
captcha), Orion hands him the very tab it is on. A Telegram button opens a Mini App served by
the console: the picture of the tab; he taps it to click, types under it, then « J'ai fini ».
What he types goes into the page, never into the conversation or the journal. The browser
keeps its cookies on disk, so the site stays signed in for Orion afterwards; Orion is told he
is done and resumes where he left the page.

Every action is checked against Telegram's signed launch data (the owner, from this bot); the
picture is served only to the one-time token of a hand-over under way.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, cast

import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from orion.computer.browser import BrowserError, Session
from orion.computer.direct import BrowserDesk, Refused

log = structlog.get_logger(__name__)

KEYS = ("Enter", "Backspace", "Tab", "Escape")
OwnerCheck = Callable[[str], Awaitable[bool]]
"""Telegram's signed launch data → whether it is the owner."""


def handover_app(desk: BrowserDesk, is_owner: OwnerCheck) -> Starlette:
    async def page(request: Request) -> Response:
        token = request.query_params.get("t", "")
        found = desk.handover(token)
        if found is None:
            return HTMLResponse(_PAGE.format(body=_EXPIRED), status_code=410)
        body = _BROWSER.format(why=_escape(found.why), token=_escape(token))
        return HTMLResponse(_PAGE.format(body=body), headers={"cache-control": "no-store"})

    async def act(request: Request) -> Response:
        try:
            body = cast(dict[str, Any], json.loads(await request.body() or b"{}"))
        except json.JSONDecodeError:
            return JSONResponse({"error": "malformed"}, 400)
        if not await is_owner(str(body.get("init", ""))):
            log.warning("browser.handover_refused")
            return JSONResponse({"error": "Ouvre-le depuis Telegram."}, 403)
        token = str(body.get("token", ""))
        found = desk.handover(token)
        if found is None:
            return JSONResponse({"error": "C'est terminé, ou le lien a expiré."}, 410)
        do = str(body.get("do", ""))
        if do in ("done", "cancel"):
            await desk.give_back(token, done=do == "done")
            said = "Merci, je reprends." if do == "done" else "D'accord, je laisse tomber."
            return JSONResponse({"done": said})
        try:
            size = await desk.on_tab(found.thread, lambda s: act_on(s, do, body))
            if do == "type":  # into a sign-in form: offered to keep once he is done (D120)
                desk.note_typed(token, await desk.on_tab(found.thread, focused))
        except (BrowserError, Refused) as exc:
            return JSONResponse({"error": f"Pas pu : {exc}"}, 409)
        tab = desk.tab(found.thread)
        return JSONResponse({"url": tab.page.url if tab else "", **size})

    async def shot(request: Request) -> Response:
        found = desk.handover(request.query_params.get("t", ""))
        if found is None:
            return Response(status_code=404)
        try:
            picture = await desk.on_tab(found.thread, lambda s: s.screenshot(full=False))
        except (BrowserError, Refused) as exc:
            return Response(str(exc), status_code=409)
        return Response(picture, media_type="image/png", headers={"cache-control": "no-store"})

    async def keep_page(request: Request) -> Response:
        token = request.query_params.get("t", "")
        found = desk.offer(token)
        if found is None:
            return HTMLResponse(_PAGE.format(body=_EXPIRED), status_code=410)
        fields = (
            f"<p>Identifiant : <b>{_escape(found.username) or '(aucun)'}</b><br>Mot de passe : "
            "celui que tu viens de taper.</p>"
            if found.password
            else '<input id="u" autocomplete="off" placeholder="Identifiant ou e-mail">'
            '<input id="p" type="password" autocomplete="off" placeholder="Mot de passe">'
        )
        body = _KEEP.format(site=_escape(found.site), fields=fields, token=_escape(token))
        return HTMLResponse(_PAGE.format(body=body), headers={"cache-control": "no-store"})

    async def keep(request: Request) -> Response:
        try:
            body = cast(dict[str, Any], json.loads(await request.body() or b"{}"))
        except json.JSONDecodeError:
            return JSONResponse({"error": "malformed"}, 400)
        if not await is_owner(str(body.get("init", ""))):
            log.warning("browser.keep_refused")
            return JSONResponse({"error": "Ouvre-le depuis Telegram."}, 403)
        token = str(body.get("token", ""))
        if not body.get("yes"):
            desk.offers.pop(token, None)
            return JSONResponse({"done": "D'accord, je ne le garde pas."})
        try:
            name = await desk.keep(
                token, str(body.get("username", ""))[:200], str(body.get("password", ""))[:500]
            )
        except Refused as exc:
            return JSONResponse({"error": f"Pas pu : {exc}"}, 409)
        return JSONResponse({"done": f"Gardé dans mon coffre, sous « {name} »."})

    return Starlette(
        routes=[
            Route("/", page, methods=["GET"]),
            Route("/act", act, methods=["POST"]),
            Route("/shot", shot, methods=["GET"]),
            Route("/keep", keep_page, methods=["GET"]),
            Route("/keep", keep, methods=["POST"]),
        ]
    )


async def focused(session: Session) -> dict[str, str] | None:
    return await session.focused_field()


async def act_on(session: Session, do: str, body: dict[str, Any]) -> dict[str, int]:
    """One of his actions on the tab; returns the tab's size, to map his taps onto it."""
    if do == "tap":
        await session.tap(float(body.get("x", 0)), float(body.get("y", 0)))
        await session.settle()
    elif do == "type":
        await session.keys(text=str(body.get("text", ""))[:500])
    elif do == "key" and str(body.get("key")) in KEYS:
        await session.keys(key=str(body["key"]))
        await session.settle()
    elif do == "scroll":
        await session.wheel(max(min(int(body.get("dy", 0)), 2000), -2000))
    elif do == "back":
        await session.back()
    elif do != "start":
        raise BrowserError(f"unknown action {do!r}")
    viewport = session.page.viewport_size if session.page is not None else None
    return {"width": viewport["width"], "height": viewport["height"]} if viewport else {}


def _escape(text: str) -> str:
    import html

    return html.escape(text, quote=True)


_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Orion — mon navigateur</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
body{{font:16px system-ui,sans-serif;margin:0;padding:12px;
background:var(--tg-theme-bg-color,#fff);color:var(--tg-theme-text-color,#111)}}
input,button{{font-size:16px;padding:10px;border:1px solid #8886;border-radius:8px;
background:transparent;color:inherit}}
button{{background:var(--tg-theme-button-color,#2a7ae2);color:var(--tg-theme-button-text-color,#fff);
border:0}}
.note{{opacity:.7;font-size:14px}}
</style></head><body>{body}</body></html>"""

_EXPIRED = "<p>C'est terminé, ou le lien a expiré. Redemande-le à Orion.</p>"

_BROWSER = """<style>
#s{{width:100%;display:block;border:1px solid #8886;border-radius:8px;touch-action:none}}
.row{{display:flex;gap:6px;margin:8px 0}} .row button,.row input{{margin:0;flex:1}}
.row input{{flex:3}} h3{{margin:0 0 6px}}
</style>
<h3>{why}</h3>
<p id="r" class="note">Ouverture…</p>
<img id="s" alt="">
<div class="row"><input id="t" type="password" autocomplete="off" placeholder="Texte à taper">
<button id="eye" type="button">👁</button><button id="go" type="button">Taper</button></div>
<div class="row"><button data-k="Enter">⏎</button><button data-k="Backspace">⌫</button>
<button data-y="-500">▲</button><button data-y="500">▼</button><button id="back">←</button></div>
<div class="row"><button id="done">J'ai fini</button><button id="no">Annuler</button></div>
<p class="note">Touche l'image pour cliquer (un champ, un bouton), puis tape le texte dessous.
C'est le navigateur d'Orion, sur ton serveur : ce que tu tapes va dans la page, pas dans votre
conversation.</p>
<script>
const tg = window.Telegram && Telegram.WebApp; if (tg) {{ tg.ready(); tg.expand(); }}
const img = document.getElementById("s"), r = document.getElementById("r");
const field = document.getElementById("t");
let size = {{width: 412, height: 860}}, n = 0, busy = false, over = false;
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
      over = true; r.textContent = data.done; setTimeout(() => tg && tg.close(), 1200);
    }} else {{
      r.textContent = data.url || "";
      if (data.width) size = {{width: data.width, height: data.height}};
    }}
  }} catch (e) {{ r.textContent = "Erreur : " + e; }}
  busy = false;
  setTimeout(shot, 400);
}}
img.addEventListener("click", (e) => act({{do: "tap",
  x: e.offsetX * size.width / img.clientWidth, y: e.offsetY * size.height / img.clientHeight}}));
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
act({{do: "start"}});
setInterval(() => {{ if (!busy && !document.hidden) shot(); }}, 2500);
</script>"""

_KEEP = """<h3>Garder ton identifiant {site}</h3>
<p class="note">Il va dans le coffre d'Orion, sur ton serveur. Orion ne le voit jamais : son
code le tape dans la page de connexion, sur ce site seulement.</p>
{fields}
<div style="display:flex;gap:6px;margin:8px 0">
<button id="yes" style="flex:1">Garder</button><button id="no" style="flex:1">Non merci</button>
</div>
<p id="r" class="note"></p>
<style>input{{display:block;width:100%;box-sizing:border-box;margin:6px 0}}</style>
<script>
const tg = window.Telegram && Telegram.WebApp; if (tg) {{ tg.ready(); tg.expand(); }}
const r = document.getElementById("r");
const value = (id) => {{ const e = document.getElementById(id); return e ? e.value : ""; }};
async function send(yes) {{
  const body = {{init: tg ? tg.initData : "", token: "{token}", yes,
                username: value("u"), password: value("p")}};
  const res = await fetch("keep", {{method: "POST",
    headers: {{"content-type": "application/json"}}, body: JSON.stringify(body)}});
  const data = await res.json();
  r.textContent = data.done || data.error || "";
  if (data.done) setTimeout(() => tg && tg.close(), 1500);
}}
document.getElementById("yes").onclick = () => send(true);
document.getElementById("no").onclick = () => send(false);
</script>"""
