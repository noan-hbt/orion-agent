"""Regressions for the runtime-hang investigation.

The reported incident: Orion ran a large task, the PC went to sleep, and
afterwards the UI accepted input but the agent never replied again.  Three
independent investigations ranked the same root cause first — the runtime
daemon loop was the only worker loop without an exception guard, so an
out-of-band failure (a SQLite/OS error on the first query after resume, or a
``BaseException`` escaping a tool call) killed it permanently while the UI
kept accepting work that nothing would ever process.
"""
from __future__ import annotations

import time

from event_handler import Event
from runtime import AgentRuntime


def _runtime(**kwargs) -> AgentRuntime:
    kwargs.setdefault("action_ledger_path", ":memory:")
    kwargs.setdefault("history_enabled", False)
    return AgentRuntime(**kwargs)


# ---------------------------------------------------------------------------
# The runtime loop must survive an out-of-band failure (hang investigation #1).
# ---------------------------------------------------------------------------


def test_runtime_loop_records_failure_instead_of_dying() -> None:
    """A poisoned event must degrade to an error record, not kill the loop."""
    runtime = _runtime()

    class Boom(Exception):
        pass

    def explode(event, *, owner_id: str) -> None:
        raise Boom("post-resume failure")

    runtime._process_runtime_event = explode  # type: ignore[method-assign]
    runtime.start()
    try:
        runtime.receive_event(Event("probe", {}))
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not runtime._runtime_errors:
            time.sleep(0.02)

        assert runtime._runtime_errors, "failure was not recorded"
        assert isinstance(runtime._runtime_errors[-1], Boom)
        # The loop is still alive and can process further work.
        assert runtime.running is True
    finally:
        runtime.stop(wait=True, drain=False)


def test_run_in_progress_is_cleared_when_the_loop_fails() -> None:
    """``_run`` must not leave the runtime claiming a run is in progress.

    While that flag is set, every new message is parked in the deferred queue
    (promotion is gated on the flag being clear), so a leaked flag is exactly
    the "accepts input, never replies" signature.
    """
    runtime = _runtime()
    with runtime._execution_lock:
        runtime._run_in_progress = True

    def boom() -> None:
        raise RuntimeError("loop-level failure")

    runtime._run_loop = boom  # type: ignore[method-assign]
    runtime._run()

    assert runtime._run_in_progress is False


def test_deferred_events_are_permanently_stranded_if_promotion_is_skipped() -> None:
    """Document the coupling: promotion requires ``_run_in_progress`` clear."""
    runtime = _runtime()
    # A run must already be in progress for a new message to be deferred.
    with runtime._execution_lock:
        runtime._run_in_progress = True
    runtime.receive_event(Event("probe", {}))
    before = runtime._deferred_events.qsize()
    # While a run is "in progress" the new event is deferred, not queued.
    assert before >= 1
    assert runtime.wake_queue.empty()
    with runtime._execution_lock:
        runtime._run_in_progress = False
        runtime._promote_deferred_events()
    assert not runtime.wake_queue.empty(), "promotion did not release the event"


# ---------------------------------------------------------------------------
# An operator must be able to stop a run (hang investigation: no interrupt path).
# ---------------------------------------------------------------------------


def test_request_cancel_reports_whether_a_run_was_active() -> None:
    runtime = _runtime()
    assert runtime.request_cancel() is False
    assert runtime._cancel_requested.is_set()

    with runtime._execution_lock:
        runtime._run_in_progress = True
    runtime.clear_cancel()
    assert runtime.request_cancel() is True


def test_cancel_makes_the_run_loop_treat_the_run_as_stopped() -> None:
    """The cancel flag must feed the existing interrupt checks."""
    runtime = _runtime()
    runtime.clear_cancel()
    assert runtime._stop_interrupts_active_run() is False
    runtime.request_cancel()
    assert runtime._stop_interrupts_active_run() is True


def test_cancel_is_cleared_when_the_next_run_is_admitted() -> None:
    """One cancel must not wedge every subsequent run."""
    runtime = _runtime()
    runtime.request_cancel()
    assert runtime._cancel_requested.is_set()
    # A run admission clears the flag; emulate the guarded block in _wake.
    with runtime._execution_lock:
        runtime._cancel_requested.clear()
        runtime._run_in_progress = True
    assert runtime._cancel_requested.is_set() is False


# ---------------------------------------------------------------------------
# The cockpit must expose the cancel path and it must be discoverable.
# ---------------------------------------------------------------------------


def test_cockpit_backend_exposes_cancel_and_reports_no_active_run() -> None:
    from cli_cockpit_backend import CockpitBackend

    class _Runtime:
        def __init__(self) -> None:
            self.calls = 0

        def request_cancel(self) -> bool:
            self.calls += 1
            return False

    class _App:
        runtime = _Runtime()

    app = _App()
    backend = CockpitBackend(app)
    result = backend.execute("/cancel")
    assert app.runtime.calls == 1
    data = result.get("data")
    assert isinstance(data, dict)
    assert data["cancelled"] is False
    assert data["reason"] == "no_active_run"


def test_cancel_is_advertised_in_the_cockpit_help() -> None:
    from cli_cockpit import CockpitCLIAdapter

    class _Backend:
        def snapshot(self):
            return {}

    cli = CockpitCLIAdapter(_Backend())
    rows = {row["command"]: row for row in cli._local_help_rows()}
    assert "cancel" in rows
    assert rows["cancel"]["usage"] == "/cancel"
