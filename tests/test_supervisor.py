from __future__ import annotations

import asyncio

import pytest

from orion.core.supervisor import RestartPolicy, ServiceGaveUpError, Supervisor

FAST = RestartPolicy(max_restarts=3, window_seconds=60, backoff_initial=0, backoff_max=0)


async def test_crashing_service_is_restarted_then_recovers() -> None:
    attempts = 0
    recovered = asyncio.Event()

    async def flaky() -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("transient")
        recovered.set()
        await asyncio.Event().wait()  # run forever

    supervisor = Supervisor()
    supervisor.add("flaky", flaky, FAST)
    task = asyncio.create_task(supervisor.run())
    await asyncio.wait_for(recovered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert attempts == 3


async def test_service_exceeding_budget_escalates_and_stops_tree() -> None:
    escalations: list[tuple[str, str]] = []
    sibling_cancelled = asyncio.Event()

    async def broken() -> None:
        raise RuntimeError("always")

    async def healthy() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise

    async def escalate(service: str, error: BaseException) -> None:
        escalations.append((service, str(error)))

    supervisor = Supervisor(escalate=escalate)
    supervisor.add("broken", broken, FAST)
    supervisor.add("healthy", healthy, FAST)
    with pytest.raises(ExceptionGroup) as info:
        await asyncio.wait_for(supervisor.run(), timeout=2)
    assert info.group_contains(ServiceGaveUpError)
    assert escalations == [("broken", "always")]
    assert sibling_cancelled.is_set()


async def test_service_returning_counts_as_failure() -> None:
    calls = 0

    async def returns() -> None:
        nonlocal calls
        calls += 1

    supervisor = Supervisor()
    supervisor.add("returns", returns, FAST)
    with pytest.raises(ExceptionGroup):
        await asyncio.wait_for(supervisor.run(), timeout=2)
    assert calls == FAST.max_restarts + 1


def test_duplicate_service_names_rejected() -> None:
    async def noop() -> None:
        return None

    supervisor = Supervisor()
    supervisor.add("a", noop)
    with pytest.raises(ValueError):
        supervisor.add("a", noop)
