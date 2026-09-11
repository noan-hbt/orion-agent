"""Tests for tool-manifest guidance injected into sub-agent prompts."""

from __future__ import annotations

from event_handler import EventHandler
from subagents import SubAgentManager
from tool_manager import ToolGuidance


class _LLMStub:
    model = "stub/model"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def tool_definitions(self):
        return [
            {
                "type": "function",
                "function": {"name": "web_search", "description": "search"},
            }
        ]

    def complete(self, messages, **kwargs):
        self.calls.append([dict(message) for message in messages])
        return {"choices": [{"message": {"role": "assistant", "content": "done"}}]}


def _manager(tmp_path, llm, *, guidance=None, max_context_chars=16_000):
    return SubAgentManager(
        llm,
        EventHandler(),
        state_path=tmp_path / "subagents.json",
        default_tools=["web_search"],
        tool_guidance=guidance,
        max_context_chars=max_context_chars,
        emit_progress_events=False,
    )


def test_subagent_system_prompt_contains_structured_guidance_for_allowed_tool(tmp_path):
    llm = _LLMStub()
    manager = _manager(
        tmp_path,
        llm,
        guidance={
            "web": ToolGuidance(
                summary="Recherche documentaire",
                instructions="Vérifie la date et la qualité des sources.",
                constraints=("Cite les sources utilisées.",),
            )
        },
    )
    agent = manager.create_agent("Researcher", "Recherche", allowed_tools=["web_search"])

    prompt = manager._system_prompt_with_tool_guidance(agent)

    assert "## TOOL GUIDANCE" in prompt
    assert "### web" in prompt
    assert "Summary: Recherche documentaire" in prompt
    assert "Vérifie la date" in prompt
    assert "Cite les sources utilisées." in prompt


def test_subagent_guidance_is_filtered_and_not_copied_to_delegated_user_context(tmp_path):
    llm = _LLMStub()
    manager = _manager(
        tmp_path,
        llm,
        guidance={
            "web": {"tools": ["web_search"], "summary": "GUIDANCE_ONLY"},
            "calendar": {"tools": ["calendar_create"], "summary": "MUST_NOT_APPEAR"},
        },
    )
    agent = manager.create_agent("Researcher", "Recherche", allowed_tools=["web_search"], max_turns=1)
    job = manager.submit(
        "Analyse les résultats",
        agent_id=agent.id,
        context="DELEGATED_CONTEXT",
    )

    assert manager._run_agent(agent, job.id) == "done"
    messages = llm.calls[0]
    system = next(message["content"] for message in messages if message["role"] == "system")
    user = next(message["content"] for message in messages if message["role"] == "user")
    assert "GUIDANCE_ONLY" in system
    assert "MUST_NOT_APPEAR" not in system
    assert "DELEGATED_CONTEXT" in user
    assert "GUIDANCE_ONLY" not in user


def test_subagent_tool_guidance_is_bounded(tmp_path):
    llm = _LLMStub()
    manager = _manager(
        tmp_path,
        llm,
        guidance={"web": {"summary": "web", "instructions": "x" * 20_000}},
        max_context_chars=1_200,
    )
    agent = manager.create_agent("Researcher", "Recherche", allowed_tools=["web_search"])

    prompt = manager._system_prompt_with_tool_guidance(agent)
    section = prompt[prompt.index("## TOOL GUIDANCE") :]

    assert len(section) <= 1_200
    assert "### web" in section
