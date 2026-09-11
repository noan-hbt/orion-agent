import json
import time
from types import SimpleNamespace

import pytest

from action_ledger import ActionDecision, ActionLedger
from approvals import ApprovalStore
from event_handler import Event
from runtime import AgentRuntime, RunContext
from tasks import ActionStatus, InMemoryTaskStore, TaskStatus
from tool_policy import ToolPolicy


def _call(name, raw_arguments, *, call_id="call-1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": raw_arguments},
    }


def _content(message):
    return json.loads(message["content"])


class RepairingClient:
    def __init__(self):
        self.complete_calls = []
        self.executed = []

    def tool_definitions(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "send_once",
                    "description": "test side effect",
                    "parameters": {"type": "object"},
                },
            }
        ]

    def get_registered_tool(self, name):
        return SimpleNamespace(side_effect=True, dedupe_window=0.0)

    def complete(self, messages, *, tools=None, **kwargs):
        self.complete_calls.append(list(messages))
        index = len(self.complete_calls)
        if index == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                _call("send_once", '{"to":"user",', call_id="bad-call")
                            ],
                        }
                    }
                ]
            }
        if index == 2:
            assert "invalid_arguments" in json.dumps(messages[-1])
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                _call(
                                    "send_once",
                                    json.dumps({"to": "user", "body": "fixed"}),
                                    call_id="good-call",
                                )
                            ],
                        }
                    }
                ]
            }
        return {
            "choices": [
                {"message": {"role": "assistant", "content": "repaired and completed"}}
            ]
        }

    def execute_tool_call(self, call, *, raise_tool_errors=True):
        self.executed.append(call)
        return {
            "role": "tool",
            "tool_call_id": call["id"],
            "name": call["function"]["name"],
            "content": json.dumps({"ok": True}),
        }


def test_malformed_json_becomes_tool_observation_and_model_repairs_without_duplicate_side_effect():
    client = RepairingClient()
    ledger = ActionLedger(":memory:", owner_id="runtime-test")
    runtime = AgentRuntime(
        llm_client=client,
        action_ledger=ledger,
        history_enabled=False,
    )

    runtime._wake(Event("message", {"text": "do the operation"}))

    assert len(client.complete_calls) == 3
    assert len(client.executed) == 1
    records = ledger.recent(operation="send_once")
    assert len(records) == 1
    assert records[0].status == "succeeded"
    assert runtime.last_error is None


class NeverExecuteClient:
    def __init__(self, *, fail=False):
        self.executed = []
        self.fail = fail

    def get_registered_tool(self, name):
        return SimpleNamespace(side_effect=True, dedupe_window=60.0)

    def execute_tool_call(self, call, *, raise_tool_errors=True):
        self.executed.append(call)
        if self.fail:
            raise RuntimeError("handler failed")
        return {
            "role": "tool",
            "tool_call_id": call["id"],
            "name": call["function"]["name"],
            "content": json.dumps({"ok": True}),
        }


class NoLedgerTouch:
    def reserve(self, *args, **kwargs):  # pragma: no cover - failure assertion
        raise AssertionError("malformed arguments must not reach ActionLedger")


def test_malformed_json_does_not_reach_policy_approval_ledger_or_handler():
    client = NeverExecuteClient()
    approvals = ApprovalStore(":memory:")
    runtime = AgentRuntime(
        llm_client=client,
        action_ledger=NoLedgerTouch(),
        tool_policy=ToolPolicy({"danger": "privileged"}),
        approval_store=approvals,
        history_enabled=False,
    )

    result = _content(runtime._execute_tool(_call("danger", '{"secret":')))

    assert result == {
        "executed": False,
        "error": "invalid_arguments",
        "invalid_arguments": True,
        "reason": "Tool arguments must be a valid JSON object.",
    }
    assert approvals.pending() == []
    assert client.executed == []


def test_uncertain_stale_action_is_not_reported_as_duplicate_or_retried_for_task():
    ledger = ActionLedger(":memory:", owner_id="worker-old", default_lease_seconds=30)
    arguments = {"to": "account:1", "body": "apply once"}
    first = ledger.reserve("danger", arguments, target="account:1", dedupe_window=60)
    ledger._connection.execute(
        "UPDATE actions SET lease_until=? WHERE action_key=?",
        (time.time() - 1.0, first.action_key),
    )
    ledger._connection.commit()

    tasks = InMemoryTaskStore()
    task = tasks.create("Do one protected side effect")
    event = Event("message", {"text": "continue"})
    run = task.start_run(event.id)
    tasks.save(task)
    context = RunContext(event=event, task=task, run_id=run.id, loaded_state={})
    client = NeverExecuteClient()
    runtime = AgentRuntime(
        llm_client=client,
        task_store=tasks,
        action_ledger=ledger,
        history_enabled=False,
    )
    runtime._current_task = task
    runtime._run_context = context

    result = _content(
        runtime._execute_tool(_call("danger", json.dumps(arguments)))
    )

    assert result["uncertain"] is True
    assert result["needs_reconciliation"] is True
    assert result["reason"] == "needs_reconciliation"
    assert "duplicate" not in result
    assert client.executed == []
    saved = tasks.get(task.id)
    assert saved is not None
    assert saved.status is TaskStatus.RUNNING
    assert saved.actions[-1].status is ActionStatus.SKIPPED
    assert saved.actions[-1].result["uncertain"] is True


class FencingLedger:
    def __init__(self, *, completion_uncertain=False):
        self.complete_calls = []
        self.uncertain_calls = []
        self.completion_uncertain = completion_uncertain

    def reserve(self, operation, arguments, **kwargs):
        return ActionDecision(
            True,
            "stable-key",
            "reserved",
            owner_id="lease-owner",
            fence_token=17,
        )

    def complete(self, key, result=None, *, owner_id=None, fence_token=None):
        self.complete_calls.append((key, owner_id, fence_token))
        return SimpleNamespace(needs_reconciliation=self.completion_uncertain)

    def mark_uncertain(self, key, error, *, owner_id=None, fence_token=None):
        self.uncertain_calls.append((key, owner_id, fence_token))
        return SimpleNamespace(needs_reconciliation=True)


@pytest.mark.parametrize("handler_fails", [False, True])
def test_runtime_finalizes_action_with_exact_reservation_owner_and_fence(handler_fails):
    ledger = FencingLedger()
    client = NeverExecuteClient(fail=handler_fails)
    runtime = AgentRuntime(
        llm_client=client,
        action_ledger=ledger,
        history_enabled=False,
    )

    runtime._execute_tool(_call("danger", json.dumps({"to": "x"})))

    expected = [("stable-key", "lease-owner", 17)]
    if handler_fails:
        assert ledger.uncertain_calls == expected
        assert ledger.complete_calls == []
    else:
        assert ledger.complete_calls == expected
        assert ledger.uncertain_calls == []


def test_side_effect_dispatch_exception_is_uncertain_and_not_retryable():
    ledger = ActionLedger(":memory:", owner_id="runtime-test")
    client = NeverExecuteClient(fail=True)
    runtime = AgentRuntime(
        llm_client=client,
        action_ledger=ledger,
        history_enabled=False,
    )
    arguments = {"to": "external-account", "body": "apply once"}

    result = _content(runtime._execute_tool(_call("danger", json.dumps(arguments))))

    assert result["executed"] is True
    assert result["uncertain"] is True
    assert result["needs_reconciliation"] is True
    assert result["reason"] == "tool_dispatch_exception"
    record = ledger.get(result["action_key"])
    assert record is not None and record.status == "uncertain"

    replay = _content(runtime._execute_tool(_call("danger", json.dumps(arguments), call_id="call-2")))
    assert replay["executed"] is False
    assert replay["uncertain"] is True
    assert replay["reason"] == "needs_reconciliation"
    assert len(client.executed) == 1


def test_fenced_completion_that_becomes_uncertain_is_not_reported_as_success():
    ledger = FencingLedger(completion_uncertain=True)
    client = NeverExecuteClient()
    runtime = AgentRuntime(
        llm_client=client,
        action_ledger=ledger,
        history_enabled=False,
    )

    result = _content(
        runtime._execute_tool(_call("danger", json.dumps({"to": "x"})))
    )

    assert len(client.executed) == 1
    assert result["executed"] is True
    assert result["uncertain"] is True
    assert result["needs_reconciliation"] is True
    assert result["reason"] == "reservation_lost_before_completion"
