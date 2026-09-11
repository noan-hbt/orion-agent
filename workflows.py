"""Small durable sequential workflow engine with approval pauses.

Action steps use a durable claim before invoking external code.  A claim is
never automatically re-issued after its lease expires: because the process may
have died after performing the side effect but before committing completion,
the step becomes ``uncertain`` and requires explicit reconciliation.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable

from approvals import ApprovalStore


class WorkflowEngine:
    def __init__(
        self,
        path: str = "data/workflows.sqlite3",
        approvals: ApprovalStore | None = None,
        *,
        claim_lease_seconds: float = 60.0,
    ) -> None:
        self.path = path
        self.approvals = approvals or ApprovalStore()
        self.claim_lease_seconds = max(0.001, float(claim_lease_seconds))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with self._db() as db:
            # Keep the original table unchanged for existing databases.
            db.execute(
                "CREATE TABLE IF NOT EXISTS workflow_runs ("
                "id TEXT PRIMARY KEY, name TEXT, definition TEXT, state TEXT, "
                "step INTEGER, context TEXT, approval_id TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS workflow_step_state ("
                "run_id TEXT NOT NULL, step INTEGER NOT NULL, state TEXT NOT NULL, "
                "claim_token TEXT, lease_until REAL, updated_at REAL NOT NULL, "
                "reason TEXT, PRIMARY KEY(run_id, step))"
            )

    @contextmanager
    def _db(self, *, immediate: bool = False):
        db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        try:
            if immediate:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def start(
        self,
        name: str,
        steps: list[dict[str, Any]],
        context: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        rid = run_id or str(uuid.uuid4())
        data = (
            rid,
            name,
            json.dumps(steps),
            "running",
            0,
            json.dumps(context or {}),
            None,
        )
        with self._db(immediate=True) as db:
            # Reusing a run id historically replaced the workflow.  Clear any
            # claims from the previous definition so that behaviour stays true.
            db.execute("DELETE FROM workflow_step_state WHERE run_id=?", (rid,))
            db.execute("INSERT OR REPLACE INTO workflow_runs VALUES (?,?,?,?,?,?,?)", data)
        return self.resume(rid)

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM workflow_runs WHERE id=?", (run_id,)
            ).fetchone()
            if not row:
                return None
            step_state = db.execute(
                "SELECT state, reason FROM workflow_step_state WHERE run_id=? AND step=?",
                (run_id, row[4]),
            ).fetchone()
        result = {
            "id": row[0],
            "name": row[1],
            "steps": json.loads(row[2]),
            "state": row[3],
            "step": row[4],
            "context": json.loads(row[5]),
            "approval_id": row[6],
            "step_state": step_state[0] if step_state else None,
        }
        if result["state"] == "uncertain":
            result["reconciliation"] = {
                "required": True,
                "step": result["step"],
                "reason": step_state[1] if step_state else "unknown",
            }
        return result

    def _advance_step(self, run_id: str, expected_step: int) -> None:
        with self._db(immediate=True) as db:
            db.execute(
                "UPDATE workflow_runs SET state='running', step=step+1 "
                "WHERE id=? AND step=? AND state!='uncertain'",
                (run_id, expected_step),
            )

    def _set_state(self, run_id: str, state: str) -> None:
        with self._db(immediate=True) as db:
            db.execute("UPDATE workflow_runs SET state=? WHERE id=?", (state, run_id))

    def _clear_approval(self, run_id: str, approval_id: str) -> None:
        with self._db(immediate=True) as db:
            row = db.execute(
                "SELECT step FROM workflow_runs WHERE id=? AND approval_id=?",
                (run_id, approval_id),
            ).fetchone()
            if row is None:
                return
            step = int(row[0])
            now = time.time()
            db.execute(
                "INSERT OR IGNORE INTO workflow_step_state "
                "(run_id,step,state,claim_token,lease_until,updated_at,reason) "
                "VALUES (?,?, 'approved', NULL, NULL, ?, NULL)",
                (run_id, step, now),
            )
            db.execute(
                "UPDATE workflow_runs SET state='running', approval_id=NULL "
                "WHERE id=? AND approval_id=?",
                (run_id, approval_id),
            )

    def _ensure_approval(self, run: dict[str, Any], step: dict[str, Any]) -> str:
        """Create at most one approval for the current step across resume calls."""
        with self._db(immediate=True) as db:
            row = db.execute(
                "SELECT step, approval_id FROM workflow_runs WHERE id=?", (run["id"],)
            ).fetchone()
            if row is None:
                raise KeyError(run["id"])
            if row[0] != run["step"]:
                return str(row[1] or "")
            if row[1]:
                return str(row[1])
            approval_config = step["approval"]
            approval = self.approvals.create(
                approval_config.get("requester", "workflow"),
                approval_config.get("scope", run["name"]),
                approval_config.get("payload", {}),
                approval_config.get("correlation_id"),
            )
            db.execute(
                "UPDATE workflow_runs SET approval_id=?, state='waiting_approval' "
                "WHERE id=? AND step=? AND approval_id IS NULL",
                (approval["id"], run["id"], run["step"]),
            )
            return str(approval["id"])

    def _claim_action(self, run_id: str, step: int) -> tuple[str, str | None]:
        """Claim an action or report why it must not be executed.

        The claim is committed before the executor runs.  An expired claim is
        deliberately converted to ``uncertain`` instead of being stolen.
        """
        now = time.time()
        token = uuid.uuid4().hex
        with self._db(immediate=True) as db:
            run = db.execute(
                "SELECT step, state FROM workflow_runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run[1] == "uncertain":
                return "uncertain", None
            if run[0] != step:
                return "advanced", None

            existing = db.execute(
                "SELECT state, lease_until FROM workflow_step_state "
                "WHERE run_id=? AND step=?",
                (run_id, step),
            ).fetchone()
            if existing:
                state, lease_until = existing
                if state == "approved":
                    db.execute(
                        "UPDATE workflow_step_state SET state='executing', claim_token=?, "
                        "lease_until=?, updated_at=?, reason=NULL WHERE run_id=? AND step=?",
                        (token, now + self.claim_lease_seconds, now, run_id, step),
                    )
                    return "claimed", token
                if state == "completed":
                    db.execute(
                        "UPDATE workflow_runs SET step=step+1, state='running' "
                        "WHERE id=? AND step=?",
                        (run_id, step),
                    )
                    return "completed", None
                if state == "uncertain":
                    db.execute(
                        "UPDATE workflow_runs SET state='uncertain' WHERE id=?", (run_id,)
                    )
                    return "uncertain", None
                if state == "executing":
                    if lease_until is not None and float(lease_until) > now:
                        return "busy", None
                    db.execute(
                        "UPDATE workflow_step_state SET state='uncertain', claim_token=NULL, "
                        "lease_until=NULL, updated_at=?, reason='claim_expired' "
                        "WHERE run_id=? AND step=? AND state='executing'",
                        (now, run_id, step),
                    )
                    db.execute(
                        "UPDATE workflow_runs SET state='uncertain' WHERE id=?", (run_id,)
                    )
                    return "uncertain", None

            db.execute(
                "INSERT INTO workflow_step_state "
                "(run_id,step,state,claim_token,lease_until,updated_at,reason) "
                "VALUES (?,?, 'executing', ?, ?, ?, NULL)",
                (run_id, step, token, now + self.claim_lease_seconds, now),
            )
            return "claimed", token

    def _complete_claim(self, run_id: str, step: int, token: str) -> None:
        """Atomically mark the claimed effect complete and advance the run."""
        now = time.time()
        with self._db(immediate=True) as db:
            changed = db.execute(
                "UPDATE workflow_step_state SET state='completed', claim_token=NULL, "
                "lease_until=NULL, updated_at=?, reason=NULL "
                "WHERE run_id=? AND step=? AND state='executing' AND claim_token=?",
                (now, run_id, step, token),
            ).rowcount
            if not changed:
                raise RuntimeError("workflow action claim lost; reconciliation required")
            db.execute(
                "UPDATE workflow_runs SET step=step+1, state='running' "
                "WHERE id=? AND step=?",
                (run_id, step),
            )

    def _mark_uncertain(self, run_id: str, step: int, reason: str) -> None:
        now = time.time()
        with self._db(immediate=True) as db:
            db.execute(
                "UPDATE workflow_step_state SET state='uncertain', claim_token=NULL, "
                "lease_until=NULL, updated_at=?, reason=? "
                "WHERE run_id=? AND step=? AND state!='completed'",
                (now, reason, run_id, step),
            )
            db.execute(
                "UPDATE workflow_runs SET state='uncertain' WHERE id=? AND step=?",
                (run_id, step),
            )

    def reconcile(self, run_id: str, *, completed: bool) -> dict[str, Any]:
        """Explicitly resolve an uncertain side effect.

        ``completed=True`` means an operator verified the effect happened and
        advances the workflow. ``completed=False`` means it was verified absent
        and makes the step claimable again on a later explicit ``resume``.
        """
        now = time.time()
        with self._db(immediate=True) as db:
            run = db.execute(
                "SELECT step, state, definition FROM workflow_runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            step, state = int(run[0]), str(run[1])
            if state != "uncertain":
                raise ValueError("workflow run is not awaiting reconciliation")
            row = db.execute(
                "SELECT state FROM workflow_step_state WHERE run_id=? AND step=?",
                (run_id, step),
            ).fetchone()
            if row is None or row[0] != "uncertain":
                raise ValueError("workflow step is not awaiting reconciliation")
            if completed:
                db.execute(
                    "UPDATE workflow_step_state SET state='completed', updated_at=?, reason=NULL "
                    "WHERE run_id=? AND step=?",
                    (now, run_id, step),
                )
                db.execute(
                    "UPDATE workflow_runs SET step=step+1, state='running' WHERE id=?",
                    (run_id,),
                )
            else:
                definition = json.loads(run[2])
                approval_was_granted = (
                    0 <= step < len(definition) and bool(definition[step].get("approval"))
                )
                if approval_was_granted:
                    db.execute(
                        "UPDATE workflow_step_state SET state='approved', claim_token=NULL, "
                        "lease_until=NULL, updated_at=?, reason=NULL WHERE run_id=? AND step=?",
                        (now, run_id, step),
                    )
                else:
                    db.execute(
                        "DELETE FROM workflow_step_state WHERE run_id=? AND step=?",
                        (run_id, step),
                    )
                db.execute(
                    "UPDATE workflow_runs SET state='running' WHERE id=?", (run_id,)
                )
        result = self.get(run_id)
        assert result is not None
        return result

    def resume(self, run_id: str, executor: Callable | None = None) -> dict[str, Any]:
        while True:
            run = self.get(run_id)
            if not run:
                raise KeyError(run_id)
            if run["state"] in {"completed", "rejected", "uncertain"}:
                return run
            if run["step"] >= len(run["steps"]):
                self._set_state(run_id, "completed")
                completed = self.get(run_id)
                assert completed is not None
                return completed

            step_index = run["step"]
            step = run["steps"][step_index]
            cond = step.get("condition", True)
            if callable(cond) and not cond(run["context"]):
                self._advance_step(run_id, step_index)
                continue
            if isinstance(cond, str) and not bool(run["context"].get(cond)):
                self._advance_step(run_id, step_index)
                continue

            approval_already_granted = run["step_state"] in {
                "approved", "executing", "completed", "uncertain"
            }
            if (
                step.get("approval")
                and run["approval_id"] is None
                and not approval_already_granted
            ):
                self._ensure_approval(run, step)
                waiting = self.get(run_id)
                assert waiting is not None
                return waiting
            if run["approval_id"]:
                approval = self.approvals.get(run["approval_id"])
                if approval["status"] == "pending":
                    return run
                if approval["status"] != "approved":
                    self._set_state(run_id, "rejected")
                    rejected = self.get(run_id)
                    assert rejected is not None
                    return rejected
                self._clear_approval(run_id, run["approval_id"])
                # Approval authorizes the current step. Continue processing
                # this step instead of looping and creating a new approval.
                run["approval_id"] = None

            if executor and step.get("action"):
                claim_state, token = self._claim_action(run_id, step_index)
                if claim_state in {"busy", "uncertain"}:
                    current = self.get(run_id)
                    assert current is not None
                    return current
                if claim_state in {"advanced", "completed"}:
                    continue
                assert token is not None
                try:
                    executor(step["action"], run["context"])
                except BaseException as exc:
                    self._mark_uncertain(
                        run_id, step_index, f"executor_raised:{type(exc).__name__}"
                    )
                    raise
                try:
                    self._complete_claim(run_id, step_index, token)
                except BaseException as exc:
                    # The effect returned successfully but its durable completion
                    # is not proven. Never retry it automatically.
                    self._mark_uncertain(
                        run_id, step_index, f"commit_failed_after_effect:{type(exc).__name__}"
                    )
                    raise
                continue

            # Preserve the legacy behaviour: action-only steps are skipped when
            # resume is called without an executor.
            self._advance_step(run_id, step_index)

    def _save(self, run: dict[str, Any]) -> None:
        """Backward-compatible internal persistence helper."""
        with self._db(immediate=True) as db:
            db.execute(
                "UPDATE workflow_runs SET state=?,step=?,context=?,approval_id=? WHERE id=?",
                (
                    run["state"],
                    run["step"],
                    json.dumps(run["context"]),
                    run["approval_id"],
                    run["id"],
                ),
            )
