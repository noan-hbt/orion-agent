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
    from orion.providers.base import ModelProvider
    from orion.providers.openrouter import OpenRouterProvider
    from orion.providers.router import Router

    providers: dict[str, ModelProvider] = {}
    try:
        api_key = config.providers.openrouter.api_key.resolve()
    except LookupError as exc:
        if not config.providers.chatgpt.enabled:
            print(f"Impossible de démarrer : {exc}")
            return None
    else:
        providers["openrouter"] = OpenRouterProvider(
            api_key.get_secret_value(),
            config.providers.openrouter.base_url,
            sort=config.providers.openrouter.sort,
        )
    if config.providers.chatgpt.enabled:
        from orion.providers.chatgpt import ChatGPTProvider
        from orion.providers.chatgpt_auth import ChatGPTAuth

        auth = ChatGPTAuth(config.chatgpt_path)
        if auth.load() is None:
            print("Impossible de démarrer : pas connecté à ChatGPT (orion chatgpt login).")
            return None
        providers["chatgpt"] = ChatGPTProvider(
            auth, default_effort=config.providers.chatgpt.reasoning_effort
        )
    agent = config.agent
    chains = {m: [f for f in agent.fallback_models if f != m] for m in agent.models()}
    try:
        return Router(providers, next(iter(providers)), chains)
    except ValueError as exc:
        print(f"Impossible de démarrer : {exc}")
        return None


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
    from orion.secretary.settings import SecretarySettings

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
        secretary = SecretarySettings.model_validate(config.modules.get("secretary", {}))
        for contact in secretary.contacts if secretary.enabled else []:
            await client.allow_imessage(cfg.identity, contact.number)
            print(f"{contact.name} ({contact.number}) peut écrire à Orion (iMessage seulement).")
        if config.voice.enabled and config.voice.live:
            await client.answer_calls_live(config.voice.live_url, identity.id)
            print("Appels entrants : Orion décroche lui-même, en direct.")
        elif config.voice.enabled:
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


async def _hands(config: OrionConfig, args: argparse.Namespace) -> int:
    from orion.hands.desk import HandsDesk

    async with SqliteJournal(config.journal_path, HybridClock(config.instance)) as journal:
        desk = HandsDesk(journal)
        await desk.start()
        if args.hands_command == "revoke":
            await desk.revoke(args.device)
            print(f"{args.device} ne peut plus se connecter (redémarre orion serve s'il l'est).")
            return 0
        code = await desk.new_code(args.device)
    print(f"Code pour {args.device} (une fois, 10 minutes) : {code}")
    print(f"Sur la machine : orion hand pair <adresse d'Orion> {code} --root ~/Documents")
    return 0


async def _places_token(config: OrionConfig) -> int:
    from orion.places.module import PATH, Places

    async with SqliteJournal(config.journal_path, HybridClock(config.instance)) as journal:
        places = Places(journal)
        await places.sync()
        token = await places.new_token()
    print(f"Jeton du téléphone (l'ancien ne marche plus) : {token}")
    print(
        "Dans Raccourcis : Automatisation > Arrivée (ou Départ) > le lieu > « Obtenir le contenu "
        f"de l'URL », POST vers https://<ton domaine>{PATH}, en-tête Authorization: Bearer "
        '<jeton>, corps JSON {"place": "boulangerie", "event": "arrive"}.'
    )
    return 0


async def _hand(args: argparse.Namespace) -> int:
    from orion.hands.hand import DEFAULT_ACTIONS, Hand, HandConfig, Refused, pair

    configure_logging("INFO", False)
    if args.hand_command == "pair":
        actions = [*DEFAULT_ACTIONS, *(["open_url"] if args.allow_open else [])]
        try:
            config = await pair(args.server, args.code, args.root, actions)
        except Refused as exc:
            print(f"Échec : {exc}")
            return 1
        config.save()
        print(f"Relié comme « {config.device} ». Lance maintenant : orion hand run")
        return 0
    try:
        await Hand(HandConfig.load()).run_forever()
    except Refused as exc:
        print(str(exc))
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


async def _console_link(config: OrionConfig) -> int:
    from orion.console.app import PREFIX
    from orion.console.auth import Gate, session_key
    from orion.console.module import ConsoleModule

    module = ConsoleModule(config)
    async with SqliteJournal(config.journal_path, HybridClock(config.instance)) as journal:
        token = await Gate(journal, session_key(config.data_dir)).new_link()
    print(f"{module.base_url()}{PREFIX}/login?t={token}")
    print("(une seule fois, pendant 10 minutes)")
    return 0


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


async def _login(name: str, url: str, out: Path | None) -> int:
    """On a machine with a screen: sign in by hand, keep the session for the guardian."""
    import json

    from playwright.async_api import async_playwright

    target = out or Path(f"{name}.session.json")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(locale="fr-FR")
        page = await context.new_page()
        await page.goto(url)
        await asyncio.to_thread(
            input, "Connecte-toi dans la fenêtre, puis reviens ici et appuie sur Entrée… "
        )
        state = await context.storage_state()
        await browser.close()
    await asyncio.to_thread(_write_private, target, json.dumps(state))
    print(
        f"Session écrite dans {target}. Copie-la sur le serveur, puis, sous l'utilisateur du "
        f"gardien : orion sessions add {name} {target.name} ; efface ensuite ce fichier."
    )
    return 0


def _write_private(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


async def _sessions(config: OrionConfig, args: argparse.Namespace) -> int:
    from orion.guardian.ledger import Ledger
    from orion.guardian.sessions import SessionError, add_session, read_state, remove_session

    clock = HybridClock(f"{config.instance}-guardian")
    async with SqliteJournal(config.guardian_ledger_path, clock) as journal:
        ledger = Ledger(journal)
        await ledger.catch_up()
        try:
            if args.sessions_command == "add":
                state = await asyncio.to_thread(read_state, args.file)
                sites = await add_session(config, ledger, args.name, state, args.domains or [])
                print(f"Session « {args.name} » gardée pour : {', '.join(sites)}.")
            elif args.sessions_command == "remove":
                gone = await remove_session(config, ledger, args.name)
                print("Session retirée." if gone else "Pas de session de ce nom.")
            else:
                found = ledger.state.sessions
                for name, sites in sorted(found.items()):
                    print(f"- {name} : {', '.join(sites)}")
                if not found:
                    print("Aucune session gardée.")
        except SessionError as exc:
            print(f"Impossible : {exc}")
            return 2
    return 0


def _library_import(config: OrionConfig, folder: Path) -> int:
    """Add a folder of notes to the library (embeddings are computed at the first search)."""
    from orion.library.module import notes_in

    if not folder.is_dir():
        print(f"{folder} n'est pas un dossier.")
        return 2
    notes = [
        (str(path.relative_to(folder)), path.resolve(), path.read_text("utf-8", "replace"))
        for path in notes_in(folder)
    ]
    added = asyncio.run(_library_add(config, [n for n in notes if n[2].strip()]))
    print(f"{added} document(s) ajouté(s) à la bibliothèque.")
    return 0


async def _library_add(config: OrionConfig, notes: list[tuple[str, Path, str]]) -> int:
    from orion.core.events import Provenance
    from orion.library.module import DOCUMENT_TRUST, Library
    from orion.library.store import LibraryStore

    store = LibraryStore(config.data_dir / "library.sqlite3")
    await store.open()
    added = 0
    try:
        async with SqliteJournal(config.journal_path, HybridClock(config.instance)) as journal:
            library = Library(journal, store, None)
            await library.sync()
            known = {title for _, title, _, _ in await store.docs()}
            for title, path, text in notes:
                if title in known:
                    continue
                provenance = Provenance(source=f"file:{path}", actor="owner:cli")
                await library.add(title, text, provenance, DOCUMENT_TRUST, None)
                added += 1
    finally:
        await store.close()
    return added


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


async def _chatgpt(config: OrionConfig, args: argparse.Namespace) -> int:
    from orion.providers.chatgpt_auth import ChatGPTAuth, ChatGPTAuthError

    auth = ChatGPTAuth(config.chatgpt_path)
    try:
        if args.chatgpt_command == "logout":
            gone = auth.sign_out()
            print(
                "Jetons effacés. Pour couper l'accès, retire aussi Orion dans les réglages de "
                "ChatGPT (applications connectées)."
                if gone
                else "Orion n'était pas connecté."
            )
            return 0
        if args.chatgpt_command == "models":
            import httpx

            from orion.providers.base import ProviderError
            from orion.providers.chatgpt import plan_models

            async with httpx.AsyncClient(timeout=30.0) as http:
                try:
                    models = await plan_models(auth, http)
                except ProviderError as exc:
                    print(f"Échec : {exc}")
                    return 1
            for slug, name in models:
                print(f"chatgpt:{slug}  ({name})")
            return 0
        if args.chatgpt_command == "status":
            known = auth.load()
            if known is None:
                print("Pas connecté : orion chatgpt login")
                return 1
            print(f"Connecté : {known.email or known.subject}")
            print(f"Usage du forfait accordé : {'oui' if known.can_use_plan else 'non'}")
            return 0 if known.can_use_plan else 1
        redirected = args.callback
        if redirected is None:
            print("1. Ouvre cette adresse dans ton navigateur, connecte-toi et accepte :\n")
            print(auth.start())
            print("\n2. Tu es renvoyé vers une page 127.0.0.1 qui ne s'affiche pas : c'est normal.")
            if not sys.stdin.isatty():
                print("   Ensuite : orion chatgpt login --callback '<adresse de cette page>'")
                return 0
            redirected = await asyncio.to_thread(
                input, "   Colle ici l'adresse complète de cette page : "
            )
        credentials = await auth.finish(redirected)
    except ChatGPTAuthError as exc:
        print(f"Échec : {exc}")
        return 1
    print(f"Connecté : {credentials.email or credentials.subject}.")
    if not credentials.can_use_plan:
        print("L'usage du forfait n'a pas été accordé : relance la connexion et coche-le.")
        return 1
    print("Orion peut utiliser ton forfait ChatGPT : modèles `chatgpt:<modèle>` dans orion.toml.")
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


async def _simulate(config: OrionConfig, days: int) -> int:
    from orion.initiative.simulate import simulate

    if not 1 <= days <= 60:
        print("--days : entre 1 et 60")
        return 1
    week = await simulate(config, days)
    print(week.render())
    return 0


def _check_backup(path: Path) -> int:
    from orion.core.backup import BackupError, verify

    files = sorted(path.glob("*.sqlite3")) if path.is_dir() else [path]
    if not files:
        print(f"Aucun journal dans {path}")
        return 1
    try:
        for file in files:
            print(f"{file} : {verify(file)} événements, tous relisibles")
    except BackupError as exc:
        print(f"Copie inutilisable : {exc}")
        return 1
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
    if config.providers.chatgpt.enabled:
        from orion.providers.chatgpt_auth import ChatGPTAuth

        known = ChatGPTAuth(config.chatgpt_path).load()
        if known is None:
            print("chatgpt:    not signed in (orion chatgpt login)")
        else:
            plan = "plan use granted" if known.can_use_plan else "plan use NOT granted"
            print(f"chatgpt:    {known.email or known.subject}, {plan}")
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
    chatgpt = sub.add_parser("chatgpt", help="Use your ChatGPT plan for Orion's models (D62)")
    chatgpt_sub = chatgpt.add_subparsers(dest="chatgpt_command", required=True)
    chatgpt_login = chatgpt_sub.add_parser("login", help="Sign in with ChatGPT")
    chatgpt_login.add_argument(
        "--callback", default=None, help="The 127.0.0.1 address the browser was sent back to"
    )
    chatgpt_sub.add_parser("status", help="Who is signed in, and whether plan use is granted")
    chatgpt_sub.add_parser("models", help="The models your plan offers")
    chatgpt_sub.add_parser("logout", help="Forget the tokens")
    hands = sub.add_parser("hands", help="Orion's hands on your machines (server side)")
    hands_sub = hands.add_subparsers(dest="hands_command", required=True)
    hands_pair = hands_sub.add_parser("pair", help="A one-time code to pair a machine")
    hands_pair.add_argument("device", help="A name for the machine, e.g. 'mac'")
    hands_revoke = hands_sub.add_parser("revoke", help="Cut a machine off")
    hands_revoke.add_argument("device")
    hand = sub.add_parser("hand", help="Run on your machine: Orion's hand there")
    hand_sub = hand.add_subparsers(dest="hand_command", required=True)
    hand_pairing = hand_sub.add_parser("pair", help="Pair this machine with your Orion")
    hand_pairing.add_argument("server", help="Your Orion's address, e.g. https://orion.example.com")
    hand_pairing.add_argument("code", help="From `orion hands pair <device>` on the server")
    hand_pairing.add_argument(
        "--root", action="append", default=[], help="A folder Orion may search and read"
    )
    hand_pairing.add_argument(
        "--allow-open", action="store_true", help="Let Orion open web pages (each one approved)"
    )
    hand_sub.add_parser("run", help="Connect and serve (keep it running)")
    console = sub.add_parser("console", help="The control page")
    console_sub = console.add_subparsers(dest="console_command", required=True)
    console_sub.add_parser("link", help="A one-time login link")
    contain = sub.add_parser(
        "contain", help="What depends on a suspicious source; --apply invalidates it"
    )
    contain.add_argument("source", help="e.g. 'mail:perso:<id@x>' or 'web:https://…'")
    contain.add_argument("--apply", action="store_true")
    contain.add_argument("--reason", default="source suspecte")
    login = sub.add_parser("login", help="On a machine with a screen: sign in to a site for Orion")
    login.add_argument("name", help="A short name for the session, e.g. 'doctolib'")
    login.add_argument("url", help="The site's sign-in page")
    login.add_argument("--out", type=Path, help="Where to write it (default: <name>.session.json)")
    sessions = sub.add_parser("sessions", help="Logged-in sessions the guardian keeps")
    sessions_sub = sessions.add_subparsers(dest="sessions_command", required=True)
    sessions_add = sessions_sub.add_parser("add", help="Keep a session made with `orion login`")
    sessions_add.add_argument("name")
    sessions_add.add_argument("file", type=Path)
    sessions_add.add_argument(
        "--domains", nargs="*", help="The sites it is for (default: the cookies' domains)"
    )
    sessions_remove = sessions_sub.add_parser("remove", help="Forget a session")
    sessions_remove.add_argument("name")
    sessions_sub.add_parser("list", help="The sessions kept, and their sites")
    places = sub.add_parser("places", help="Reminders by place, from the owner's iPhone")
    places_sub = places.add_subparsers(dest="places_command", required=True)
    places_sub.add_parser("token", help="A new token for the phone's Shortcuts automations")
    library = sub.add_parser("library", help="Orion's library of the owner's notes")
    library_sub = library.add_subparsers(dest="library_command", required=True)
    library_import = library_sub.add_parser("import", help="Add a folder of notes (.md, .txt)")
    library_import.add_argument("folder", type=Path)
    backup = sub.add_parser("backup", help="Copy the journal (everything else is rebuilt)")
    backup.add_argument("destination", type=Path, help="Folder that holds the copies")
    backup.add_argument("--keep", type=int, default=14, help="Copies kept, newest first")
    backup.add_argument(
        "--guardian", action="store_true", help="Copy the guardian's ledger instead"
    )
    simulate = sub.add_parser(
        "simulate", help="Play the next days of initiative on a copy: what Orion would send"
    )
    simulate.add_argument("--days", type=int, default=7, help="How many days ahead")
    check_backup = sub.add_parser(
        "check-backup", help="Check that a copy of the journal can be restored"
    )
    check_backup.add_argument("path", type=Path, help="The copy (a .sqlite3 file or its folder)")
    args = parser.parse_args(argv)

    if args.command == "version":
        print(__version__)
        return 0

    if args.command == "hand":
        return asyncio.run(_hand(args))

    if args.command == "check-backup":
        return _check_backup(args.path)

    config = load_config(args.config)
    configure_logging(config.logging.level, config.logging.json_output)
    if args.command == "doctor":
        return asyncio.run(_doctor(config))
    if args.command == "memory":
        return asyncio.run(_memory(config))
    if args.command == "forget":
        return asyncio.run(_forget(config, args.source, args.reason))
    if args.command == "hands":
        return asyncio.run(_hands(config, args))
    if args.command == "console":
        return asyncio.run(_console_link(config))
    if args.command == "contain":
        return asyncio.run(_contain(config, args.source, args.apply, args.reason))
    if args.command == "login":
        return asyncio.run(_login(args.name, args.url, args.out))
    if args.command == "sessions":
        return asyncio.run(_sessions(config, args))
    if args.command == "places":
        return asyncio.run(_places_token(config))
    if args.command == "library":
        return _library_import(config, args.folder)
    if args.command == "google":
        return asyncio.run(_google_auth(args.client_id, args.secret_env))
    if args.command == "chatgpt":
        return asyncio.run(_chatgpt(config, args))
    if args.command == "backup":
        return _backup(config, args.destination, args.keep, args.guardian)
    if args.command == "simulate":
        return asyncio.run(_simulate(config, args.days))
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
