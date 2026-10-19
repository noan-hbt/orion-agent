from __future__ import annotations

import pytest

from orion.core.hlc import HybridClock, Timestamp
from orion.core.ids import UlidGenerator, new_id


def test_ulid_shape_and_monotonic_within_same_ms() -> None:
    gen = UlidGenerator(clock_ms=lambda: 1_700_000_000_000)
    ids = [gen.new() for _ in range(1000)]
    assert all(len(i) == 26 for i in ids)
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_ulid_sorts_by_time() -> None:
    times = iter([1_000, 2_000])
    gen = UlidGenerator(clock_ms=lambda: next(times))
    first, second = gen.new(), gen.new()
    assert first < second


def test_new_id_is_unique() -> None:
    assert new_id() != new_id()


def test_hlc_monotonic_when_wall_clock_stalls_or_goes_back() -> None:
    times = iter([100, 100, 90, 90, 150])
    clock = HybridClock("a", clock_ms=lambda: next(times))
    stamps = [clock.now() for _ in range(5)]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == 5
    assert stamps[-1] == Timestamp(150, 0, "a")


def test_hlc_observe_preserves_causality_with_skewed_remote() -> None:
    local = HybridClock("a", clock_ms=lambda: 100)
    remote_stamp = Timestamp(5_000, 3, "b")  # remote clock is far ahead
    received = local.observe(remote_stamp)
    assert received > remote_stamp
    assert local.now() > received


def test_hlc_encoding_round_trips_and_sorts_lexicographically() -> None:
    a = Timestamp(42, 1, "node-a")
    b = Timestamp(42, 2, "node-a")
    c = Timestamp(1_000, 0, "node-a")
    assert Timestamp.decode(a.encode()) == a
    assert sorted([c.encode(), b.encode(), a.encode()]) == [a.encode(), b.encode(), c.encode()]


@pytest.mark.parametrize("node", ["", "has.dot"])
def test_hlc_rejects_bad_node_ids(node: str) -> None:
    with pytest.raises(ValueError):
        HybridClock(node)
