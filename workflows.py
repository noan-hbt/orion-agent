"""Small durable sequential workflow engine with approval pauses."""
from __future__ import annotations
import json, os, sqlite3, uuid
from contextlib import contextmanager
from typing import Any, Callable
from approvals import ApprovalStore

class WorkflowEngine:
    def __init__(self, path="data/workflows.sqlite3", approvals: ApprovalStore | None = None):
        self.path=path; self.approvals=approvals or ApprovalStore()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS workflow_runs (id TEXT PRIMARY KEY, name TEXT, definition TEXT, state TEXT, step INTEGER, context TEXT, approval_id TEXT)")
    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path)
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    def start(self, name: str, steps: list[dict[str, Any]], context: dict[str, Any] | None = None, run_id=None):
        rid=run_id or str(uuid.uuid4()); data=(rid,name,json.dumps(steps),"running",0,json.dumps(context or {}),None)
        with self._db() as db: db.execute("INSERT OR REPLACE INTO workflow_runs VALUES (?,?,?,?,?,?,?)",data)
        return self.resume(rid)
    def get(self, run_id):
        with self._db() as db:
            row=db.execute("SELECT * FROM workflow_runs WHERE id=?",(run_id,)).fetchone()
        if not row: return None
        return {"id":row[0],"name":row[1],"steps":json.loads(row[2]),"state":row[3],"step":row[4],"context":json.loads(row[5]),"approval_id":row[6]}
    def resume(self, run_id, executor: Callable | None = None):
        run=self.get(run_id)
        if not run: raise KeyError(run_id)
        while run["step"] < len(run["steps"]):
            step=run["steps"][run["step"]]
            cond=step.get("condition", True)
            if callable(cond) and not cond(run["context"]): run["step"]+=1; continue
            if isinstance(cond,str) and not bool(run["context"].get(cond)): run["step"]+=1; continue
            if step.get("approval") and run["approval_id"] is None:
                a=step["approval"]; approval=self.approvals.create(a.get("requester","workflow"),a.get("scope",run["name"]),a.get("payload",{}),a.get("correlation_id"))
                run["approval_id"]=approval["id"]; run["state"]="waiting_approval"; self._save(run); return run
            if run["approval_id"]:
                approval=self.approvals.get(run["approval_id"])
                if approval["status"] == "pending": return run
                if approval["status"] != "approved": run["state"]="rejected"; self._save(run); return run
                run["approval_id"]=None
            if executor and step.get("action"): executor(step["action"], run["context"])
            run["step"]+=1
        run["state"]="completed"; self._save(run); return run
    def _save(self,r):
        with self._db() as db: db.execute("UPDATE workflow_runs SET state=?,step=?,context=?,approval_id=? WHERE id=?",(r["state"],r["step"],json.dumps(r["context"]),r["approval_id"],r["id"]))
