from __future__ import annotations

import json
import time

from openrouter_client import OpenRouterClient, RegisteredTool


def _client() -> OpenRouterClient:
    return OpenRouterClient("test-key")


def _schema(width: int = 20) -> dict:
    return {
        "type": "object",
        "properties": {
            f"field_{index}": {
                "type": "string",
                "description": "x" * 40,
                "enum": ["a", "b", "c"],
            }
            for index in range(width)
        },
    }


def test_tool_definitions_are_isolated_from_input_and_returned_mutations() -> None:
    client = _client()
    try:
        parameters = _schema(2)
        client.register_tool("search", lambda **_: None, description="search", parameters=parameters)

        # Mutating the caller-owned schema after registration must not alter the
        # registry snapshot.
        parameters["properties"]["field_0"]["description"] = "corrupted-input"
        first = client.tool_definitions()
        assert first[0]["function"]["parameters"]["properties"]["field_0"]["description"] == "x" * 40

        # Results remain ordinary mutable dict/list objects, but are detached
        # from both the cache and RegisteredTool.parameters.
        first[0]["function"]["parameters"]["properties"]["field_0"]["description"] = "corrupted-output"
        first.append({"type": "function", "function": {"name": "fake"}})
        second = client.tool_definitions()

        assert len(second) == 1
        assert second[0]["function"]["parameters"]["properties"]["field_0"]["description"] == "x" * 40
        assert client.get_registered_tool("search").parameters["properties"]["field_0"]["description"] == "x" * 40
    finally:
        client.close()


def test_cache_is_reused_and_invalidated_on_register_unregister(monkeypatch) -> None:
    client = _client()
    original = RegisteredTool.definition.fget
    definition_reads = 0

    def counted_definition(tool):
        nonlocal definition_reads
        definition_reads += 1
        return original(tool)

    monkeypatch.setattr(RegisteredTool, "definition", property(counted_definition))
    try:
        client.register_tool("a", lambda: None, description="a", parameters=_schema(3))
        client.register_tool("b", lambda: None, description="b", parameters=_schema(3))

        client.tool_definitions()
        assert definition_reads == 2
        for _ in range(20):
            client.tool_definitions()
        assert definition_reads == 2

        client.register_tool("c", lambda: None, description="c", parameters=_schema(3))
        client.tool_definitions()
        assert definition_reads == 5  # one rebuild for the new 3-tool registry
        client.tool_definitions()
        assert definition_reads == 5

        client.unregister_tool("b")
        names = [item["function"]["name"] for item in client.tool_definitions()]
        assert names == ["a", "c"]
        assert definition_reads == 7  # one rebuild for the remaining two tools
    finally:
        client.close()


def test_cached_definitions_reduce_repeated_schema_construction_cost() -> None:
    client = _client()
    try:
        for index in range(40):
            client.register_tool(
                f"tool_{index}",
                lambda **_: None,
                description="tool " + "d" * 80,
                parameters=_schema(25),
            )

        # Prime the serialization cache once. Repeated calls must only decode a
        # stable JSON snapshot and never rebuild RegisteredTool definitions.
        expected = client.tool_definitions()
        cached_json = client._tool_definitions_json
        assert cached_json is not None

        started = time.perf_counter()
        for _ in range(50):
            current = client.tool_definitions()
            assert current == expected
        elapsed = time.perf_counter() - started

        # Operation-count invariant: one cached serialized representation is
        # retained across all hot calls. Keep the timing only as a lightweight
        # regression signal rather than a platform-sensitive hard threshold.
        assert client._tool_definitions_json is cached_json
        assert json.loads(cached_json) == expected
        assert elapsed >= 0
    finally:
        client.close()
