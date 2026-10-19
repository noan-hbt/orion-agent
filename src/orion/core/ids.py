"""ULID identifiers: 48-bit millisecond timestamp + 80 random bits, Crockford base32.

ULIDs sort lexicographically by creation time, which keeps journal ids human-scannable and
index-friendly. Within one process, ids generated in the same millisecond stay monotonic.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RANDOM_BITS = 80
_RANDOM_MAX = (1 << _RANDOM_BITS) - 1


def _system_ms() -> int:
    return time.time_ns() // 1_000_000


def _encode(value: int, length: int) -> str:
    chars: list[str] = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        chars.append(_ALPHABET[rem])
    return "".join(reversed(chars))


class UlidGenerator:
    """Monotonic ULID generator. Thread-safe."""

    def __init__(self, clock_ms: Callable[[], int] | None = None) -> None:
        self._clock_ms = clock_ms or _system_ms
        self._lock = threading.Lock()
        self._last_ms = -1
        self._last_random = 0

    def new(self) -> str:
        with self._lock:
            now = self._clock_ms()
            if now <= self._last_ms:
                # Same (or earlier) millisecond: bump the random part to stay monotonic.
                if self._last_random == _RANDOM_MAX:
                    raise OverflowError("ULID random space exhausted within one millisecond")
                now = self._last_ms
                random_part = self._last_random + 1
            else:
                random_part = int.from_bytes(os.urandom(10), "big")
            self._last_ms = now
            self._last_random = random_part
        return _encode(now, 10) + _encode(random_part, 16)


_default = UlidGenerator()


def new_id() -> str:
    """Return a new process-wide monotonic ULID."""
    return _default.new()
