"""Explicit, user-triggered mutating actions for the Orion cockpit.

The dispatcher is intentionally duck-typed so it can be used by the CLI and
by small runtime harnesses.  It never infers an approval or a capability that
the supplied application does not expose.
"""
from __future__ import annotations

from typing import Any, Mapping


def _get(app: Any, name: str, default: Any = None) -> Any:
    return app.get(name, default) if isinstance(app, Mapping) else getattr(app, name, default)


def _result(action: str, data: Any = None, error: str | None = None) -> dict[str, Any]:
    value = {"ok": error is None, "action": action, "data": data}
    if error is not None:
        value["error"] = error
    return value


def _service(app: Any, *names: str) -> Any:
    for name in names:
        value = _get(app, name)
        if value is not None:
            return value
    return None


def execute_action(application: Any, command: str, args: Any = None) -> dict[str, Any]:
    """Execute one explicit cockpit action.

    ``args`` may be a mapping (preferred) or a sequence of positional values.
    The returned envelope is stable: ``ok``, ``action``, ``data`` and, on
    failure, ``error``.
    """
    name = str(command or "").strip().lstrip("/").lower()
    if name.startswith("action "):
        name = name[7:].strip()
    raw = args if isinstance(args, Mapping) else {}
    positional = list(args or []) if not isinstance(args, Mapping) and args is not None else []
    def val(key: str, index: int = 0, default: Any = None):
        return raw.get(key, positional[index] if len(positional) > index else default)
    try:
        manager = _service(application, "subagent_manager", "subagents", "agent_manager")
        teams = _service(application, "team_bus", "teams")
        approvals = _service(application, "approval_store", "approvals")
        if name == "spawn":
            if manager is None or not callable(getattr(manager, "create_agent", None)):
                return _result(name, error="Service sous-agents indisponible")
            requested_tools = val("allowed_tools", default=None)
            data = manager.create_agent(name=str(val("name", 0, val("role", 0, "agent"))), description=str(val("description", 1, "")), model=val("model", 2), system_prompt=str(val("system_prompt", 3, "")), allowed_tools=None if requested_tools is None else list(requested_tools), capabilities=list(val("capabilities", default=[])), max_turns=int(val("max_turns", default=8)))
        elif name == "kill":
            if manager is None or not callable(getattr(manager, "delete_agent", None)):
                return _result(name, error="Service sous-agents indisponible")
            data = manager.delete_agent(str(val("agent_id", 0)))
        elif name == "delegate":
            if manager is None or not callable(getattr(manager, "submit", None)):
                return _result(name, error="Service sous-agents indisponible")
            data = manager.submit(str(val("objective", 1, val("objective", 0, ""))), agent_id=val("agent_id", 0, None), context=str(val("context", 2, "")), priority=int(val("priority", default=20)))
        elif name == "send":
            if teams is not None and callable(getattr(teams, "send", None)):
                data = teams.send(recipient=str(val("recipient", 0)), body=str(val("body", 1, val("message", 1, ""))), subject=val("subject", default=""), priority=int(val("priority", default=20)))
            elif manager is not None and callable(getattr(manager, "send_message", None)):
                data = manager.send_message(str(val("job_id", 0)), str(val("message", 1)))
            else:
                return _result(name, error="Service de communication indisponible")
        elif name in {"approve", "reject"}:
            operation = getattr(approvals, name, None) if approvals is not None else None
            if not callable(operation):
                return _result(name, error="Service approvals indisponible")
            approval_id = val("approval_id", 0)
            if approval_id is None or not str(approval_id).strip():
                return _result(name, error=f"Usage: /{name} <approval_id> [reason]")
            if isinstance(args, Mapping):
                decided_by = str(raw.get("decided_by", "user"))
                reason = raw.get("reason")
            else:
                decided_by = "user"
                reason = " ".join(str(item) for item in positional[1:]).strip() or None
            data = operation(str(approval_id), decided_by=decided_by, reason=reason)
        elif name in {"autonomy", "workspace"}:
            target = val(name, 0)
            setter = _get(application, f"set_{name}")
            if not callable(setter):
                return _result(name, error=f"Aucun setter {name} disponible")
            data = setter(target)
        else:
            return _result(name, error=f"Action inconnue: /{name}")
        return _result(name, data)
    except Exception as exc:
        return _result(name, error=str(exc))


__all__ = ["execute_action"]
