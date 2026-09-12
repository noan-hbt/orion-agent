from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from context_assembler import ContextAssembler, ContextComponent, ContextPolicy


def _decode_evidence(value: str) -> dict:
    return json.loads(value)


def _assert_tool_protocol_atomic(history: list[dict]) -> None:
    pending: set[str] = set()
    for message in history:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            pending = {str(call["id"]) for call in message["tool_calls"]}
        elif message.get("role") == "tool":
            assert str(message.get("tool_call_id")) in pending
            pending.discard(str(message.get("tool_call_id")))
        elif pending:
            raise AssertionError("tool protocol was split")
    assert not pending


def test_evidence_preserves_ranked_lists_longer_than_eight_when_they_fit():
    assembler = ContextAssembler(total_max_chars=20_000)
    memories = [{"content": f"rank-{index}"} for index in range(20)]

    payload = _decode_evidence(assembler.evidence_envelope({"memories": memories}))

    assert payload["data"]["memories"] == memories


def test_evidence_shrink_keeps_best_first_memory_instead_of_tail_items():
    assembler = ContextAssembler(total_max_chars=20_000)
    memories = [
        {"content": f"rank-{index}-" + "x" * 140}
        for index in range(20)
    ]

    payload = _decode_evidence(
        assembler.evidence_envelope({"memories": memories}, max_chars=700)
    )
    kept = payload["data"]["memories"]

    assert kept
    assert kept[0]["content"].startswith("rank-0-")


def test_evidence_total_budget_keeps_high_priority_request_before_profile_noise():
    assembler = ContextAssembler(total_max_chars=20_000)

    payload = _decode_evidence(
        assembler.evidence_envelope(
            {
                "request": {"text": "must survive"},
                "event": {"type": "message"},
                "profile": {"notes": "x" * 5000},
            },
            max_chars=420,
        )
    )

    assert payload["data"]["request"]["text"] == "must survive"
    assert payload["data"]["event"]["type"] == "message"


def test_evidence_history_shrink_never_splits_tool_call_and_results():
    assembler = ContextAssembler(total_max_chars=20_000)
    history = [
        {"role": "user", "content": "old " + "z" * 500},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "run tools"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                {"id": "b", "type": "function", "function": {"name": "b", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "A"},
        {"role": "tool", "tool_call_id": "b", "content": "B"},
    ]

    payload = _decode_evidence(
        assembler.evidence_envelope({"history": history}, max_chars=850)
    )

    _assert_tool_protocol_atomic(
        [item for item in payload["data"]["history"] if isinstance(item, dict) and item.get("role")]
    )


def test_bound_history_skips_oversized_latest_block_and_keeps_current_request():
    request = {"role": "user", "content": "current request"}
    oversized = {"role": "user", "content": "evidence " + "x" * 10_000}

    bounded = ContextAssembler.bound_history(
        [request, oversized], max_chars=120, max_tokens=100
    )

    assert request in bounded


def test_task_projection_keeps_real_plan_and_current_step_but_drops_persistence_noise():
    assembler = ContextAssembler(total_max_chars=4000)
    task = {
        "id": 7,
        "objective": "ship feature",
        "status": "running",
        "priority": 20,
        "current_state": {"phase": "build"},
        "plan": [
            {"id": "s1", "position": 1, "title": "done", "status": "completed", "result": "ok"},
            {"id": "s2", "position": 2, "title": "build", "status": "in_progress", "result": None},
            {"id": "s3", "position": 3, "title": "test", "status": "pending", "result": None},
        ],
        "runs": [{"id": "r"}],
        "actions": [{"id": "a"}],
        "artifacts": ["artifact"],
        "history": ["noise"],
        "updated_at": "now",
    }

    rendered = json.loads(
        assembler.render(ContextComponent("task", task, max_chars=1200, max_tokens=500))
    )

    assert rendered["current_plan_step"]["id"] == "s2"
    assert [step["id"] for step in rendered["plan"]] == ["s1", "s2", "s3"]
    assert "runs" not in rendered
    assert "actions" not in rendered
    assert "artifacts" not in rendered
    assert "history" not in rendered


def test_request_projection_uses_configured_budget_not_one_third_of_it():
    assembler = ContextAssembler(total_max_chars=4000)
    rendered = json.loads(
        assembler.render(
            ContextComponent("request", {"text": "x" * 1000}, max_chars=300)
        )
    )

    assert len(rendered["text"]) > 240
    assert len(json.dumps(rendered, ensure_ascii=False, separators=(",", ":"))) <= 300


def test_component_max_tokens_is_enforced_with_custom_counter():
    assembler = ContextAssembler(
        total_max_chars=4000,
        total_max_tokens=1000,
        output_reserve_tokens=100,
        token_counter=len,
    )
    component = ContextComponent(
        "request", {"text": "x" * 1000}, max_chars=1000, max_tokens=120
    )

    rendered = assembler.render(component)

    assert len(rendered) <= 120
    assert assembler.count_tokens(rendered) <= 120


def test_memory_retrieval_precedes_persistent_noise_and_keeps_freshness_metadata():
    class Store:
        query = None

        def search(self, query, namespace):
            self.query = query
            return [
                SimpleNamespace(
                    id="m1",
                    content="launch date",
                    provenance="note:1",
                    confidence=0.9,
                    namespace=namespace,
                    freshness=0.7,
                    updated_at=123.0,
                    expires_at=None,
                    kind="fact",
                    scope="default",
                    status="active",
                    supports=(),
                    contradicts=(),
                )
            ]

    store = Store()
    assembler = ContextAssembler(memory_store=store, total_max_chars=4000)
    rendered = assembler.assemble(
        [
            ContextComponent("memories", ["noise", "launch date"], max_chars=1000, priority=35),
            ContextComponent("memory_query", {"content": "launch date"}, max_chars=1000, priority=75),
        ]
    )
    memories = json.loads(rendered["memories"])

    assert store.query == "launch date"
    assert memories[0]["content"] == "launch date"
    assert memories[0]["freshness"] == 0.7
    assert memories[0]["updated_at"] == 123.0
    assert memories.count("launch date") == 0
    assert memories[-1] == "noise"


def test_memory_retrieval_failure_is_distinguishable_from_empty_result():
    class BrokenStore:
        def search(self, query, namespace):
            raise RuntimeError("offline")

    class EmptyStore:
        def search(self, query, namespace):
            return []

    broken = ContextAssembler(memory_store=BrokenStore(), total_max_chars=4000)
    empty = ContextAssembler(memory_store=EmptyStore(), total_max_chars=4000)
    component = ContextComponent("memory_query", "anything", max_chars=1000)

    broken_values = json.loads(broken.assemble([component])["memories"])
    empty_values = json.loads(empty.assemble([component])["memories"])

    assert broken_values[0]["kind"] == "retrieval_status"
    assert broken_values[0]["available"] is False
    assert empty_values == []


def test_duplicate_component_names_are_charged_once_with_last_value_winning():
    assembler = ContextAssembler(
        total_max_chars=250, total_max_tokens=100, output_reserve_tokens=10
    )
    rendered = assembler.assemble(
        [
            ContextComponent("dup", "a" * 90, max_chars=100, priority=100),
            ContextComponent("dup", "b" * 90, max_chars=100, priority=100),
            ContextComponent("other", "c" * 90, max_chars=100, priority=50),
        ]
    )

    assert rendered["dup"] == "b" * 90
    assert rendered["other"] == "c" * 90
    assert sum(len(value) for value in rendered.values()) <= 250


def test_tool_observations_component_obeys_budget_and_keeps_recent_observations():
    assembler = ContextAssembler(total_max_chars=4000)
    observations = [
        {"index": index, "value": "x" * 80} for index in range(20)
    ]
    rendered = assembler.render(
        ContextComponent(
            "tool_observations", observations, max_chars=320, max_tokens=200
        )
    )
    values = json.loads(rendered)
    concrete = [item for item in values if isinstance(item, dict) and "index" in item]

    assert len(rendered) <= 320
    assert concrete[-1]["index"] == 19


def test_evidence_deduplicates_loaded_state_when_task_current_state_is_identical():
    assembler = ContextAssembler(total_max_chars=4000)
    payload = _decode_evidence(
        assembler.evidence_envelope(
            {
                "task": {"id": 1, "current_state": {"phase": "build"}},
                "loaded_state": {"phase": "build"},
            }
        )
    )

    assert "loaded_state" not in payload["data"]


def test_unknown_token_counter_is_rejected_instead_of_silently_falling_back():
    with pytest.raises(ValueError, match="token_counter"):
        ContextAssembler(token_counter="mystery")


def test_task_evidence_uses_task_specific_policy_budget():
    assembler = ContextAssembler(
        policy=ContextPolicy(
            total_max_chars=10_000,
            total_max_tokens=4_000,
            output_reserve_tokens=500,
            task_max_chars=420,
            task_max_tokens=160,
            event_max_chars=2_000,
            event_max_tokens=800,
        )
    )
    task = {
        "id": "task-1",
        "objective": "x" * 1500,
        "status": "running",
        "plan": [],
    }

    payload = _decode_evidence(assembler.evidence_envelope({"task": task}))
    encoded_task = json.dumps(
        payload["data"]["task"], ensure_ascii=False, separators=(",", ":")
    )

    assert len(encoded_task) <= 420
