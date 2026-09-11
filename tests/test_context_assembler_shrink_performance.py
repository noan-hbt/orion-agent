from __future__ import annotations

import context_assembler as module
from context_assembler import ContextAssembler


def _reference_mapping_shrink(value, max_chars):
    """Previous mapping algorithm, retained here as a semantic oracle."""
    result = {}
    omitted = 0
    for key, item in value.items():
        key = str(key)
        candidate = ContextAssembler._shrink_value(item, max_chars=max(1, max_chars // 3))
        trial = dict(result)
        trial[key] = candidate
        if len(ContextAssembler._serialize(trial)) <= max_chars:
            result[key] = candidate
        else:
            omitted += 1
    if omitted or len(ContextAssembler._serialize(result)) > max_chars:
        result["truncated"] = True
        result["omitted"] = max(1, omitted)
    while len(ContextAssembler._serialize(result)) > max_chars and result:
        removable = next(
            (key for key in result if key not in {"truncated", "omitted"}), None
        )
        if removable is None:
            break
        result.pop(removable)
    if len(ContextAssembler._serialize(result)) > max_chars:
        return {}
    return result


def test_mapping_shrink_matches_previous_semantics_and_order():
    source = {
        "alpha": "a" * 80,
        "βeta": "b" * 120,
        7: "numeric-key",
        "7": "string-key-overwrite",
        "truncated": "user-value",
        "tiny": "ok",
        "tail": "z" * 120,
    }
    for max_chars in (1, 2, 32, 80, 160, 230, 1000):
        expected = _reference_mapping_shrink(source, max_chars)
        actual = ContextAssembler._shrink_value(source, max_chars=max_chars)

        assert actual == expected
        assert list(actual) == list(expected)
        if max_chars >= 2:
            assert len(ContextAssembler._serialize(actual)) <= max_chars


def test_large_mapping_shrink_serialization_work_is_linear(monkeypatch):
    source = {f"key-{index}": "x" * 80 for index in range(3000)}
    original_dumps = module.json.dumps
    mapping_items_serialized = 0

    def counting_dumps(value, *args, **kwargs):
        nonlocal mapping_items_serialized
        if isinstance(value, dict):
            mapping_items_serialized += len(value)
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(module.json, "dumps", counting_dumps)

    reduced = ContextAssembler._shrink_value(source, max_chars=4000)

    # The old implementation repeatedly serialized the growing result and
    # processed hundreds of thousands of mapping entries. The optimized path
    # serializes only bounded one-entry fragments, so work stays O(n).
    assert mapping_items_serialized <= len(source) + 10
    assert reduced["truncated"] is True
    assert reduced["omitted"] > 0
    assert len(original_dumps(reduced, ensure_ascii=False, separators=(",", ":"))) <= 4000
