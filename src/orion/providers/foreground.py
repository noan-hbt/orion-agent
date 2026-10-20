"""Whether the owner is waiting for the model calls being made (D79).

A turn answering him sets it; everything Orion does on its own runs without it. The spending
ceiling (`orion.providers.spending`) never cuts a call made while he waits.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

_foreground: ContextVar[bool] = ContextVar("orion_foreground_spending", default=False)


@contextmanager
def foreground() -> Generator[None]:
    """Model calls made inside are for the owner, who is waiting: never cut."""
    token = _foreground.set(True)
    try:
        yield
    finally:
        _foreground.reset(token)


def waiting() -> bool:
    return _foreground.get()
