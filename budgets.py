"""Optional bounded budgets and resilience primitives for Orion.

All classes are opt-in and thread-safe; callers can use ``BudgetTracker`` as
hooks around LLM/tool execution without coupling the runtime to this module.
"""
from __future__ import annotations
import random, threading, time
from dataclasses import dataclass

class BudgetExceeded(RuntimeError):
    """A configured execution budget was exceeded."""

@dataclass(frozen=True)
class BudgetLimits:
    max_duration: float | None = None
    max_turns: int | None = None
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    max_calls: int | None = None

class BudgetTracker:
    def __init__(self, limits: BudgetLimits | None = None):
        self.limits = limits or BudgetLimits(); self.started = time.monotonic()
        self.turns = self.tokens = self.calls = 0; self.cost_usd = 0.0
        self._lock = threading.Lock()
    def check(self):
        l=self.limits
        if l.max_duration is not None and time.monotonic()-self.started > l.max_duration: raise BudgetExceeded("Durée maximale dépassée")
        if l.max_turns is not None and self.turns >= l.max_turns: raise BudgetExceeded("Nombre maximal de tours dépassé")
        if l.max_tokens is not None and self.tokens >= l.max_tokens: raise BudgetExceeded("Budget de tokens dépassé")
        if l.max_cost_usd is not None and self.cost_usd >= l.max_cost_usd: raise BudgetExceeded("Budget coût dépassé")
        if l.max_calls is not None and self.calls >= l.max_calls: raise BudgetExceeded("Nombre maximal d'appels dépassé")
    def record(self, *, turns=0, tokens=0, cost_usd=0.0, calls=0):
        with self._lock:
            self.turns += turns; self.tokens += tokens; self.cost_usd += float(cost_usd); self.calls += calls; self.check()

class CircuitBreaker:
    def __init__(self, threshold=5, recovery=30.0): self.threshold=threshold; self.recovery=recovery; self.failures=0; self.opened=0.0
    def allow(self):
        if self.opened and time.monotonic()-self.opened < self.recovery: return False
        return True
    def success(self): self.failures=0; self.opened=0.0
    def failure(self):
        self.failures += 1
        if self.failures >= self.threshold: self.opened=time.monotonic()

class RateLimiter:
    def __init__(self, rate: float | None = None): self.rate=rate; self._next=0.0; self._lock=threading.Lock()
    def reserve(self) -> float:
        if not self.rate or self.rate <= 0: return 0.0
        with self._lock:
            now=time.monotonic(); delay=max(0.0,self._next-now); self._next=max(now,self._next)+1.0/self.rate
        return delay
    def wait(self):
        delay = self.reserve()
        if delay: time.sleep(delay)

def jittered_backoff(base: float, attempt: int, cap: float = 60.0) -> float:
    return min(cap, max(0.0, base * (2 ** attempt)) * random.uniform(0.8, 1.2))
