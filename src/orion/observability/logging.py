"""Structured logging setup. JSON in production, readable console output in development."""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        # Resolve stderr at log time, not at configure time, so redirected or replaced streams
        # (daemon log rotation, test capture) never leave loggers pointing at a closed file.
        logger_factory=_stderr_logger,
        cache_logger_on_first_use=False,
    )


def _stderr_logger(*_args: object) -> structlog.PrintLogger:
    return structlog.PrintLogger(file=sys.stderr)
