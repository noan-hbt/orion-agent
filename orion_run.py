"""Lance Orion comme processus long-vivant.

Usage : ``python orion_run.py --config orion.toml``.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

from orion_config import load_orion


def _report_error(event: object, error: Exception) -> None:
    event_id = getattr(event, "id", "unknown")
    event_type = getattr(event, "type", "unknown")
    print(
        f"[orion:error] event={event_type} id={event_id}: "
        f"{type(error).__name__}: {error}",
        file=sys.stderr,
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Lance Orion en mode continu")
    parser.add_argument("--config", type=Path, default=Path("orion.toml"))
    args = parser.parse_args()

    shutdown = threading.Event()
    for signal_name in ("SIGINT", "SIGTERM"):
        signal_value = getattr(signal, signal_name, None)
        if signal_value is not None:
            signal.signal(signal_value, lambda *_: shutdown.set())

    application = load_orion(args.config)
    # Les workers sont asynchrones : sans callbacks, une erreur OpenRouter ou
    # de channel peut sinon laisser uniquement le prompt CLI visible.
    application.events.on_error = _report_error
    application.runtime.on_error = _report_error
    print(
        f"Orion demarre (channels: {', '.join(application.channels.adapters) or 'aucun'})",
        flush=True,
    )
    application.run_forever(shutdown)


if __name__ == "__main__":
    main()
