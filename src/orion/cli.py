"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from orion import __version__
from orion.config import OrionConfig, load_config
from orion.core.hlc import HybridClock
from orion.core.journal import SqliteJournal
from orion.memory.store import MemoryStore
from orion.observability.logging import configure_logging

if TYPE_CHECKING:
    from orion.channels.inkbox import InkboxClient
    from orion.providers.router import Router


def _provider(config: OrionConfig) -> Router | None:
    from orion.providers.openrouter import OpenRouterProvider
    from orion.providers.router import Router

    try:
        api_key = config.providers.openrouter.api_key.resolve()
    except LookupError as exc:
        print(f"Impossible de démarrer : {exc}")
        return None
    openrouter = OpenRouterProvider(
        api_key.get_secret_value(),
        config.providers.openrouter.base_url,
        sort=config.providers.openrouter.sort,
    )
    agent = config.agent
    chains = {
        m: [f for f in agent.fallback_models if f != m]
        for m in {agent.model, agent.extractor_model}
    }
    return Router({"openrouter": openrouter}, "openrouter", chains)


def _inkbox(config: OrionConfig) -> InkboxClient | None:
    from orion.channels.inkbox import InkboxClient

    cfg = config.channels.imessage
    try:
        api_key = cfg.api_key.resolve()
    except LookupError as exc:
        print(f"Impossible de joindre Inkbox : {exc}")
        return None
    return InkboxClient(api_key.get_secret_value(), cfg.base_url)


def _secrets_kept_apart(config: OrionConfig) -> bool:
    """Run split, the core must not see the guardian's secrets (D28): fail closed."""
    if not config.guardian.enabled:
        return True
    from orion.guardian.secrets import exposed

    visible = exposed(config)
    if visible:
        print(
            f"Impossible de démarrer : {', '.join(visible)} est visible par le noyau. Avec "
            "[guardian] enabled = true, ces secrets ne vivent que dans l'environnement de "
            "`orion guardian`."
        )
    return not visible


async def _chat(config: OrionConfig) -> int:
    # Imported lazily: `orion version` and `orion doctor` must work without network deps loaded.
    from orion.app import build_app
    from orion.channels.terminal import TerminalApprover, run_terminal
    from orion.modules.base import ModuleError

    if not _secrets_kept_apart(config):
        return 2
    provider = _provider(config)
    if provider is None:
        return 3
    try:
        async with build_app(config, provider) as app:
            app.approver.target = TerminalApprover()
            await run_terminal(app.orion)
    except ModuleError as exc:
        print(f"Impossible de démarrer : {exc}")
        return 2
    finally:
        await provider.aclose()
    return 0


async def _guardian(config: OrionConfig) -> int:
    from orion.guardian.process import run
    from orion.guardian.service import GuardianError

    try:
        await run(config)
    except GuardianError as exc:
        print(f"Le gardien ne peut pas démarrer : {exc}")
        return 2
    return 0


async def _serve(config: OrionConfig) -> int:
    from orion.app import build_app
    from orion.modules.base import ModuleError
    from orion.modules.registry import load
    from orion.serve import serve

    try:
        modules = load(config)
    except ModuleError as exc:
        print(f"Impossible de démarrer : {exc}")
        return 2
    if not any(module.channel for module in modules):
        print("Aucun canal activé : mets `enabled = true` dans [channels.imessage].")
        return 2
    if not _secrets_kept_apart(config):
        return 2
    provider = _provider(config)
    if provider is None:
        return 3
    try:
        async with build_app(config, provider, modules) as app:
            await serve(app)
    except ModuleError as exc:
        print(f"Impossible de démarrer : {exc}")
        return 2
    finally:
        await provider.aclose()
    return 0


async def _inkbox_calls(config: OrionConfig) -> int:
    client = _inkbox(config)
    if client is None:
        return 3
    try:
        identity = await client.identity(config.channels.imessage.identity or "")
        calls = await client.calls(identity.id if identity else None)
    finally:
        await client.aclose()
    for call in calls[:10]:
        print(
            f"{call.created_at[:16]}  {call.direction:8}  {call.remote_phone_number:16}  "
            f"{call.status}"
            + (f" ({call.hangup_reason})" if call.hangup_reason else "")
            + (f", {call.duration_seconds} s" if call.duration_seconds else "")
        )
    if not calls:
        print("Aucun appel.")
    return 0


async def _inkbox_setup(config: OrionConfig, webhook_url: str | None) -> int:
    cfg = config.channels.imessage
    if not cfg.identity:
        print('Choisis un identifiant pour Orion : `identity = "..."` dans [channels.imessage].')
        return 2
    client = _inkbox(config)
    if client is None:
        return 3
    try:
        identity = await client.identity(cfg.identity)
        if identity is None:
            identity = await client.create_identity(cfg.identity, "Orion")
            print(f"Identité créée : @{identity.agent_handle}")
        elif not identity.imessage_enabled:
            identity = await client.enable_imessage(cfg.identity)
            print(f"iMessage activé pour @{identity.agent_handle}")
        else:
            print(f"Identité existante : @{identity.agent_handle} (iMessage actif)")
        if cfg.owner_number:
            await client.allow_only(cfg.identity, cfg.owner_number)
            print(f"Ligne réservée à {cfg.owner_number} : personne d'autre ne peut joindre Orion.")
        if config.voice.enabled:
            await client.answer_calls_with_voice_ai(identity.id)
            print("Appels entrants : la voix d'Orion décroche.")
        triage = await client.triage_number()
        print(
            f"\nDepuis ton iPhone ({cfg.owner_number or 'owner_number à renseigner'}), envoie "
            f"« {triage.command_for(cfg.identity)} » au {triage.number}. Ensuite, écris-lui ce "
            "que tu veux."
        )
        if webhook_url:
            key = await client.subscribe_webhook(
                identity.id,
                webhook_url,
                ["imessage.received", "imessage.reaction_received", "call.ended"],
            )
            print(f"\nWebhook abonné : {webhook_url}")
            if key:
                print(
                    "Clé de signature (affichée une seule fois) : mets-la dans la variable "
                    f"d'environnement {cfg.signing_key.env} du serveur :\n{key}"
                )
            else:
                print("Inkbox n'a pas renvoyé de clé : l'identité en avait déjà une.")
    finally:
        await client.aclose()
    return 0


async def _memory(config: OrionConfig) -> int:
    from orion.memory.render import profile_markdown
    from orion.memory.service import MemoryService

    store = MemoryStore(config.memory_path)
    await store.open()
    try:
        async with SqliteJournal(config.journal_path, HybridClock(config.instance)) as journal:
            await MemoryService(journal, store).sync()
            text = profile_markdown(
                await store.facts(),
                await store.commitments(),
                await store.rules(),
                list((await store.settings()).values()),
            )
    finally:
        await store.close()
    profile = config.data_dir / "profile.md"
    profile.write_text(text, encoding="utf-8")
    print(text)
    print(f"(écrit dans {profile})")
    return 0


async def _forget(config: OrionConfig, source: str, reason: str) -> int:
    from orion.memory.service import MemoryService

    store = MemoryStore(config.memory_path)
    await store.open()
    try:
        async with SqliteJournal(config.journal_path, HybridClock(config.instance)) as journal:
            report = await MemoryService(journal, store).forget_source(
                source, reason=reason, actor="owner:cli"
            )
    finally:
        await store.close()
    print(
        f"{report.redacted_events} événements effacés, {report.facts_removed} faits et "
        f"{report.commitments_removed} engagements retirés. "
        f"Vérification par rejeu : {'OK' if report.verified else 'ÉCHEC'}"
    )
    return 0 if report.verified else 1


async def _contain(config: OrionConfig, source: str, apply: bool, reason: str) -> int:
    from orion.initiative.timers import TimerProjection, Timers
    from orion.memory.provenance import Provenances
    from orion.memory.service import MemoryService

    store = MemoryStore(config.memory_path)
    await store.open()
    try:
        async with SqliteJournal(config.journal_path, HybridClock(config.instance)) as journal:
            memory = MemoryService(journal, store)
            await memory.sync()
            provenances = Provenances(journal, memory, Timers(journal, TimerProjection()))
            if apply:
                found = await provenances.contain(source, reason, "owner:cli")
            else:
                found = await provenances.trace(source)
    finally:
        await store.close()
    print(found.describe())
    if not apply and not found.empty:
        print("(rien n'a changé : relance avec --apply pour invalider)")
    return 0


async def _google_auth(client_id: str, secret_env: str) -> int:
    import os
    import secrets

    import httpx

    from orion.calendar.write import CalendarWriteError, google_consent_url, google_refresh_token

    secret = os.environ.get(secret_env)
    if not secret:
        print(f"Mets le secret du client OAuth dans {secret_env}.")
        return 2
    state = secrets.token_urlsafe(16)
    print("1. Ouvre cette adresse dans ton navigateur et accepte :\n")
    print(google_consent_url(client_id, state))
    print("\n2. Google te renvoie vers une page 127.0.0.1 qui ne s'affiche pas : c'est normal.")
    redirected = await asyncio.to_thread(input, "   Colle ici l'adresse complète de cette page : ")
    async with httpx.AsyncClient(timeout=20.0) as http:
        try:
            token = await google_refresh_token(http, client_id, secret, redirected, state)
        except CalendarWriteError as exc:
            print(f"Échec : {exc}")
            return 1
    print("\nMets ceci dans les secrets du gardien (jamais dans orion.toml) :")
    print(f"GOOGLE_REFRESH_TOKEN={token}")
    return 0


def _backup(config: OrionConfig, destination: Path, keep: int, guardian: bool) -> int:
    from orion.core.backup import BackupError, backup

    source = config.guardian_ledger_path if guardian else config.journal_path
    try:
        result = backup(source, destination, keep)
    except BackupError as exc:
        print(f"Sauvegarde impossible : {exc}")
        return 1
    print(f"{result.events} événements copiés dans {result.path}")
    for old in result.removed:
        print(f"ancienne copie retirée : {old}")
    return 0


async def _doctor(config: OrionConfig) -> int:
    print(f"orion {__version__}")
    print(f"instance:  {config.instance}")
    print(f"data dir:  {config.data_dir.resolve()}")
    async with SqliteJournal(config.journal_path, HybridClock(config.instance)) as journal:
        print(f"journal:   {config.journal_path.resolve()} ({await journal.last_seq()} events)")
    try:
        config.providers.openrouter.api_key.resolve()
        print("openrouter: api key present")
    except LookupError as exc:
        print(f"openrouter: {exc}")
    if config.guardian.enabled:
        print(f"guardian:  {await _guardian_status(config)}")
    from orion.modules.base import ModuleError
    from orion.modules.registry import discover, load

    try:
        enabled = {module.name: module for module in load(config)}
    except ModuleError as exc:
        print(f"modules:   {exc}")
        return 2
    for kind in discover():
        module = enabled.get(kind.name)
        detail = "; ".join(module.doctor()) if module else ""
        state = "on" if module else "off"
        print(f"{kind.name + ':':<15}{state}" + (f" ({detail})" if detail else ""))
    return 0


def _keygen() -> int:
    from orion.federation.crypto import Identity, generate_secret

    secret = generate_secret()
    print(
        "Clé privée de cette instance : mets-la dans l'environnement du gardien seulement "
        "(ORION_FEDERATION_KEY par défaut), jamais dans orion.toml.\n"
        f"{secret}\nEmpreinte publique : {Identity(secret).fingerprint}"
    )
    return 0


async def _federation(config: OrionConfig, args: argparse.Namespace) -> int:
    from orion.guardian.client import GuardianClient, GuardianUnavailableError
    from orion.guardian.federation import ADMIN_HEADER

    token_file = config.guardian.data_dir / "admin.token"
    admin: dict[str, str] = {}
    if args.federation_command in ("invite", "accept", "revoke"):
        try:
            admin = {ADMIN_HEADER: token_file.read_text(encoding="utf-8").strip()}
        except OSError:
            print(
                f"Impossible de lire {token_file} : lance cette commande sous l'utilisateur "
                "du gardien, une fois `orion guardian` démarré avec [federation] enabled = true."
            )
            return 2
    client = GuardianClient.over_socket(config.guardian.socket)
    try:
        if args.federation_command == "invite":
            data = await client.post("/v1/federation/invites", {"label": args.name}, admin)
            print(
                f"Invitation pour {args.name}, valable jusqu'au {data['expires_at'][:10]}. "
                "Donne-lui ce code (en main propre ou par message) ; son propriétaire lance "
                f"`orion federation accept <code> --name <ton nom>` :\n{data['code']}"
            )
        elif args.federation_command == "accept":
            data = await client.post(
                "/v1/federation/accept", {"code": args.code, "label": args.name}, admin
            )
            print(
                f"Relié à l'Orion de {args.name} ({data['instance']}). Empreinte : "
                f"{data['fingerprint']} : vérifie-la avec {args.name}."
            )
        elif args.federation_command == "revoke":
            data = await client.post("/v1/federation/revoke", {"peer": args.peer}, admin)
            print(f"Relation avec {data['revoked']} révoquée.")
        else:
            me = await client.get("/v1/federation/identity")
            print(f"Cette instance : {me['instance']}, empreinte {me['fingerprint']}")
            for peer in await client.peers():
                print(f"- {peer['label']} ({peer['instance']})")
    except GuardianUnavailableError as exc:
        print(f"Le gardien a refusé ou ne répond pas : {exc}")
        return 2
    finally:
        await client.aclose()
    return 0


async def _guardian_status(config: OrionConfig) -> str:
    from orion.guardian.client import GuardianClient, GuardianUnavailableError
    from orion.guardian.secrets import exposed

    client = GuardianClient.over_socket(config.guardian.socket)
    try:
        await client.health()
        state = f"reachable at {config.guardian.socket}"
    except GuardianUnavailableError:
        state = f"not reachable at {config.guardian.socket} (start `orion guardian`)"
    finally:
        await client.aclose()
    visible = exposed(config)
    return state + (f"; secrets visible here: {', '.join(visible)}" if visible else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orion")
    parser.add_argument("--config", type=Path, default=None, help="Path to orion.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="Print the version")
    sub.add_parser("doctor", help="Check configuration and storage")
    sub.add_parser("chat", help="Talk to Orion in the terminal")
    sub.add_parser("serve", help="Run Orion: iMessage channel and initiative engine")
    sub.add_parser("guardian", help="Run the guardian process, which holds the secrets (D28)")
    inkbox = sub.add_parser("inkbox", help="Inkbox (iMessage) administration")
    inkbox_sub = inkbox.add_subparsers(dest="inkbox_command", required=True)
    setup = inkbox_sub.add_parser("setup", help="Create Orion's identity and show how to connect")
    setup.add_argument("--webhook-url", default=None, help="Public HTTPS URL for webhook mode")
    inkbox_sub.add_parser("calls", help="Recent calls on Orion's line, with why they ended")
    federation = sub.add_parser("federation", help="Pair with other people's Orion (D29)")
    federation_sub = federation.add_subparsers(dest="federation_command", required=True)
    federation_sub.add_parser("keygen", help="Create this instance's private key")
    federation_sub.add_parser("peers", help="This instance's fingerprint and its paired Orions")
    invite = federation_sub.add_parser("invite", help="Create a one-time invitation code")
    invite.add_argument("--name", required=True, help="How you call the person you invite")
    accept = federation_sub.add_parser("accept", help="Pair using someone's invitation code")
    accept.add_argument("code")
    accept.add_argument("--name", required=True, help="How you call the person who invited you")
    revoke = federation_sub.add_parser("revoke", help="End a pairing")
    revoke.add_argument("peer", help="Name or instance")
    sub.add_parser("memory", help="Show what Orion remembers (also writes profile.md)")
    forget = sub.add_parser("forget", help="Forget everything that came from a source")
    forget.add_argument("source", help="Source prefix, e.g. 'terminal:' or 'mail:abc'")
    forget.add_argument("--reason", default="owner request")
    google = sub.add_parser("google", help="Let Orion add events to a Google Calendar")
    google_sub = google.add_subparsers(dest="google_command", required=True)
    google_auth = google_sub.add_parser("auth", help="Get the refresh token (once)")
    google_auth.add_argument("client_id", help="OAuth client id (type: Desktop app)")
    google_auth.add_argument(
        "--secret-env", default="GOOGLE_CLIENT_SECRET", help="Variable holding the client secret"
    )
    contain = sub.add_parser(
        "contain", help="What depends on a suspicious source; --apply invalidates it"
    )
    contain.add_argument("source", help="e.g. 'mail:perso:<id@x>' or 'web:https://…'")
    contain.add_argument("--apply", action="store_true")
    contain.add_argument("--reason", default="source suspecte")
    backup = sub.add_parser("backup", help="Copy the journal (everything else is rebuilt)")
    backup.add_argument("destination", type=Path, help="Folder that holds the copies")
    backup.add_argument("--keep", type=int, default=14, help="Copies kept, newest first")
    backup.add_argument(
        "--guardian", action="store_true", help="Copy the guardian's ledger instead"
    )
    args = parser.parse_args(argv)

    if args.command == "version":
        print(__version__)
        return 0

    config = load_config(args.config)
    configure_logging(config.logging.level, config.logging.json_output)
    if args.command == "doctor":
        return asyncio.run(_doctor(config))
    if args.command == "memory":
        return asyncio.run(_memory(config))
    if args.command == "forget":
        return asyncio.run(_forget(config, args.source, args.reason))
    if args.command == "contain":
        return asyncio.run(_contain(config, args.source, args.apply, args.reason))
    if args.command == "google":
        return asyncio.run(_google_auth(args.client_id, args.secret_env))
    if args.command == "backup":
        return _backup(config, args.destination, args.keep, args.guardian)
    if args.command == "inkbox":
        if args.inkbox_command == "calls":
            return asyncio.run(_inkbox_calls(config))
        return asyncio.run(_inkbox_setup(config, args.webhook_url))
    if args.command == "serve":
        try:
            return asyncio.run(_serve(config))
        except KeyboardInterrupt:
            return 130
    if args.command == "federation":
        if args.federation_command == "keygen":
            return _keygen()
        return asyncio.run(_federation(config, args))
    if args.command == "guardian":
        try:
            return asyncio.run(_guardian(config))
        except KeyboardInterrupt:
            return 130
    if args.command == "chat":
        try:
            return asyncio.run(_chat(config))
        except KeyboardInterrupt:
            return 130
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
