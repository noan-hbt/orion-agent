"""Supervision of long-running services, Erlang-style.

A service that crashes is logged and restarted with backoff. If it keeps crashing past its
restart budget, the supervisor escalates: it calls the escalation hook (to warn the owner) and
stops the whole tree, rather than limping along with a dead component. Nothing dies silently.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)

ServiceFn = Callable[[], Awaitable[None]]
EscalationHook = Callable[[str, BaseException], Awaitable[None]]


class ServiceGaveUpError(RuntimeError):
    """Raised when a service exhausts its restart budget."""

    def __init__(self, service: str, last_error: BaseException) -> None:
        super().__init__(f"service {service!r} exceeded its restart budget")
        self.service = service
        self.last_error = last_error


@dataclass(frozen=True)
class RestartPolicy:
    max_restarts: int = 5
    window_seconds: float = 60.0
    backoff_initial: float = 0.5
    backoff_max: float = 30.0


@dataclass
class _ServiceSpec:
    name: str
    fn: ServiceFn
    policy: RestartPolicy
    restarts: deque[float] = field(default_factory=deque[float])


async def _no_escalation(_service: str, _error: BaseException) -> None:
    return None


class Supervisor:
    def __init__(
        self,
        escalate: EscalationHook = _no_escalation,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._services: list[_ServiceSpec] = []
        self._escalate = escalate
        self._monotonic = monotonic

    def add(self, name: str, fn: ServiceFn, policy: RestartPolicy | None = None) -> None:
        if any(s.name == name for s in self._services):
            raise ValueError(f"service {name!r} already registered")
        self._services.append(_ServiceSpec(name, fn, policy or RestartPolicy()))

    async def run(self) -> None:
        """Run all services until cancelled, or until one gives up (then raise)."""
        async with asyncio.TaskGroup() as group:
            for spec in self._services:
                group.create_task(self._supervise(spec), name=f"supervise:{spec.name}")

    async def _supervise(self, spec: _ServiceSpec) -> None:
        backoff = spec.policy.backoff_initial
        while True:
            try:
                log.info("service.start", service=spec.name)
                await spec.fn()
                log.warning("service.returned", service=spec.name)
                error: BaseException = RuntimeError("service returned unexpectedly")
            except asyncio.CancelledError:
                log.info("service.cancelled", service=spec.name)
                raise
            except Exception as exc:
                log.exception("service.crashed", service=spec.name)
                error = exc

            now = self._monotonic()
            spec.restarts.append(now)
            while spec.restarts and now - spec.restarts[0] > spec.policy.window_seconds:
                spec.restarts.popleft()
            if len(spec.restarts) > spec.policy.max_restarts:
                log.error("service.gave_up", service=spec.name, restarts=len(spec.restarts))
                await self._escalate(spec.name, error)
                raise ServiceGaveUpError(spec.name, error) from error

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, spec.policy.backoff_max)
