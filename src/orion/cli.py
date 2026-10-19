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
        api_key.get_secret_value(), config.providers.openrouter.base_url
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


async def _chat(config: OrionConfig) -> int:
    # Imported lazily: `orion version` and `orion doctor` must work without network deps loaded.
    from orion.app import build_app
    from orion.channels.terminal import TerminalApprover, run_terminal

    provider = _provider(config)
    if provider is None:
        return 3
    try:
        async with build_app(config, provider) as app:
            app.approver.target = TerminalApprover()
            await run_terminal(app.orion)
    finally:
        await provider.aclose()
    return 0


async def _serve(config: OrionConfig) -> int:
    from orion.app import build_app
    from orion.serve import ServeError, serve

    if not config.channels.imessage.enabled:
        print("Aucun canal activé : mets `enabled = true` dans [channels.imessage].")
        return 2
    provider = _provider(config)
    client = _inkbox(config)
    if provider is None or client is None:
        return 3
    try:
        async with build_app(config, provider) as app:
            await serve(app, client)
    except ServeError as exc:
        print(f"Impossible de démarrer : {exc}")
        return 2
    finally:
        await client.aclose()
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
            f"« {triage.connect_command} » au {triage.number}. Ensuite, écris-lui ce que tu veux."
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
    imessage = config.channels.imessage
    if imessage.enabled:
        try:
            imessage.api_key.resolve()
            print(f"inkbox:    api key present, identity @{imessage.identity or '?'}")
        except LookupError as exc:
            print(f"inkbox:    {exc}")
    else:
        print("inkbox:    disabled")
    print(
        "calls:     on"
        if config.voice.enabled
        else "calls:     off (set enabled = true in [voice], then rerun `orion inkbox setup`)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orion")
    parser.add_argument("--config", type=Path, default=None, help="Path to orion.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="Print the version")
    sub.add_parser("doctor", help="Check configuration and storage")
    sub.add_parser("chat", help="Talk to Orion in the terminal")
    sub.add_parser("serve", help="Run Orion: iMessage channel and initiative engine")
    inkbox = sub.add_parser("inkbox", help="Inkbox (iMessage) administration")
    inkbox_sub = inkbox.add_subparsers(dest="inkbox_command", required=True)
    setup = inkbox_sub.add_parser("setup", help="Create Orion's identity and show how to connect")
    setup.add_argument("--webhook-url", default=None, help="Public HTTPS URL for webhook mode")
    inkbox_sub.add_parser("calls", help="Recent calls on Orion's line, with why they ended")
    sub.add_parser("memory", help="Show what Orion remembers (also writes profile.md)")
    forget = sub.add_parser("forget", help="Forget everything that came from a source")
    forget.add_argument("source", help="Source prefix, e.g. 'terminal:' or 'mail:abc'")
    forget.add_argument("--reason", default="owner request")
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
    if args.command == "inkbox":
        if args.inkbox_command == "calls":
            return asyncio.run(_inkbox_calls(config))
        return asyncio.run(_inkbox_setup(config, args.webhook_url))
    if args.command == "serve":
        try:
            return asyncio.run(_serve(config))
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
