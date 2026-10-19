"""Hybrid logical clock (Kulkarni et al., 2014).

Timestamps combine physical milliseconds with a logical counter, so they stay close to wall
time while preserving causality across instances whose clocks disagree. The string form sorts
lexicographically in timestamp order, which lets SQLite order events by it directly.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import total_ordering

_WALL_DIGITS = 15
_COUNTER_DIGITS = 6
_MAX_COUNTER = 16**_COUNTER_DIGITS - 1


@total_ordering
@dataclass(frozen=True, slots=True)
class Timestamp:
    wall_ms: int
    counter: int
    node: str

    def encode(self) -> str:
        return f"{self.wall_ms:0{_WALL_DIGITS}d}.{self.counter:0{_COUNTER_DIGITS}x}.{self.node}"

    @classmethod
    def decode(cls, value: str) -> Timestamp:
        wall, counter, node = value.split(".", 2)
        return cls(int(wall), int(counter, 16), node)

    def _key(self) -> tuple[int, int, str]:
        return (self.wall_ms, self.counter, self.node)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Timestamp):
            return NotImplemented
        return self._key() < other._key()

    def __str__(self) -> str:
        return self.encode()


def _system_ms() -> int:
    return time.time_ns() // 1_000_000


class HybridClock:
    """Thread-safe HLC for one node (one Orion instance)."""

    def __init__(self, node: str, clock_ms: Callable[[], int] | None = None) -> None:
        if not node or "." in node:
            raise ValueError("node id must be non-empty and must not contain '.'")
        self._node = node
        self._clock_ms = clock_ms or _system_ms
        self._lock = threading.Lock()
        self._wall = 0
        self._counter = 0

    @property
    def node(self) -> str:
        return self._node

    def now(self) -> Timestamp:
        """Timestamp a local or send event."""
        with self._lock:
            physical = self._clock_ms()
            if physical > self._wall:
                self._wall, self._counter = physical, 0
            else:
                self._advance_counter()
            return Timestamp(self._wall, self._counter, self._node)

    def observe(self, remote: Timestamp) -> Timestamp:
        """Merge a timestamp received from another node, then timestamp the receive event."""
        with self._lock:
            physical = self._clock_ms()
            wall = max(self._wall, remote.wall_ms, physical)
            if wall == self._wall == remote.wall_ms:
                counter = max(self._counter, remote.counter) + 1
            elif wall == self._wall:
                counter = self._counter + 1
            elif wall == remote.wall_ms:
                counter = remote.counter + 1
            else:
                counter = 0
            if counter > _MAX_COUNTER:
                raise OverflowError("HLC counter overflow")
            self._wall, self._counter = wall, counter
            return Timestamp(self._wall, self._counter, self._node)

    def _advance_counter(self) -> None:
        if self._counter >= _MAX_COUNTER:
            raise OverflowError("HLC counter overflow")
        self._counter += 1
