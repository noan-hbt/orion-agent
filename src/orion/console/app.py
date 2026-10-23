"""The control page itself: plain server-rendered HTML, no script.

Everything the owner does here is journaled as the owner's own correction (trust OWNER), the
same events the tools write: forgetting a fact invalidates it, closing a commitment closes it,
retiring a rule retires it, taking autonomy back revokes it. Nothing here bypasses the journal.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from orion.console.auth import COOKIE, SESSION_DAYS, Gate
from orion.console.dashboard import dashboard
from orion.core.events import Event, Trust
from orion.core.hlc import Timestamp
from orion.core.journal import EventStore
from orion.guardian.autonomy import Autonomy, label
from orion.initiative.engine import InitiativeSettings, effective_settings
from orion.memory.model import CommitmentStatus
from orion.memory.provenance import Provenances
from orion.memory.service import MemoryService, Origin
from orion.modules.base import Planned
from orion.providers.spending import Spending

PREFIX = "/console"
ACTIVITY = {
    "initiative.delivered": "a écrit le premier",
    "initiative.suppressed": "s'est tu",
    "approval.decided": "a demandé ton accord",
    "mail.sent": "a envoyé un mail",
    "calendar.event.added": "a ajouté à l'agenda",
    "autonomy.granted": "autonomie accordée",
    "autonomy.revoked": "autonomie retirée",
    "provenance.contained": "source mise en quarantaine",
    "job.started": "a lancé une tâche",
    "job.finished": "tâche terminée",
    "web.searched": "a cherché sur le web",
}
STYLE = """
:root{--bg:#fafaf8;--fg:#1d1d1b;--muted:#6b6b66;--line:#e3e2dc;--accent:#2f5d8a;--warn:#9a3b1f}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--muted:#9a9a93;
--line:#2c2c2a;--accent:#8db4dc;--warn:#e08a6b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,sans-serif}main{max-width:860px;margin:0 auto;padding:16px}
nav{display:flex;flex-wrap:wrap;gap:12px;padding:12px 16px;border-bottom:1px solid var(--line)}
nav a{color:var(--accent);text-decoration:none}h1{font-size:20px}h2{font-size:16px;margin-top:28px}
table{width:100%;border-collapse:collapse}td,th{padding:6px 4px;border-bottom:1px solid var(--line);
vertical-align:top;text-align:left}th{color:var(--muted);font-weight:500}.muted{color:var(--muted)}
.warn{color:var(--warn)}button{font:inherit;padding:2px 8px;border:1px solid var(--line);
border-radius:6px;background:transparent;color:var(--fg);cursor:pointer}form{display:inline}
input[type=text]{font:inherit;padding:4px 6px;width:100%;max-width:480px;background:transparent;
color:var(--fg);border:1px solid var(--line);border-radius:6px}
"""


Plans = Callable[[datetime, datetime], Awaitable[list[Planned]]]


@dataclass
class Console:
    gate: Gate
    journal: EventStore
    memory: MemoryService
    autonomy: Autonomy | None
    provenances: Provenances
    timezone: str
    secure_cookie: bool = True
    planned: Plans | None = None
    """What Orion will do on its own (D78)."""
    spending: Spending | None = None
    """What it spent (D79)."""
    telegram_login: Callable[[str], Awaitable[int | None]] | None = None
    """Who opened the console from Telegram, from the Mini App's signed launch data (D91):
    checked with the bot's token, by the Telegram module or the guardian that holds it."""
    owner_telegram: int = 0
    initiative: InitiativeSettings | None = None
    """The engine's settings from the config file, for the dashboard's attention (D101)."""
    browser: ASGIApp | None = None
    """Orion's browser handed to the owner (D116), served under `/console/browser/`."""


def _page(title: str, body: str) -> HTMLResponse:
    nav = "".join(
        f'<a href="{PREFIX}/{path}">{name}</a>'
        for path, name in (
            ("", "Accueil"),
            ("dashboard", "Tableau de bord"),
            ("memory", "Mémoire"),
            ("commitments", "Engagements"),
            ("playbook", "Manuel"),
            ("autonomy", "Autonomie"),
            ("planned", "Prévu"),
            ("activity", "Activité"),
            ("sources", "Sources"),
        )
    )
    html = (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Orion — {escape(title)}</title><style>{STYLE}</style></head>"
        f"<body><nav>{nav}</nav><main><h1>{escape(title)}</h1>{body}</main></body></html>"
    )
    return HTMLResponse(
        html, headers={"cache-control": "no-store", "referrer-policy": "no-referrer"}
    )


def _button(action: str, text: str) -> str:
    return f'<form method="post" action="{PREFIX}/{action}"><button>{escape(text)}</button></form>'


def _when(event: Event, zone: str) -> str:
    from zoneinfo import ZoneInfo

    moment = datetime.fromtimestamp(Timestamp.decode(event.hlc).wall_ms / 1000, UTC)
    return f"{moment.astimezone(ZoneInfo(zone)):%d/%m %H:%M}"


def console_app(console: Console) -> Starlette:
    owner = Origin(actor="owner:console", source="console", trust=Trust.OWNER)

    def guarded(
        handler: Callable[[Request], Awaitable[Response]],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def check(request: Request) -> Response:
            if not console.gate.valid(request.cookies.get(COOKIE)):
                return _page("Accès", "<p>Demande-moi le lien de la console par message.</p>")
            if request.method == "POST":
                origin = request.headers.get("origin")
                if origin and urlsplit(origin).netloc != request.url.netloc:
                    return Response("cross-site request refused", status_code=403)
            return await handler(request)

        return check

    async def login(request: Request) -> Response:
        cookie = await console.gate.redeem(request.query_params.get("t", ""))
        if cookie is None:
            return _page("Lien expiré", "<p>Ce lien a déjà servi ou a expiré. Redemande-le.</p>")
        response = RedirectResponse(f"{PREFIX}/", status_code=303)
        response.set_cookie(
            COOKIE,
            cookie,
            max_age=SESSION_DAYS * 86400,
            httponly=True,
            secure=console.secure_cookie,
            samesite="strict",
            path=PREFIX,
        )
        return response

    async def from_telegram(_request: Request) -> Response:
        """The Mini App: Telegram's script hands over the signed launch data, posted back."""
        # Every way it can fail is said on the page: a script that stops shows nothing.
        script = (
            "const m=document.getElementById('m');"
            "const say=(t)=>{m.textContent=t;};"
            "try{"
            "const w=window.Telegram&&Telegram.WebApp;"
            "if(!w||!w.initData){say('Ouvre la console avec le bouton « Console » de ta "
            "conversation Telegram avec moi.');}"
            "else{w.ready();"
            "const late=setTimeout(()=>say('Le serveur ne répond pas. Réessaie dans un "
            "instant.'),15000);"
            f"fetch('{PREFIX}/telegram',{{method:'POST',body:w.initData,credentials:'same-origin'}})"
            ".then(r=>{clearTimeout(late);"
            f"if(r.ok)location.replace('{PREFIX}/');"
            "else if(r.status===403)say('Seul le propriétaire peut ouvrir la console.');"
            "else say('Le serveur a refusé ('+r.status+'). Réessaie dans un instant.');})"
            ".catch(e=>{clearTimeout(late);say('Pas de connexion au serveur : '+e);});}"
            "}catch(e){say('Erreur : '+e);}"
        )
        html = (
            '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<script src="https://telegram.org/js/telegram-web-app.js"></script>'
            f"<title>Orion</title><style>{STYLE}</style></head>"
            f'<body><main><p id="m">Un instant…</p></main><script>{script}</script></body></html>'
        )
        return HTMLResponse(html, headers={"cache-control": "no-store"})

    async def telegram_login(request: Request) -> Response:
        check = console.telegram_login
        if check is None or not console.owner_telegram:
            return Response("Telegram is not connected", status_code=404)
        user = await check((await request.body()).decode(errors="replace"))
        if user is None or user != console.owner_telegram:
            return Response("not the owner", status_code=403)
        response = Response("ok")
        response.set_cookie(
            COOKIE,
            await console.gate.telegram_session(user),
            max_age=SESSION_DAYS * 86400,
            httponly=True,
            secure=console.secure_cookie,
            samesite="strict",
            path=PREFIX,
        )
        return response

    async def home(_request: Request) -> Response:
        store = console.memory.store
        facts, commitments, rules = (
            await store.facts(),
            await store.commitments(),
            await store.rules(),
        )
        granted = sorted(console.autonomy.state.granted) if console.autonomy else []
        body = (
            f"<p>{len(facts)} souvenirs, {len(commitments)} engagements ouverts, "
            f"{len(rules)} règles dans le manuel.</p>"
            + (
                "<p>Ce que je fais sans demander : "
                + ", ".join(escape(label(a)) for a in granted)
                + "</p>"
                if granted
                else "<p>Je te demande avant chaque action.</p>"
            )
        )
        if console.spending is not None:
            await console.spending.sync()
            spent = console.spending.summary(console.spending.clock().date())
            body += (
                "<h2>Ce que j'ai coûté</h2><p>"
                + "<br>".join(escape(line) for line in spent.splitlines())
                + "</p>"
            )
        return _page("Ce que je sais et ce que je fais", body)

    async def board(_request: Request) -> Response:
        base = console.initiative or InitiativeSettings(timezone=console.timezone)
        settings = effective_settings(base, await console.memory.store.settings())
        last = await console.journal.last_seq()
        events = await console.journal.read(max(0, last - 20_000), 20_000)
        now = datetime.now(ZoneInfo(settings.timezone))
        return _page("Tableau de bord", dashboard(events, settings, now))

    async def planned(_request: Request) -> Response:
        if console.planned is None:
            return _page("Ce que je vais faire", "<p>Rien à montrer.</p>")
        zone = ZoneInfo(console.timezone)
        now = datetime.now(zone)
        rows = [
            f"<tr><td class=muted>{p.at.astimezone(zone):%a %d/%m %H:%M}</td>"
            f"<td>{escape(p.what)}</td>"
            f"<td class=muted>{escape(p.undo) if p.undo else ''}</td></tr>"
            for p in await console.planned(now, now + timedelta(days=14))
        ]
        body = (
            "<p>Ce que je ferai de moi-même ces deux prochaines semaines. Pour annuler, "
            "dis-le-moi ; la colonne de droite dit comment je le ferai.</p>"
            + ("<table>" + "".join(rows) + "</table>" if rows else "<p>Rien de prévu.</p>")
        )
        return _page("Ce que je vais faire", body)

    async def memory(request: Request) -> Response:
        past = request.query_params.get("past") == "1"
        rows: list[str] = []
        for f in await console.memory.store.facts(include_past=past):
            action = (
                _button(f"facts/{f.id}/forget", "Oublier")
                if f.current
                else f'<span class="muted">{escape(f.invalid_reason or "remplacé")}</span>'
            )
            trust = "" if f.trust >= Trust.SYSTEM else ' class="warn"'
            rows.append(
                f"<tr><td>{escape(f.statement)}</td><td class=muted>{escape(f.category)}</td>"
                f"<td{trust}>{escape(f.source)}<br><small>{f.trust.name}</small></td>"
                f"<td>{action}</td></tr>"
            )
        toggle = (
            f'<a href="{PREFIX}/memory">Seulement le présent</a>'
            if past
            else f'<a href="{PREFIX}/memory?past=1">Avec l\'historique</a>'
        )
        body = (
            f"<p>{toggle}. En orange : ce qui vient d'ailleurs que de toi.</p>"
            "<table><tr><th>Souvenir</th><th>Type</th><th>D'où</th><th></th></tr>"
            + "".join(rows)
            + "</table>"
        )
        return _page("Mémoire", body)

    async def forget_fact(request: Request) -> Response:
        fact_id = str(request.path_params["id"])
        await console.memory.invalidate(fact_id, "retiré par toi depuis la console", owner)
        return RedirectResponse(f"{PREFIX}/memory", status_code=303)

    async def commitments(_request: Request) -> Response:
        rows = [
            f"<tr><td>{escape(c.what)}</td><td>{escape(c.debtor)} → {escape(c.creditor)}</td>"
            f"<td>{escape(c.due or '')}</td><td>"
            + _button(f"commitments/{c.id}/done", "Fait")
            + " "
            + _button(f"commitments/{c.id}/cancel", "Annuler")
            + "</td></tr>"
            for c in await console.memory.store.commitments()
        ]
        body = (
            "<table><tr><th>Quoi</th><th>Qui doit à qui</th><th>Échéance</th><th></th></tr>"
            + "".join(rows)
            + "</table>"
        )
        return _page("Engagements ouverts", body)

    async def close_commitment(request: Request) -> Response:
        status = (
            CommitmentStatus.DONE
            if request.path_params["how"] == "done"
            else CommitmentStatus.CANCELLED
        )
        await console.memory.close_commitment(
            str(request.path_params["id"]), status, owner, evidence="depuis la console"
        )
        return RedirectResponse(f"{PREFIX}/commitments", status_code=303)

    async def playbook(_request: Request) -> Response:
        rules = [
            f"<tr><td>{escape(r.rule)}</td><td class=muted>{escape(r.basis.value)}</td><td>"
            + _button(f"rules/{r.id}/retire", "Retirer")
            + "</td></tr>"
            for r in await console.memory.store.rules()
        ]
        settings = [
            f"<tr><td>{escape(s.key.value)}</td><td>{escape(str(s.value))}</td>"
            f"<td class=muted>{escape(s.reason)}</td></tr>"
            for s in (await console.memory.store.settings()).values()
        ]
        body = (
            "<h2>Règles apprises</h2><table>" + "".join(rules) + "</table>"
            "<h2>Réglages</h2><table>" + "".join(settings) + "</table>"
        )
        return _page("Mon manuel pour travailler avec toi", body)

    async def retire_rule(request: Request) -> Response:
        await console.memory.retire_rule(
            str(request.path_params["id"]), "retirée par toi depuis la console", owner
        )
        return RedirectResponse(f"{PREFIX}/playbook", status_code=303)

    async def autonomy(_request: Request) -> Response:
        if console.autonomy is None:
            return _page("Autonomie", "<p>Indisponible.</p>")
        await console.autonomy.start()
        rows: list[str] = []
        for action in console.autonomy.autonomable:
            alone = action in console.autonomy.state.granted
            state = (
                "sans demander"
                if alone
                else f"je demande (oui {console.autonomy.state.streak.get(action, 0)} fois de "
                "suite)"
            )
            button = _button(f"autonomy/{action}/revoke", "Me redemander") if alone else ""
            rows.append(
                f"<tr><td>{escape(label(action))}</td><td>{state}</td><td>{button}</td></tr>"
            )
        return _page("Ce que je peux faire seul", "<table>" + "".join(rows) + "</table>")

    async def revoke(request: Request) -> Response:
        if console.autonomy is not None:
            await console.autonomy.revoke(
                str(request.path_params["action"]), "depuis la console", None
            )
        return RedirectResponse(f"{PREFIX}/autonomy", status_code=303)

    async def activity(_request: Request) -> Response:
        last = await console.journal.last_seq()
        events = await console.journal.read(max(0, last - 3000), 3000)
        asked: dict[str, str] = {
            str(e.payload["approval_id"]): str(e.payload.get("tool"))
            for e in events
            if e.kind == "approval.requested"
        }
        rows: list[str] = []
        for e in reversed([e for e in events if e.kind in ACTIVITY and not e.redacted][-150:]):
            detail = _detail(e, asked)
            rows.append(
                f"<tr><td class=muted>{_when(e, console.timezone)}</td>"
                f"<td>{escape(ACTIVITY[e.kind])}</td><td>{escape(detail)}</td></tr>"
            )
        return _page("Ce que j'ai fait, et pourquoi", "<table>" + "".join(rows) + "</table>")

    async def sources(request: Request) -> Response:
        source = request.query_params.get("source", "").strip()
        report = ""
        if source:
            found = await console.provenances.trace(source)
            report = f"<pre>{escape(found.describe())}</pre>" + (
                f'<form method="post" action="{PREFIX}/contain">'
                f'<input type="hidden" name="source" value="{escape(source)}">'
                "<button>Mettre en quarantaine</button></form>"
                if not found.empty
                else ""
            )
        body = (
            "<p>Un mail, une page ou un autre Orion malveillant ? Donne sa source (telle qu'elle "
            "apparaît dans la mémoire) pour voir tout ce qui en dépend.</p>"
            f'<form method="get" action="{PREFIX}/sources"><input type="text" name="source" '
            f'value="{escape(source)}" placeholder="mail:perso:&lt;id@exemple&gt;"> '
            "<button>Tracer</button></form>" + report
        )
        return _page("Sources suspectes", body)

    async def contain(request: Request) -> Response:
        form = parse_qs((await request.body()).decode())
        source = (form.get("source") or [""])[0].strip()
        found = await console.provenances.contain(source, "depuis la console", "owner:console")
        return _page("Quarantaine", f"<pre>{escape(found.describe())}</pre><p>Fait.</p>")

    def get(path: str, handler: Callable[[Request], Awaitable[Response]]) -> Route:
        return Route(f"{PREFIX}{path}", guarded(handler), methods=["GET"])

    def post(path: str, handler: Callable[[Request], Awaitable[Response]]) -> Route:
        return Route(f"{PREFIX}{path}", guarded(handler), methods=["POST"])

    async def handed(scope: Scope, receive: Receive, send: Send) -> None:
        """The hand-over page (D116), when the computer module put it there."""
        app = console.browser
        if app is None:
            await Response("not here", status_code=404)(scope, receive, send)
            return
        await app(scope, receive, send)

    return Starlette(
        routes=[
            Route(f"{PREFIX}/login", login, methods=["GET"]),
            Route(f"{PREFIX}/telegram", from_telegram, methods=["GET"]),
            Route(f"{PREFIX}/telegram", telegram_login, methods=["POST"]),
            Mount(f"{PREFIX}/browser", app=handed),
            get("/", home),
            get("/memory", memory),
            post("/facts/{id}/forget", forget_fact),
            get("/commitments", commitments),
            post("/commitments/{id}/{how}", close_commitment),
            get("/playbook", playbook),
            post("/rules/{id}/retire", retire_rule),
            get("/autonomy", autonomy),
            post("/autonomy/{action}/revoke", revoke),
            get("/dashboard", board),
            get("/planned", planned),
            get("/activity", activity),
            get("/sources", sources),
            post("/contain", contain),
        ]
    )


def _detail(event: Event, asked: dict[str, str]) -> str:
    p: dict[str, Any] = event.payload
    if event.kind == "initiative.delivered":
        return f"{p.get('why', '')} — {p.get('arbiter', '')}".strip(" —")
    if event.kind == "initiative.suppressed":
        return f"{p.get('fallback', p.get('key', ''))} ({p.get('reason', '')})"
    if event.kind == "approval.decided":
        tool = asked.get(str(p.get("approval_id")), "?")
        return f"{tool} : {'oui' if p.get('approved') else 'non'} ({p.get('how', '')})"
    if event.kind == "mail.sent":
        return f"à {', '.join(p.get('to', []))} : {p.get('subject', '')}"
    if event.kind == "calendar.event.added":
        return str(p.get("occurrence", {}).get("title", ""))
    if event.kind in ("autonomy.granted", "autonomy.revoked"):
        return label(str(p.get("action", "")))
    if event.kind == "provenance.contained":
        return str(p.get("source", ""))
    if event.kind in ("job.started", "job.finished"):
        return str(p.get("title") or p.get("job_id", ""))
    if event.kind == "web.searched":
        return str(p.get("query", ""))
    return ""
