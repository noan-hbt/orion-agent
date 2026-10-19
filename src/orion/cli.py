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
    args = parser.parse_args(argv)

    if args.command == "version":
        print(__version__)
        return 0

    config = load_config(args.config)
    configure_logging(config.logging.level, config.logging.json_output)
    if args.command == "doctor":
        return asyncio.run(_doctor(config))
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
