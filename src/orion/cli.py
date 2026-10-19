"""Command-line entry point. For now: `orion version` and `orion doctor`."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from orion import __version__
from orion.config import OrionConfig, load_config
from orion.core.hlc import HybridClock
from orion.core.journal import SqliteJournal
from orion.memory.store import MemoryStore
from orion.observability.logging import configure_logging


async def _chat(config: OrionConfig) -> int:
    # Imported lazily: `orion version` and `orion doctor` must work without network deps loaded.
    from orion.channels.terminal import TerminalApprover, run_terminal
    from orion.context.compiler import ContextCompiler
    from orion.guardian.policy import Guardian
    from orion.memory.extractor import MemoryExtractor
    from orion.memory.service import MemoryService
    from orion.memory.tools import memory_tools
    from orion.providers.openrouter import OpenRouterProvider
    from orion.resources import read_resource
    from orion.runtime import Orion
    from orion.tools.base import ToolRegistry
    from orion.tools.builtin import builtin_tools

    try:
        api_key = config.providers.openrouter.api_key.resolve()
    except LookupError as exc:
        print(f"Impossible de démarrer : {exc}")
        return 3
    provider = OpenRouterProvider(api_key.get_secret_value(), config.providers.openrouter.base_url)
    store = MemoryStore(config.memory_path)
    await store.open()
    try:
        async with SqliteJournal(config.journal_path, HybridClock(config.instance)) as journal:
            memory = MemoryService(journal, store)
            orion = Orion(
                journal=journal,
                provider=provider,
                registry=ToolRegistry([*builtin_tools(), *memory_tools(memory)]),
                guardian=Guardian(TerminalApprover()),
                compiler=ContextCompiler(
                    read_resource("constitution.md"), config.agent.max_window_tokens
                ),
                agent=config.agent,
                memory=memory,
                extractor=MemoryExtractor(provider, memory, config.agent.extractor_model),
            )
            await run_terminal(orion)
    finally:
        await store.close()
        await provider.aclose()
    return 0


async def _memory(config: OrionConfig) -> int:
    from orion.memory.render import profile_markdown
    from orion.memory.service import MemoryService

    store = MemoryStore(config.memory_path)
    await store.open()
    try:
        async with SqliteJournal(config.journal_path, HybridClock(config.instance)) as journal:
            await MemoryService(journal, store).sync()
            text = profile_markdown(await store.facts(), await store.commitments())
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
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orion")
    parser.add_argument("--config", type=Path, default=None, help="Path to orion.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="Print the version")
    sub.add_parser("doctor", help="Check configuration and storage")
    sub.add_parser("chat", help="Talk to Orion in the terminal")
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
    if args.command == "chat":
        try:
            return asyncio.run(_chat(config))
        except KeyboardInterrupt:
            return 130
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
