from __future__ import annotations

import json

import pytest

from context_assembler import ContextAssembler, ContextPolicy


def _tool(name: str, description: str = "tool") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
        },
    }


def _payload(assembler: ContextAssembler, messages, tools=None) -> str:
    body = {"messages": messages}
    if tools:
        body["tools"] = tools
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def test_huge_tool_schemas_fail_when_policy_plus_tools_cannot_fit():
    assembler = ContextAssembler(
        total_max_chars=420,
        total_max_tokens=200,
        output_reserve_tokens=50,
    )
    messages = [{"role": "system", "content": "immutable policy"}]
    tools = [_tool("huge", "x" * 1000)]

    with pytest.raises(ValueError, match="policy/system messages and tool schemas"):
        assembler.guard_messages(messages, tools=tools, stage="decision")


def test_system_prefix_is_preserved_but_counts_against_budget():
    assembler = ContextAssembler(
        total_max_chars=360,
        total_max_tokens=120,
        output_reserve_tokens=20,
    )
    system = {"role": "system", "content": "POLICY-" + "p" * 80}
    messages = [
        system,
        {"role": "user", "content": "old-" + "x" * 160},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "recent request"},
    ]

    guarded = assembler.guard_messages(messages, tools=None, stage="decision")

    assert guarded[0] == system
    assert any(item.get("content") == "recent request" for item in guarded)
    encoded = _payload(assembler, guarded)
    assert len(encoded) <= assembler.total_max_chars
    assert assembler.count_tokens(encoded) <= assembler.total_max_tokens


def test_output_reserve_reduces_available_provider_input_tokens():
    policy = ContextPolicy(
        total_max_chars=1000,
        total_max_tokens=100,
        output_reserve_tokens=40,
    )
    assembler = ContextAssembler(policy=policy, token_counter=lambda text: len(text))
    assert assembler.total_max_tokens == 60

    messages = [
        {"role": "system", "content": "p"},
        {"role": "user", "content": "x" * 100},
    ]
    guarded = assembler.guard_messages(messages, tools=None, stage="initial")
    encoded = _payload(assembler, guarded)

    assert assembler.count_tokens(encoded) <= 60
    assert guarded == [{"role": "system", "content": "p"}]


def test_guard_trims_tool_protocol_as_atomic_block():
    assembler = ContextAssembler(
        total_max_chars=460,
        total_max_tokens=150,
        output_reserve_tokens=30,
    )
    messages = [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": "old request " + "x" * 180},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "b", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "A"},
        {"role": "tool", "tool_call_id": "c2", "content": "B"},
        {"role": "user", "content": "latest"},
    ]

    guarded = assembler.guard_messages(messages, tools=[_tool("a"), _tool("b")])

    roles = [message["role"] for message in guarded]
    assert guarded[0]["role"] == "system"
    assert guarded[-1] == {"role": "user", "content": "latest"}
    # The old tool block is either present in full or absent in full.
    tool_results = [message for message in guarded if message.get("role") == "tool"]
    assistants_with_calls = [
        message for message in guarded if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    assert bool(tool_results) == bool(assistants_with_calls)
    if tool_results:
        assert {item["tool_call_id"] for item in tool_results} == {"c1", "c2"}
    assert "tool" not in roles or "assistant" in roles
    encoded = _payload(assembler, guarded, [_tool("a"), _tool("b")])
    assert len(encoded) <= assembler.total_max_chars
    assert assembler.count_tokens(encoded) <= assembler.total_max_tokens


def test_final_call_without_tools_does_not_pay_tool_schema_cost():
    assembler = ContextAssembler(
        total_max_chars=420,
        total_max_tokens=140,
        output_reserve_tokens=20,
    )
    messages = [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": "final response context"},
    ]

    guarded = assembler.guard_messages(messages, tools=None, stage="final", final=True)

    assert guarded == messages
    encoded = _payload(assembler, guarded)
    assert len(encoded) <= assembler.total_max_chars
    assert assembler.count_tokens(encoded) <= assembler.total_max_tokens
