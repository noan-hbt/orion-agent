from __future__ import annotations

import hashlib
import json

import pytest

from approvals import ApprovalStore
from event_handler import EventHandler
from runtime import AgentRuntime
from subagents import SubAgentJobStatus, SubAgentManager
from tool_policy import ToolPolicy


class _ToolLLM:
    model = "stub/model"

    def __init__(self, tool_name: str, arguments: dict[str, object]) -> None:
        self.tool_name = tool_name
        self.arguments = arguments
        self.execute_calls: list[dict] = []
        self.complete_calls = 0

    def tool_definitions(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": self.tool_name,
                    "description": "test tool",
                    "parameters": {"type": "object"},
                },
            }
        ]

    def complete(self, messages, **kwargs):
        self.complete_calls += 1
        if self.complete_calls == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": self.tool_name,
                                        "arguments": json.dumps(self.arguments),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"role": "assistant", "content": "done"}}]}

    def execute_tool_call(self, call, **kwargs):
        self.execute_calls.append(dict(call))
        return {
            "role": "tool",
            "tool_call_id": call["id"],
            "name": self.tool_name,
            "content": "ok",
        }


def _manager(
    tmp_path,
    llm,
    *,
    default_tools,
    policy,
    authorizer=None,
):
    return SubAgentManager(
        llm,
        EventHandler(workers=0),
        state_path=tmp_path / "subagents.json",
        default_tools=default_tools,
        tool_policy=policy,
        tool_authorizer=authorizer,
        emit_progress_events=False,
    )


def test_privileged_tool_denied_without_authorizer_and_handler_not_called(tmp_path) -> None:
    llm = _ToolLLM("danger", {"target": "prod"})
    manager = _manager(
        tmp_path,
        llm,
        default_tools=["danger"],
        policy=ToolPolicy({"danger": "privileged"}),
    )
    agent = manager.create_agent("worker", "does work", allowed_tools=["danger"], max_turns=2)
    job = manager.submit("do work", agent_id=agent.id)

    assert manager._allowed_tool_definitions(agent)[0]["function"]["name"] == "wait_for_input"
    assert manager._run_agent(agent, job.id) == "done"
    assert llm.execute_calls == []


def test_privileged_tool_authorizer_approves_exact_call_by_digest(tmp_path) -> None:
    arguments = {"target": "prod", "force": True}
    llm = _ToolLLM("danger", arguments)
    approvals: list[tuple[str, str, str, str]] = []

    def authorizer(agent, job, tool_name, arguments_digest):
        approvals.append((agent.id, job.id, tool_name, arguments_digest))
        return True

    manager = _manager(
        tmp_path,
        llm,
        default_tools=["danger"],
        policy=ToolPolicy({"danger": "privileged"}),
        authorizer=authorizer,
    )
    agent = manager.create_agent("worker", "does work", allowed_tools=["danger"], max_turns=2)
    job = manager.submit("do work", agent_id=agent.id)

    definitions = manager._allowed_tool_definitions(agent)
    assert {item["function"]["name"] for item in definitions} == {"danger", "wait_for_input"}
    assert manager._run_agent(agent, job.id) == "done"

    canonical = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    expected_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert approvals == [(agent.id, job.id, "danger", expected_digest)]
    assert len(llm.execute_calls) == 1


@pytest.mark.parametrize("classification", ["read_only", "side_effect"])
def test_non_privileged_policy_classes_follow_capability_ceiling(tmp_path, classification) -> None:
    llm = _ToolLLM("regular", {"value": 1})
    manager = _manager(
        tmp_path,
        llm,
        default_tools=["regular"],
        policy=ToolPolicy({"regular": classification}),
    )
    agent = manager.create_agent("worker", "does work", allowed_tools=["regular"], max_turns=2)
    job = manager.submit("do work", agent_id=agent.id)

    assert manager._run_agent(agent, job.id) == "done"
    assert len(llm.execute_calls) == 1


def test_capability_ceiling_precedes_privileged_authorizer(tmp_path) -> None:
    llm = _ToolLLM("danger", {"target": "prod"})
    authorizer_calls = []

    def authorizer(*args):
        authorizer_calls.append(args)
        return True

    manager = _manager(
        tmp_path,
        llm,
        default_tools=["safe"],
        policy=ToolPolicy({"danger": "privileged"}),
        authorizer=authorizer,
    )
    agent = manager.create_agent("worker", "does work", allowed_tools=[] , max_turns=2)
    # Simulate accidental in-memory widening after all create/load checks.
    agent.allowed_tools.append("danger")
    job = manager.submit("do work", agent_id=agent.id)

    assert manager._run_agent(agent, job.id) == "done"
    assert authorizer_calls == []
    assert llm.execute_calls == []


class _RetryingToolLLM(_ToolLLM):
    def complete(self, messages, **kwargs):
        self.complete_calls += 1
        if self.complete_calls <= 2:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": f"call-{self.complete_calls}",
                                    "type": "function",
                                    "function": {
                                        "name": self.tool_name,
                                        "arguments": json.dumps(self.arguments),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"role": "assistant", "content": "done"}}]}

    def get_registered_tool(self, name):
        if name == self.tool_name:
            return type("Registered", (), {"side_effect": True, "dedupe_window": 60.0})()
        return None


def test_privileged_worker_call_waits_for_runtime_approval_then_executes_once(tmp_path) -> None:
    arguments = {"target": "production", "force": True}
    llm = _RetryingToolLLM("danger", arguments)
    approvals = ApprovalStore(":memory:")
    policy = ToolPolicy({"danger": "privileged"})
    manager = SubAgentManager(
        llm,
        EventHandler(workers=0),
        state_path=tmp_path / "subagents-approval.json",
        default_tools=["danger"],
        tool_policy=policy,
        emit_progress_events=False,
    )
    runtime = AgentRuntime(
        llm_client=llm,
        subagent_manager=manager,
        tool_policy=policy,
        approval_store=approvals,
        action_ledger=manager.action_ledger,
        history_enabled=False,
    )
    runtime.receive_event = lambda _event: None
    manager.tool_approval_broker = runtime._subagent_tool_approval_broker
    try:
        agent = manager.create_agent("worker", "does privileged work", allowed_tools=["danger"], max_turns=3)
        job = manager.submit("do privileged work", agent_id=agent.id)

        manager._execute_job(job.id)
        waiting = manager.get_job(job.id)
        assert waiting is not None
        assert waiting.status is SubAgentJobStatus.WAITING
        assert waiting.pending_approval_id
        assert llm.execute_calls == []
        stored = approvals.get(waiting.pending_approval_id)
        assert stored is not None
        assert stored["requester"].startswith("subagent:")
        assert stored["payload"]["job_id"] == job.id
        assert stored["payload"]["args_hash"] == runtime._approval_args_hash(arguments)

        approvals.approve(waiting.pending_approval_id, decided_by="operator")
        queued = manager.get_job(job.id)
        assert queued is not None and queued.status is SubAgentJobStatus.QUEUED

        manager._execute_job(job.id)
        completed = manager.get_job(job.id)
        assert completed is not None and completed.status is SubAgentJobStatus.COMPLETED
        assert len(llm.execute_calls) == 1
        assert json.loads(llm.execute_calls[0]["function"]["arguments"]) == arguments
    finally:
        manager.close()
