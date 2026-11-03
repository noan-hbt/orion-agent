"""Hooks: `[modules.hooks]` (on by default; nothing happens until a hook is made, D225).

« À chaque mail de ma banque, range-le et dis-moi le montant », « tous les lundis à 9 h,
fais-moi le point de la semaine », « quand GitHub appelle, regarde ce qui a cassé ». The owner
makes hooks in the app (Réglages > Hooks), or asks Orion, who makes them with `hooks`; Orion
makes his own too. See `orion.hooks.service` for what a hook is and how it fires.

Services: one reads the journal as it grows and checks the hours (`hooks.watch`), one serves
the webhook addresses (`hooks.http`, `/hooks/in/<hook>/<token>`, behind Caddy).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog
import uvicorn
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from orion.core.events import Provenance, Trust
from orion.core.ids import new_id
from orion.core.web import Server
from orion.hooks.catalog import HOOK_THREAD, WEBHOOK, catalog_lines
from orion.hooks.service import Fire, Hook, HookError, Hooks, HookSpec, hook_key
from orion.modules.base import Daemon, Module, ModuleSettings, Planned, Plate
from orion.runtime import OWNER_THREAD
from orion.tools.actions import NO_ARGS, Action, action_tool
from orion.tools.base import (
    DRAWER,
    Effect,
    Reversibility,
    Tool,
    ToolContext,
    ToolInputError,
    ToolRegistry,
    ToolResult,
)

if TYPE_CHECKING:
    from orion.config import OrionConfig

log = structlog.get_logger(__name__)

PATH = "/hooks/in"
WATCH_EVERY = 3.0
"""Seconds between two reads of the journal: a hook reacts within a few seconds."""


class HookSettings(ModuleSettings):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8797
    public_url: str = Field(
        default="",
        description="The HTTPS address services reach (Caddy in front); empty: the app's",
    )
    per_hook_per_day: int = Field(default=24, ge=1, le=1000)
    per_day: int = Field(default=100, ge=1, le=5000)
    min_gap_seconds: int = Field(default=30, ge=0, le=3600)


class _Change(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hook: str = Field(description="The hook's id (hk_…)")
    name: str | None = Field(default=None, max_length=60)
    match: str | None = Field(default=None, max_length=200)
    pattern: str | None = Field(default=None, max_length=200)
    cron: str | None = Field(default=None, max_length=100)
    then: str | None = Field(default=None, max_length=1500)
    tool: str | None = Field(default=None, max_length=60)
    args: dict[str, Any] | None = None
    notify: str | None = Field(default=None, max_length=300)
    level: str | None = None
    once: bool | None = None
    until: str | None = None
    cooldown_minutes: int | None = Field(default=None, ge=0, le=7 * 24 * 60)
    enabled: bool | None = Field(default=None, description="false: paused; true: back on")


class _One(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hook: str = Field(description="The hook's id (hk_…)")


def checker(registry: ToolRegistry) -> Callable[[str, dict[str, Any]], str | None]:
    """What is wrong with a custom action, checked against Orion's real tools."""

    def check(name: str, args: dict[str, Any]) -> str | None:
        tool = registry.get(name)
        if tool is None or name == DRAWER:
            return f"Pas d'outil « {name} »."
        try:
            tool.parse(json.dumps(args, ensure_ascii=False))
        except ToolInputError as exc:
            schema = json.dumps(tool.spec().parameters, ensure_ascii=False)
            return f"Arguments de {name} invalides : {exc}. Il prend : {schema}"
        return None

    return check


def hook_tool(hooks: Hooks) -> Tool:
    def provenance(ctx: ToolContext) -> Provenance:
        return Provenance(
            source=f"thread:{ctx.thread_id}",
            actor="orion:core",
            derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
        )

    def author(ctx: ToolContext) -> str:
        """His words in his conversation: his hook; anything else is Orion's own."""
        asked = ctx.thread_id == OWNER_THREAD and (
            ctx.context_trust >= Trust.OWNER or ctx.unrestricted
        )
        return "owner" if asked else "orion"

    async def triggers(_args: Any, _ctx: ToolContext) -> ToolResult:
        return ToolResult(
            "\n".join(
                [
                    *catalog_lines(),
                    "match : des mots que l'événement doit contenir (« | » entre des "
                    "alternatives). Une action : then (ce que tu fais alors), tool + args (un "
                    "de tes outils, appelé tel quel), notify (une notification telle quelle, "
                    "{evenement} pour ce qui l'a déclenché).",
                ]
            )
        )

    async def listing(_args: Any, _ctx: ToolContext) -> ToolResult:
        lines = await hooks.lines()
        return ToolResult("\n".join(lines) if lines else "Aucun hook.")

    async def create(args: HookSpec, ctx: ToolContext) -> ToolResult:
        try:
            hook = await hooks.create(
                HookSpec.model_validate(args.model_dump()),
                provenance(ctx),
                ctx.context_trust,
                author(ctx),
                free=ctx.unrestricted,
            )
        except HookError as exc:
            return ToolResult(str(exc), is_error=True)
        said = [f"Hook créé ({hook.id}) : quand {hook.when()} → {hook.what()}."]
        if not hook.enabled:
            said.append(
                "Il est en pause : la conversation contient un contenu venu d'ailleurs, il "
                "l'allume lui-même dans l'app (Réglages > Hooks). Dis-le-lui."
            )
        if hook.on == WEBHOOK:
            said.append(
                "Son adresse secrète est dans l'app (Réglages > Hooks) : dis-lui de la copier "
                "dans le service qui doit l'appeler. Ne la lui écris pas toi-même."
            )
        if hook.tool:
            said.append(
                "Son action passe par le gardien comme tout appel : si elle agit dehors, il lui "
                "sera demandé à chaque fois, sauf autonomie (`autonomy_grant`)."
            )
        if author(ctx) == "orion":
            said.append("C'est ton hook : dis-lui en une phrase que tu l'as posé, et pourquoi.")
        return ToolResult(" ".join(said))

    async def change(args: _Change, ctx: ToolContext) -> ToolResult:
        changes = {k: v for k, v in args.model_dump().items() if k != "hook" and v is not None}
        if not changes:
            return ToolResult("Rien à changer.", is_error=True)
        try:
            hook = await hooks.change(
                args.hook, changes, provenance(ctx), ctx.context_trust, free=ctx.unrestricted
            )
        except HookError as exc:
            return ToolResult(str(exc), is_error=True)
        state = "allumé" if hook.enabled else "en pause"
        return ToolResult(f"Changé ({hook.id}, {state}) : quand {hook.when()} → {hook.what()}.")

    async def delete(args: _One, ctx: ToolContext) -> ToolResult:
        try:
            hook = await hooks.delete(args.hook, provenance(ctx), ctx.context_trust)
        except HookError as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult(f"Supprimé : « {hook.name} ».")

    async def test(args: _One, _ctx: ToolContext) -> ToolResult:
        try:
            fire = await hooks.test(args.hook)
        except HookError as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult(
            f"Déclenché pour essai ({fire.id}) : ce qu'il fait arrive dans un instant."
        )

    return action_tool(
        "hooks",
        "When something happens, do something. « quand <ceci> arrive, fais <cela> », for him "
        "or for yourself (« à chaque mail de ma banque… », « tous les lundis à 9 h… », « quand "
        "j'arrive au bureau… », an address a service calls). Each fires on a trigger "
        "(`triggers` lists them), may filter with `match`, and then wakes you with `then`, "
        "calls one of your tools (`tool`, `args`), or notifies him as written (`notify`). Not "
        "for a one-off reminder at a time: `later`.",
        [
            Action("triggers", "What can set a hook off.", NO_ARGS, triggers, Effect.NONE, True),
            Action(
                "list", "His hooks and yours, with their ids.", NO_ARGS, listing, Effect.NONE, True
            ),
            Action("create", "Make a hook.", HookSpec, create, Effect.INTERNAL),
            Action(
                "change",
                "Change, pause (enabled false) or turn back on a hook.",
                _Change,
                change,
                Effect.INTERNAL,
            ),
            Action("delete", "Remove a hook.", _One, delete, Effect.INTERNAL, True, False),
            Action("test", "Fire a hook now, to see what it does.", _One, test, Effect.INTERNAL),
        ],
        reversibility=Reversibility.REVERSIBLE,
    )


class HooksModule(Module):
    name = "hooks"
    instructions = "hooks.md"
    read_when = (
        "il veut qu'une chose en déclenche une autre (« à chaque fois que… », « quand … arrive, "
        "fais… », « tous les lundis… »), ou régler ses notifications"
    )
    settings_model = HookSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.hooks: Hooks | None = None
        self._daemon: Daemon | None = None
        self._running: set[asyncio.Task[None]] = set()

    def public_url(self) -> str:
        settings: HookSettings = self.settings
        if settings.public_url:
            return settings.public_url
        mobile = self.config.modules.get("mobile", {})
        return str(mobile.get("public_url", "") or "")

    async def assemble(self, plate: Plate) -> list[Tool]:
        settings: HookSettings = self.settings
        self.hooks = Hooks(
            plate.journal,
            self.config.agent.timezone,
            hook_key(self.config.data_dir / "hooks"),
            self.public_url(),
            settings.per_hook_per_day,
            settings.per_day,
            settings.min_gap_seconds,
        )
        await self.hooks.sync()
        return [hook_tool(self.hooks)]

    async def planned(self, now: datetime, until: datetime) -> list[Planned]:
        return await self.hooks.planned(now, until) if self.hooks is not None else []

    async def review(self, now: datetime) -> list[str]:
        return await self.hooks.lines() if self.hooks is not None else []

    async def serve(self, daemon: Daemon) -> None:
        hooks = self.hooks
        if hooks is None:  # pragma: no cover - assemble always runs first
            return
        settings: HookSettings = self.settings
        self._daemon = daemon
        hooks.tools = checker(daemon.orion.registry)
        hooks.acting.append(self._act)
        daemon.reports.append(hooks)

        async def watch() -> None:
            while True:
                await hooks.scan()
                await hooks.tick()
                await asyncio.sleep(WATCH_EVERY)

        daemon.add_service("hooks.watch", watch)
        server = Server(
            uvicorn.Config(
                webhook_app(hooks),
                host=settings.host,
                port=settings.port,
                log_config=None,
                access_log=False,
                proxy_headers=True,
            )
        )
        daemon.add_service("hooks.http", server.serve)

    async def _act(self, hook: Hook, fire: Fire) -> None:
        """What a hook does besides waking Orion: its notification, its tool call."""
        daemon = self._daemon
        if daemon is None:
            return
        if hook.notify:
            text = hook.notify.replace("{evenement}", fire.gist).strip()
            try:
                await daemon.inbox.notify_as(text, new_id(), "hook", hook.level)
            except Exception:
                log.exception("hooks.notify_failed", hook=hook.id)
        if hook.tool:
            task = asyncio.create_task(self._call(daemon, hook, fire))
            self._running.add(task)
            task.add_done_callback(self._running.discard)

    async def _call(self, daemon: Daemon, hook: Hook, fire: Fire) -> None:
        """Its tool, through the guardian; an approval may take minutes, so off the watch."""
        hooks = self.hooks
        assert hooks is not None
        try:
            result, reason = await daemon.orion.call_tool(
                hook.tool,
                dict(hook.args),
                min(fire.trust, hook.trust),
                HOOK_THREAD,
                asking=f"ton hook « {hook.name} » ({hook.when()}) le demande",
            )
            await hooks.ran(fire, hook.tool, not result.is_error, result.content)
            log.info("hooks.ran", hook=hook.id, tool=hook.tool, ok=not result.is_error, why=reason)
        except Exception as exc:
            log.exception("hooks.call_failed", hook=hook.id, tool=hook.tool)
            await hooks.ran(fire, hook.tool, False, f"{type(exc).__name__}: {exc}")

    async def close(self) -> None:
        for task in list(self._running):
            task.cancel()

    def doctor(self) -> list[str]:
        settings: HookSettings = self.settings
        where = self.public_url() or "(no public_url: webhook addresses cannot be shown)"
        return [f"webhooks on {settings.host}:{settings.port}{PATH}, reached at {where}"]


def webhook_app(hooks: Hooks) -> Starlette:
    async def receive(request: Request) -> JSONResponse:
        hook_id = str(request.path_params["hook"])
        token = str(request.path_params["token"])
        body = await request.body()
        try:
            fire = await hooks.receive(
                hook_id, token, body, request.headers.get("content-type", "")
            )
        except PermissionError:
            log.warning("hooks.refused", client=request.client.host if request.client else "")
            return JSONResponse({"error": "not found"}, status_code=404)
        except OverflowError:
            return JSONResponse({"error": "too many calls"}, status_code=429)
        return JSONResponse({"ok": True, "fired": fire is not None})

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    return Starlette(
        routes=[
            Route(f"{PATH}/{{hook}}/{{token}}", receive, methods=["POST"]),
            Route("/hooks/health", health, methods=["GET"]),
        ]
    )
