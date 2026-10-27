"""Secrets from the owner's phone, straight to the guardian (D94).

« Active mon mail » needs the mailbox's password, and a secret never goes through a message:
the conversation is journaled and the model reads it. So the guardian serves a form of its own,
on its own public listener (behind Caddy at `/secrets/`), that the core never sits in front of:

- the core asks (`POST /v1/secrets/ask`, a variable's name and why); the guardian sends the
  owner a Telegram button that opens the form as a Mini App, with a one-time token (15 min);
- the form posts Telegram's signed launch data with the value; the guardian checks the
  signature with the bot's token and that it is the owner, then writes the value to its own
  `secrets.env` (mode 0600, in its data dir), records that the name was set (never the
  value), tells the owner, and restarts to load it;
- a variable already set on the server (systemd's environment file) is never replaced: those
  are changed on the server.

The core then names the variable in orion.toml (`{ env = "MAIL_PERSO_PASSWORD" }`), which is a
change the owner approves (D92). It never sees the value.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import secrets as tokens
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from orion.channels.telegram import TelegramError, check_init_data
from orion.core.events import Trust

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.guardian.service import GuardianService
    from orion.guardian.telegram import TelegramGate

log = structlog.get_logger(__name__)

SECRET_ASKED = "guardian.secret.asked"
SECRET_SET = "guardian.secret.set"
VAULT_FILE = "secrets.env"
CORE_VAULT = "core.secrets.env"
CORE_SECRETS = frozenset({"OPENROUTER_API_KEY"})
"""The core's only secrets: written for the core, never kept by the guardian."""
ASK_TTL = 15 * 60.0
NAME = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
MAX_VALUE = 4096


def vault_path(config: OrionConfig) -> Path:
    return config.guardian.data_dir / VAULT_FILE


def core_vault_path(config: OrionConfig) -> Path | None:
    """The core's own secret (`CORE_SECRETS`), set from the app: beside orion.toml, in the
    guardian's folder, readable by the core through the group (D154)."""
    return config.source.parent / CORE_VAULT if config.source is not None else None


def load_vault(config: OrionConfig) -> set[str]:
    """Put the vault's secrets in the environment. Returns the names it set. A name set from
    the app, signed with his passkey, wins over the server's file (D154); a name the Telegram
    form refused there never got in."""
    from orion.setup import read_env

    loaded: set[str] = set()
    for name, value in read_env(vault_path(config)).items():
        if value:
            os.environ[name] = value
            loaded.add(name)
    return loaded


@dataclass
class Asked:
    name: str
    why: str
    until: float


class Vault:
    def __init__(
        self, service: GuardianService, gate: TelegramGate | None, from_vault: set[str]
    ) -> None:
        self.service = service
        self.gate = gate
        self.from_vault = from_vault
        """Names the vault set: those may be set again; any other one set is the server's."""
        settings = service.config.guardian
        self.base = settings.secrets_url.rstrip("/")
        self.host, self.port = settings.secrets_host, settings.secrets_port
        self.asked: dict[str, Asked] = {}
        """One-time token → what the owner is asked; kept in memory: a restart ends them."""
        self.now = time.time
        self.public_routes: list[Route] = []
        """More pages on the same listener (the logins' form), added before `install`."""

    def install(self) -> None:
        self.service.routes.append(Route("/v1/secrets/ask", self._ask, methods=["POST"]))
        if self.gate is not None and self.base:  # the Telegram form; the app needs none
            self.service.listeners.append(("secrets", self.public_app(), self.host, self.port))

    def public_app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/secrets/", self._page, methods=["GET"]),
                Route("/secrets/set", self._set, methods=["POST"]),
                *self.public_routes,
            ]
        )

    def _refusal(self, name: str) -> str | None:
        if not NAME.fullmatch(name):
            return "a variable name in capitals, e.g. MAIL_PERSO_PASSWORD"
        if name in os.environ and name not in self.from_vault:
            return f"{name} is set on the server: it is changed there"
        return None

    async def _ask(self, request: Request) -> Response:
        body = cast(dict[str, Any], json.loads(await request.body() or b"{}"))
        name, why = str(body.get("name", "")).strip(), str(body.get("why", "")).strip()[:300]
        refusal = self._refusal(name)
        if refusal is not None:
            return await self.service.deny(f"secret {name}", refusal)
        token = tokens.token_urlsafe(24)
        self.asked = {t: a for t, a in self.asked.items() if a.until > self.now()}
        self.asked[token] = Asked(name, why, self.now() + ASK_TTL)
        edge = self.service.app_edge
        if edge is not None:  # his app: Réglages > Secrets (D154)
            await edge.push(
                {
                    "title": "Orion te demande un secret",
                    "body": f"{name}" + (f" ({why})" if why else "") + " : à saisir dans "
                    "l'app, Réglages > Secrets.",
                    "tag": f"secret-{name}",
                    "kind": "message",
                }
            )
        if self.gate is None:
            await self.service.ledger.record(SECRET_ASKED, {"name": name, "why": why})
            return JSONResponse({"asked": name})
        markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "Saisir le secret",
                        "web_app": {"url": f"{self.base}/secrets/?t={token}"},
                    }
                ]
            ]
        }
        text = (
            f"Il me faut un secret : {name}" + (f" ({why})" if why else "") + ".\nTouche le "
            "bouton : le formulaire est celui de mon gardien, et la valeur ne passe jamais par "
            "notre conversation. Valable 15 minutes."
        )
        try:
            await self.gate.client.call(
                "sendMessage", chat_id=self.gate.owner_id, text=text, reply_markup=markup
            )
        except TelegramError as exc:
            return JSONResponse({"error": f"guardian: Telegram unreachable: {exc}"}, 502)
        await self.service.ledger.record(SECRET_ASKED, {"name": name, "why": why})
        return JSONResponse({"asked": name})

    async def _page(self, request: Request) -> Response:
        asked = self.asked.get(request.query_params.get("t", ""))
        if asked is None or asked.until <= self.now():
            return HTMLResponse(PAGE.format(body=EXPIRED), status_code=410)
        form = _FORM.format(
            name=html.escape(asked.name),
            why=html.escape(asked.why),
            token=html.escape(request.query_params["t"], quote=True),
        )
        return HTMLResponse(PAGE.format(body=form))

    async def _set(self, request: Request) -> Response:
        from orion.setup import write_env

        try:
            body = cast(dict[str, Any], json.loads(await request.body() or b"{}"))
        except json.JSONDecodeError:
            return JSONResponse({"error": "malformed"}, 400)
        token, value = str(body.get("token", "")), str(body.get("value", ""))
        gate = self.gate
        if gate is None:
            return JSONResponse({"error": "no Telegram here: use the app"}, 404)
        user = check_init_data(str(body.get("init", "")), gate.client.token, self.now())
        if not gate.owner_id or user != gate.owner_id:
            await self.service.deny("secret", "not opened by the owner, from Telegram")
            return JSONResponse({"error": "Ouvre ce formulaire depuis Telegram."}, 403)
        asked = self.asked.pop(token, None)
        if asked is None or asked.until <= self.now():
            return JSONResponse({"error": "Ce lien a expiré : redemande-le à Orion."}, 410)
        if not value.strip() or len(value) > MAX_VALUE or "\n" in value:
            self.asked[token] = asked
            return JSONResponse({"error": "Valeur vide ou invalide."}, 400)
        refusal = self._refusal(asked.name)
        if refusal is not None:
            return JSONResponse({"error": refusal}, 409)
        write_env(vault_path(self.service.config), {asked.name: value.strip()})
        os.environ[asked.name] = value.strip()
        self.from_vault.add(asked.name)
        await self.service.ledger.record(SECRET_SET, {"name": asked.name})
        log.info("guardian.secret_set", name=asked.name)
        await gate.tell(f"C'est enregistré dans mon gardien : {asked.name}.")
        self._restart()
        return JSONResponse({"set": asked.name})

    # -- from his app, signed with his passkey for this name and this value (D154) ----------

    def asked_now(self) -> list[dict[str, str]]:
        now = self.now()
        seen: dict[str, str] = {}
        for asked in self.asked.values():
            if asked.until > now:
                seen[asked.name] = asked.why
        return [{"name": name, "why": why} for name, why in sorted(seen.items())]

    async def set_from_app(self, name: str, value: str, device: str) -> str:
        """Set `name`, replacing the server's value too: his signature is for this very value.
        Where: `guardian`, or `core` for the core's own key (then the core restarts)."""
        from orion.setup import write_env

        value = value.strip()
        if not NAME.fullmatch(name):
            raise ValueError("a variable name in capitals, e.g. MAIL_PERSO_PASSWORD")
        if not value or len(value) > MAX_VALUE or "\n" in value or "\r" in value:
            raise ValueError("empty or invalid value")
        if name in CORE_SECRETS:
            path = core_vault_path(self.service.config)
            if path is None:
                raise ValueError("Orion runs without an orion.toml: no place for the core's key")
            write_env(path, {name: value})
            path.chmod(0o640)  # the core reads it through the group; it watches it and restarts
            where = "core"
        else:
            write_env(vault_path(self.service.config), {name: value})
            os.environ[name] = value
            self.from_vault.add(name)
            where = "guardian"
        self.asked = {t: a for t, a in self.asked.items() if a.name != name}
        await self.service.ledger.record(
            SECRET_SET, {"name": name, "how": f"passkey:{device}"}, Trust.OWNER, source="app"
        )
        log.info("guardian.secret_set", name=name, how="app", where=where)
        if where == "guardian":
            self._restart()
        return where

    def _restart(self) -> None:
        if self.service.restart is not None:
            asyncio.get_running_loop().create_task(self.service.restart())


PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Orion — secret</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
body{{font:16px system-ui,sans-serif;margin:0;padding:20px;
background:var(--tg-theme-bg-color,#fff);color:var(--tg-theme-text-color,#111)}}
input{{width:100%;box-sizing:border-box;font-size:16px;padding:12px;margin:12px 0;
border:1px solid #8886;border-radius:8px;background:transparent;color:inherit}}
button{{width:100%;padding:12px;font-size:16px;border:0;border-radius:8px;
background:var(--tg-theme-button-color,#2a7ae2);color:var(--tg-theme-button-text-color,#fff)}}
.note{{opacity:.7;font-size:14px}}
</style></head><body>{body}</body></html>"""

EXPIRED = "<p>Ce lien a expiré. Redemande-le à Orion.</p>"

_FORM = """<h3>{name}</h3><p class="note">{why}</p>
<form id="f"><input id="v" type="password" autocomplete="off" required
placeholder="Colle la valeur ici"><button>Enregistrer dans le gardien</button></form>
<p id="r" class="note">La valeur va directement au gardien d'Orion : ni la conversation ni le
modèle ne la voient.</p>
<script>
const tg = window.Telegram && Telegram.WebApp; if (tg) tg.ready();
document.getElementById("f").onsubmit = async (e) => {{
  e.preventDefault();
  const r = document.getElementById("r");
  const res = await fetch("set", {{method: "POST", headers: {{"content-type": "application/json"}},
    body: JSON.stringify({{init: tg ? tg.initData : "", token: "{token}",
      value: document.getElementById("v").value}})}});
  const data = await res.json();
  if (res.ok) {{ r.textContent = "Enregistré."; setTimeout(() => tg && tg.close(), 800); }}
  else r.textContent = data.error || "Refusé.";
}};
</script>"""
