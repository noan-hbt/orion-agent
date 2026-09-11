from __future__ import annotations

import threading
import sqlite3

import pytest

from approvals import ApprovalStore
from workflows import WorkflowEngine


def _engine(tmp_path, **kwargs):
    approvals = ApprovalStore(str(tmp_path / "approvals.sqlite3"))
    return WorkflowEngine(
        str(tmp_path / "workflows.sqlite3"), approvals=approvals, **kwargs
    )


def test_concurrent_resume_never_executes_same_action_together(tmp_path):
    engine = _engine(tmp_path)
    run = engine.start(
        "deploy",
        [{"approval": {"scope": "deploy"}, "action": "publish"}],
    )
    engine.approvals.approve(run["approval_id"])

    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    first_result: list[dict] = []

    def executor(action, _context):
        calls.append(action)
        entered.set()
        assert release.wait(2)

    def first_resume():
        first_result.append(engine.resume(run["id"], executor=executor))

    thread = threading.Thread(target=first_resume)
    thread.start()
    assert entered.wait(2)

    concurrent = engine.resume(run["id"], executor=executor)
    assert concurrent["state"] == "running"
    assert concurrent["step_state"] == "executing"
    assert calls == ["publish"]

    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert first_result[0]["state"] == "completed"
    assert calls == ["publish"]


def test_crash_after_effect_before_commit_becomes_uncertain_and_does_not_retry(
    tmp_path, monkeypatch
):
    engine = _engine(tmp_path)
    run = engine.start(
        "deploy",
        [{"approval": {"scope": "deploy"}, "action": "publish"}],
    )
    engine.approvals.approve(run["approval_id"])
    effects: list[str] = []

    def fail_commit(*_args, **_kwargs):
        raise RuntimeError("simulated crash before durable completion")

    monkeypatch.setattr(engine, "_complete_claim", fail_commit)
    with pytest.raises(RuntimeError):
        engine.resume(run["id"], executor=lambda action, _context: effects.append(action))

    uncertain = engine.get(run["id"])
    assert uncertain["state"] == "uncertain"
    assert uncertain["step_state"] == "uncertain"
    assert uncertain["reconciliation"]["required"] is True
    assert uncertain["reconciliation"]["reason"] == "commit_failed_after_effect:RuntimeError"
    assert effects == ["publish"]

    # A later resume must not blindly perform the external effect again.
    again = engine.resume(
        run["id"], executor=lambda action, _context: effects.append(action)
    )
    assert again["state"] == "uncertain"
    assert effects == ["publish"]


def test_expired_claim_requires_reconciliation_instead_of_retry(tmp_path):
    engine = _engine(tmp_path, claim_lease_seconds=0.001)
    run = engine.start(
        "deploy",
        [{"approval": {"scope": "deploy"}, "action": "publish"}],
    )
    engine.approvals.approve(run["approval_id"])
    # Consume the approved pause and durably claim the action as if another
    # process then died before invoking/committing the executor.
    engine._clear_approval(run["id"], run["approval_id"])
    state, token = engine._claim_action(run["id"], 0)
    assert state == "claimed" and token

    import time
    time.sleep(0.01)
    effects: list[str] = []
    uncertain = engine.resume(
        run["id"], executor=lambda action, _context: effects.append(action)
    )
    assert uncertain["state"] == "uncertain"
    assert uncertain["reconciliation"]["reason"] == "claim_expired"
    assert effects == []

    # Explicitly certifying that the effect did not happen re-enables a later
    # retry; this is an operator decision, not an automatic one.
    reconciled = engine.reconcile(run["id"], completed=False)
    assert reconciled["state"] == "running"
    done = engine.resume(
        run["id"], executor=lambda action, _context: effects.append(action)
    )
    assert done["state"] == "completed"
    assert effects == ["publish"]


def test_approval_flow_still_pauses_then_executes_once(tmp_path):
    engine = _engine(tmp_path)
    run = engine.start(
        "deploy",
        [{"approval": {"requester": "ci", "scope": "deploy"}}, {"action": "finish"}],
    )
    assert run["state"] == "waiting_approval"

    engine.approvals.approve(run["approval_id"], decided_by="owner")
    actions: list[str] = []
    done = engine.resume(run["id"], executor=lambda action, _context: actions.append(action))

    assert done["state"] == "completed"
    assert actions == ["finish"]


def test_existing_workflow_runs_schema_is_upgraded_without_rewrite(tmp_path):
    path = tmp_path / "workflows.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE workflow_runs (id TEXT PRIMARY KEY, name TEXT, definition TEXT, "
            "state TEXT, step INTEGER, context TEXT, approval_id TEXT)"
        )
        db.execute(
            "INSERT INTO workflow_runs VALUES (?,?,?,?,?,?,?)",
            ("legacy", "old", "[]", "running", 0, "{}", None),
        )

    engine = WorkflowEngine(str(path), approvals=ApprovalStore(str(tmp_path / "approvals.sqlite3")))
    run = engine.resume("legacy")

    assert run["state"] == "completed"
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_step_state'"
        ).fetchone()
