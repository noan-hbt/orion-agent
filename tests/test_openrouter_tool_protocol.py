"""Provider-compatibility contracts for OpenRouter tool conversations."""

from openrouter_client import OpenRouterAPIError, OpenRouterClient


def _client() -> OpenRouterClient:
    # The protocol helpers are pure and do not require the optional httpx
    # dependency.  Constructing through ``__new__`` keeps these tests usable
    # in the lightweight source checkout as well as in the full installation.
    client = OpenRouterClient.__new__(OpenRouterClient)
    client.default_params = {"temperature": 0.2}
    client.model = "openai/gpt-5.6-luna"
    return client


def test_tool_messages_are_normalized_for_strict_providers():
    payload = _client()._payload(
        [
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "web_search",
                "content": {"ok": True},
            }
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "parameters": {"type": "object"},
                },
            }
        ],
        parallel_tool_calls=True,
    )

    assert payload["messages"] == [
        {"role": "tool", "tool_call_id": "call-1", "content": '{"ok": true}'}
    ]
    assert payload["parallel_tool_calls"] is True


def test_text_only_turn_omits_tool_controls():
    payload = _client()._payload(
        [{"role": "user", "content": "réponds"}],
        tools=[],
        tool_choice="auto",
        parallel_tool_calls=True,
    )

    assert "tools" not in payload
    assert "tool_choice" not in payload
    assert "parallel_tool_calls" not in payload


def test_parallel_tool_compatibility_fallback_drops_only_optional_control():
    client = _client()
    payload = client._payload(
        [{"role": "user", "content": "cherche"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "web_search", "parameters": {"type": "object"}},
            }
        ],
        parallel_tool_calls=True,
    )
    error = OpenRouterAPIError(
        "OpenRouter HTTP 400: Provider returned error",
        status_code=400,
    )

    fallback = client._compatibility_fallback_payload(payload, error)

    assert fallback is not None
    assert "tools" in fallback
    assert "parallel_tool_calls" not in fallback
