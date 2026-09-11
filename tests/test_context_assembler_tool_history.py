import json

from context_assembler import ContextAssembler, ContextComponent, _token_count


def _tool_turn():
    return [
        {"role": "user", "content": "use both tools"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call-a", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                {"id": "call-b", "type": "function", "function": {"name": "b", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call-a", "content": "A"},
        {"role": "tool", "tool_call_id": "call-b", "content": "B"},
    ]


def _assert_no_orphan_tools(history):
    active_ids = set()
    for message in history:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant" and message.get("tool_calls"):
            active_ids = {str(call["id"]) for call in message["tool_calls"]}
            continue
        if message.get("role") == "tool":
            assert str(message.get("tool_call_id")) in active_ids
            active_ids.discard(str(message.get("tool_call_id")))
            continue
        active_ids = set()
    assert not active_ids


def test_multiple_tool_calls_are_kept_as_one_atomic_block():
    old_turn = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
    ]
    tool_turn = _tool_turn()
    max_chars = len(ContextAssembler._serialize(tool_turn)) + 8

    bounded = ContextAssembler.bound_history(
        [*old_turn, *tool_turn], max_chars=max_chars, max_tokens=10_000
    )

    _assert_no_orphan_tools(bounded)
    assert [m for m in bounded if isinstance(m, dict) and m.get("role") == "tool"] == tool_turn[2:]
    assert any(isinstance(m, dict) and m.get("tool_calls") for m in bounded)
    assert not any(isinstance(m, dict) and m.get("content") == "old answer" for m in bounded)


def test_tight_truncation_drops_entire_tool_block_and_respects_limits():
    tool_turn = _tool_turn()
    bounded = ContextAssembler.bound_history(tool_turn, max_chars=48, max_tokens=12)

    encoded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded) <= 48
    assert _token_count(encoded) <= 12
    assert not any(isinstance(m, dict) and m.get("role") == "tool" for m in bounded)
    assert not any(isinstance(m, dict) and m.get("tool_calls") for m in bounded)


def test_orphan_or_incomplete_tool_results_are_never_preserved():
    history = [
        {"role": "tool", "tool_call_id": "orphan", "content": "bad"},
        *_tool_turn()[:-1],  # call-b result is missing, so the protocol block is incomplete.
        {"role": "user", "content": "latest plain turn"},
    ]

    bounded = ContextAssembler.bound_history(history, max_chars=10_000, max_tokens=2_500)

    _assert_no_orphan_tools(bounded)
    assert not any(isinstance(m, dict) and m.get("role") == "tool" for m in bounded)
    assert not any(isinstance(m, dict) and m.get("tool_calls") for m in bounded)
    assert {"role": "user", "content": "latest plain turn"} in bounded


def test_non_tool_content_is_unchanged_when_it_fits():
    history = [
        {"role": "system", "content": "system text"},
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]

    bounded = ContextAssembler.bound_history(history, max_chars=10_000, max_tokens=2_500)

    assert bounded == history


def test_component_and_total_budget_reduction_keep_tool_protocol_atomic():
    history = [
        {"role": "user", "content": "old " + "x" * 120},
        {"role": "assistant", "content": "old answer " + "y" * 120},
        *_tool_turn(),
    ]
    assembler = ContextAssembler(
        total_max_chars=360,
        total_max_tokens=100,
        output_reserve_tokens=10,
    )
    component = ContextComponent(
        "history", history, max_chars=320, max_tokens=80, priority=1
    )

    reduced = assembler.render_value(component)
    _assert_no_orphan_tools(reduced)
    assert len(ContextAssembler._serialize(reduced)) <= 320

    rendered = assembler.assemble([component])["history"]
    parsed = json.loads(rendered) if rendered else []
    _assert_no_orphan_tools(parsed)
    assert len(rendered) <= 360
    assert assembler.count_tokens(rendered) <= 90
