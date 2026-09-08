"""Runtime placement and bounding tests for installed-tool guidance."""

from __future__ import annotations

from event_handler import Event
from runtime import AgentRuntime, RunContext
from tool_manager import ToolGuidance


def _context(text: str = "une demande") -> RunContext:
    return RunContext(
        event=Event("message", {"text": text}, source="test"),
        task=None,
        run_id="guidance-test",
        loaded_state={},
    )


def _runtime(mode: str, guidance=None) -> AgentRuntime:
    return AgentRuntime(
        context_mode=mode,
        tool_guidance=guidance,
        action_ledger_path=":memory:",
    )


def test_tool_guidance_is_in_system_message_in_contract_mode():
    runtime = _runtime(
        "contract",
        {
            "calendar": ToolGuidance(
                summary="Consulte les calendriers.",
                instructions="Vérifie le fuseau horaire avant de proposer une heure.",
                constraints=("Ne crée jamais un événement sans confirmation.",),
            )
        },
    )

    system = runtime._system_instructions()

    assert "## TOOL GUIDANCE" in system
    assert "### calendar" in system
    assert "Vérifie le fuseau horaire" in system
    assert "Ne crée jamais un événement" in system


def test_tool_guidance_is_in_system_message_in_legacy_mode():
    runtime = _runtime(
        "legacy",
        {"web": {"summary": "Recherche web", "instructions": "Cite les sources."}},
    )

    system = runtime._system_instructions()

    assert "## TOOL GUIDANCE" in system
    assert "### web" in system
    assert "Cite les sources." in system


def test_empty_tool_guidance_does_not_add_guidance_section():
    for mode in ("contract", "legacy"):
        system = _runtime(mode, {"empty": ToolGuidance()})._system_instructions()
        assert "## TOOL GUIDANCE" not in system


def test_tool_guidance_is_bounded_in_the_system_message():
    runtime = _runtime(
        "contract",
        {"huge": {"summary": "Résumé", "instructions": "x" * 20_000}},
    )

    system = runtime._system_instructions()
    guidance = system[system.index("## TOOL GUIDANCE") :]

    assert len(guidance) <= runtime._TOOL_GUIDANCE_MAX_CHARS
    assert "### huge" in guidance


def test_tool_guidance_is_not_injected_into_user_evidence():
    marker = "GUIDANCE_MUST_STAY_SYSTEM_ONLY"
    runtime = _runtime(
        "contract",
        {"example": {"summary": marker, "instructions": "Procédure privée du tool."}},
    )

    messages = runtime._contract_initial_run_messages(_context("bonjour"))
    evidence = "\n".join(
        str(message["content"])
        for message in messages
        if message.get("role") == "user"
    )

    assert marker not in evidence
    assert "Procédure privée du tool." not in evidence

