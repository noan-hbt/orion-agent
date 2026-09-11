from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from action_ledger import ActionLedger, action_key
from event_handler import EventHandler
from subagents import SubAgentManager
from tool_policy import ToolPolicy


class _ReplaySideEffectLLM:
    model = "stub/model"

    def __init__(self, effects: list[dict[str, object]], *, fail_after_effect: bool = False):
        self.effects = effects
        self.fail_after_effect = fail_after_effect

    def tool_definitions(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "send_once",
                    "description": "side effect",
                    "parameters": {"type": "object"},
                },
            }
        ]

    def get_registered_tool(self, name):
        assert name == "send_once"
        return SimpleNamespace(side_effect=True, dedupe_window=60.0)

    def complete(self, messages, **kwargs):
        if any(message.get("role") == "tool" for message in messages):
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": "done"}}
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-stable",
                                "type": "function",
                                "function": {
                                    "name": "send_once",
                                    "arguments": json.dumps(
                                        {"to": "Account:1", "body": "Apply  Once"}
                                    ),
                                },
                            }
                        ],
                    }
                }
            ]
        }

    def execute_tool_call(self, call, *, raise_tool_errors=True):
        arguments = json.loads(call["function"]["arguments"])
        self.effects.append(arguments)
        if self.fail_after_effect:
            raise RuntimeError("provider disconnected after accepting request")
        return {
            "role": "tool",
            "tool_call_id": call["id"],
            "name": "send_once",
            "content": json.dumps({"receipt": "external-42"}),
        }


def _manager(
    state_path: Path,
    llm,
    *,
    ledger: ActionLedger,
    policy: ToolPolicy | None = None,
) -> SubAgentManager:
    return SubAgentManager(
        llm,
        EventHandler(workers=0),
        state_path=state_path,
        default_tools=["send_once"],
        tool_policy=policy or ToolPolicy({"send_once": "side_effect"}),
        action_ledger=ledger,
        emit_progress_events=False,
    )


def test_crash_before_session_persist_does_not_repeat_side_effect(tmp_path) -> None:
    state_path = tmp_path / "subagents.json"
    ledger_path = tmp_path / "actions.sqlite3"
    effects: list[dict[str, object]] = []
    first_ledger = ActionLedger(ledger_path, owner_id="subagent-first")
    first = _manager(state_path, _ReplaySideEffectLLM(effects), ledger=first_ledger)
    agent = first.create_agent("worker", "sends once", max_turns=3)
    job = first.submit("send it", agent_id=agent.id)

    # Initial session creation is already durable.  Fail only the save that
    # would persist the assistant tool call + result, reproducing the crash
    # window after external success but before session persistence.
    first._save_session = lambda *args, **kwargs: (_ for _ in ()).throw(
        OSError("crash before tool result session save")
    )
    with pytest.raises(OSError, match="crash before"):
        first._run_agent(agent, job.id)

    arguments = {"to": "Account:1", "body": "Apply  Once"}
    expected_key = action_key("send_once", arguments, target="account:1")
    record = first_ledger.get(expected_key)
    assert record is not None
    assert record.status == "succeeded"
    assert len(effects) == 1

    # Restart from the session that lacks the tool result.  The model emits the
    # same call again, but the shared durable action ledger returns the previous
    # action rather than dispatching it a second time.
    first.close()
    second_ledger = ActionLedger(ledger_path, owner_id="subagent-second")
    second = _manager(state_path, _ReplaySideEffectLLM(effects), ledger=second_ledger)
    recovered_agent = second.get_agent(agent.id)
    assert recovered_agent is not None
    recovered_job = second.get_job(job.id)
    assert recovered_job is not None

    assert second._run_agent(recovered_agent, recovered_job.id) == "done"
    assert len(effects) == 1
    session = second.get_session(recovered_job.session_id)
    assert session is not None
    duplicate_observation = next(
        message
        for message in session.messages
        if message.get("role") == "tool" and "duplicate" in str(message.get("content"))
    )
    duplicate_content = json.loads(duplicate_observation["content"])
    assert duplicate_content["duplicate"] is True
    assert duplicate_content["action_key"] == expected_key


def test_post_dispatch_failure_stays_non_retryable_until_reconciled(tmp_path) -> None:
    state_path = tmp_path / "subagents.json"
    ledger = ActionLedger(tmp_path / "actions.sqlite3", owner_id="subagent-owner")
    effects: list[dict[str, object]] = []
    llm = _ReplaySideEffectLLM(effects, fail_after_effect=True)
    manager = _manager(state_path, llm, ledger=ledger)
    agent = manager.create_agent("worker", "ambiguous sender", max_turns=2)
    job = manager.submit("send it", agent_id=agent.id)

    assert manager._run_agent(agent, job.id) == "done"
    assert len(effects) == 1
    arguments = {"to": "Account:1", "body": "Apply  Once"}
    key = action_key("send_once", arguments, target="account:1")
    record = ledger.get(key)
    assert record is not None
    assert record.status == "uncertain"
    assert record.needs_reconciliation is True

    # A second attempt is blocked while the dispatch outcome is ambiguous.
    blocked = manager._execute_tool_with_action_ledger(
        {
            "id": "retry-call",
            "type": "function",
            "function": {"name": "send_once", "arguments": json.dumps(arguments)},
        },
        "send_once",
        arguments,
    )
    blocked_content = json.loads(blocked["content"])
    assert blocked_content["uncertain"] is True
    assert blocked_content["needs_reconciliation"] is True
    assert blocked_content["reason"] == "needs_reconciliation"
    assert len(effects) == 1

    # The post-dispatch exception is moved to UNCERTAIN immediately; no lease
    # expiry window exists in which a caller can mistake it for a live retry.
    uncertain = ledger.get(key)
    assert uncertain is not None
    assert uncertain.status == "uncertain"
    assert uncertain.needs_reconciliation is True
    reconciled = ledger.reconcile(
        key,
        outcome="failed",
        error="provider confirms request was not applied",
    )
    assert reconciled.status == "failed"
    retry = ledger.reserve(
        "send_once",
        arguments,
        target="account:1",
        dedupe_window=60.0,
    )
    assert retry.allowed is True


def test_privileged_tool_is_committed_to_action_ledger(tmp_path) -> None:
    effects: list[dict[str, object]] = []
    ledger = ActionLedger(tmp_path / "actions.sqlite3", owner_id="subagent-owner")
    manager = _manager(
        tmp_path / "subagents.json",
        _ReplaySideEffectLLM(effects),
        ledger=ledger,
        policy=ToolPolicy({"send_once": "privileged"}),
    )
    manager.tool_authorizer = lambda *_args: True
    agent = manager.create_agent(
        "worker", "privileged sender", allowed_tools=["send_once"], max_turns=2
    )
    job = manager.submit("send it", agent_id=agent.id)

    assert manager._run_agent(agent, job.id) == "done"
    records = ledger.recent(operation="send_once")
    assert len(records) == 1
    assert records[0].status == "succeeded"
    assert records[0].action_key == action_key(
        "send_once",
        {"to": "Account:1", "body": "Apply  Once"},
        target="account:1",
    )
    assert len(effects) == 1
