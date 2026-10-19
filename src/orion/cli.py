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
from orion.observability.logging import configure_logging


async def _chat(config: OrionConfig) -> int:
    # Imported lazily: `orion version` and `orion doctor` must work without network deps loaded.
    from orion.channels.terminal import TerminalApprover, run_terminal
    from orion.context.compiler import ContextCompiler
    from orion.guardian.policy import Guardian
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
    try:
        async with SqliteJournal(config.journal_path, HybridClock(config.instance)) as journal:
            orion = Orion(
                journal=journal,
                provider=provider,
                registry=ToolRegistry(builtin_tools()),
                guardian=Guardian(TerminalApprover()),
                compiler=ContextCompiler(
                    read_resource("constitution.md"), config.agent.max_window_tokens
                ),
                agent=config.agent,
            )
            await run_terminal(orion)
    finally:
        await provider.aclose()
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
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orion")
    parser.add_argument("--config", type=Path, default=None, help="Path to orion.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="Print the version")
    sub.add_parser("doctor", help="Check configuration and storage")
    sub.add_parser("chat", help="Talk to Orion in the terminal")
    args = parser.parse_args(argv)

    if args.command == "version":
        print(__version__)
        return 0

    config = load_config(args.config)
    configure_logging(config.logging.level, config.logging.json_output)
    if args.command == "doctor":
        return asyncio.run(_doctor(config))
    if args.command == "chat":
        try:
            return asyncio.run(_chat(config))
        except KeyboardInterrupt:
            return 130
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
