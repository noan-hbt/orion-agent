"""Runtime context contracts: retrieval, durable state and bounded assembly."""

import json
from types import SimpleNamespace

from context_assembler import ContextAssembler, ContextComponent
from context_os import ThreadStateStore
from event_handler import Event
from memory_store import MemoryStore
from prompt_context import ConversationJournal
from runtime import AgentRuntime, RunContext


def _context(text="remember the launch date", loaded_state=None):
    event = Event("message", {"text": text}, source="telegram")
    return RunContext(event=event, task=None, run_id="run-1",
                      loaded_state=loaded_state or {})


def test_injected_memory_keeps_provenance_in_runtime_evidence():
    memories = MemoryStore()
    item = memories.put("Launch date is 12 June", provenance="user:alice#42")
    assembler = ContextAssembler(memory_store=memories, total_max_chars=4000,
                                 total_max_tokens=900, output_reserve_tokens=100)
    rendered = assembler.assemble([
        ContextComponent("memory_query", "launch date", max_chars=1000, priority=10)
    ])
    values = json.loads(rendered["memories"])
    assert values[0]["content"] == item.content
    assert values[0]["provenance"] == "user:alice#42"


def test_retrieval_augments_existing_persistent_memories_instead_of_overwriting_them():
    memories = MemoryStore()
    try:
        memories.put("Launch date is 12 June", provenance="retrieval")
        assembler = ContextAssembler(memory_store=memories, total_max_chars=4000)
        rendered = assembler.assemble([
            ContextComponent("memories", ["Persistent user preference"], max_chars=1000, priority=35),
            ContextComponent("memory_query", "launch date", max_chars=1000, priority=75),
        ])
        values = json.loads(rendered["memories"])
        assert "Persistent user preference" in values
        assert any(isinstance(item, dict) and item.get("content") == "Launch date is 12 June" for item in values)
    finally:
        memories.close()


def test_thread_state_is_loaded_and_represented_in_initial_messages(tmp_path):
    path = tmp_path / "thread.json"
    ThreadStateStore(path, thread_id="thread-7").update(step="approval", cursor="c9")
    store = ThreadStateStore(path, thread_id="thread-7")
    runtime = AgentRuntime(llm_client=None, thread_state_store=store)
    messages = runtime._contract_initial_run_messages(_context())
    evidence = messages[-1]["content"]
    assert "thread_state" in evidence
    assert "approval" in evidence
    assert "c9" in evidence


def test_contract_context_contains_fresh_current_subagent_inventory():
    class Manager:
        def list_agents(self):
            return [
                SimpleNamespace(
                    id="agent-a",
                    name="toml-recenseur",
                    model="openai/gpt-4o-mini",
                    status=SimpleNamespace(value="active"),
                ),
                SimpleNamespace(
                    id="agent-b",
                    name="noan-herbeth-presse",
                    model="openai/gpt-4o-mini",
                    status=SimpleNamespace(value="active"),
                ),
            ]

        def list_jobs(self, **_kwargs):
            return []

    runtime = AgentRuntime(
        llm_client=None,
        subagent_manager=Manager(),
        context_mode="contract",
        action_ledger_path=":memory:",
    )

    messages = runtime._contract_initial_run_messages(_context("il reste des sous agents ?"))
    evidence = next(
        message["content"]
        for message in messages
        if str(message.get("content", "")).startswith("BEGIN_ORION_EVIDENCE")
    )
    payload = json.loads(
        evidence.removeprefix("BEGIN_ORION_EVIDENCE\n").removesuffix("\nEND_ORION_EVIDENCE")
    )

    inventory = payload["data"]["current_subagents"]
    assert inventory["available"] is True
    assert inventory["count"] == 2
    assert [item["id"] for item in inventory["agents"]] == ["agent-a", "agent-b"]


def test_legacy_context_marks_live_subagent_inventory_as_newer_than_history():
    class Manager:
        def list_agents(self):
            return [
                SimpleNamespace(
                    id="live-id",
                    name="live-agent",
                    model="openai/gpt-4o-mini",
                    status=SimpleNamespace(value="active"),
                )
            ]

        def list_jobs(self, **_kwargs):
            return []

    runtime = AgentRuntime(
        llm_client=None,
        subagent_manager=Manager(),
        context_mode="legacy",
        history_enabled=False,
        action_ledger_path=":memory:",
    )

    messages = runtime._initial_run_messages(_context("combien de sous agents ?"))
    live_message = next(
        message["content"]
        for message in messages
        if "Inventaire live des sous-agents" in str(message.get("content", ""))
    )
    assert "live-id" in live_message
    assert "live-agent" in live_message
    assert "prévaut sur toute mention historique" in live_message


def test_completed_event_is_answered_without_a_second_llm_call():
    calls = []
    class Client:
        def complete(self, *args, **kwargs):
            calls.append(1)
    context = _context()
    context.event = Event("subagent.completed", {"result": "finished"})
    AgentRuntime._run_agent_loop(type("R", (), {"llm_client": Client()})(), context)
    assert context.answer == "finished"
    assert calls == []


def test_tombstoned_memory_is_not_reinjected():
    memories = MemoryStore()
    item = memories.put("The private launch code", provenance="note:1")
    assert memories.forget(item.id, tombstone=True)
    assembler = ContextAssembler(memory_store=memories)
    rendered = assembler.assemble([
        ContextComponent("memory_query", "private launch code", max_chars=1000)
    ])
    assert json.loads(rendered["memories"]) == []


def test_context_budget_is_respected_and_priority_is_retained():
    assembler = ContextAssembler(total_max_chars=320, total_max_tokens=80,
                                 output_reserve_tokens=10)
    rendered = assembler.assemble([
        ContextComponent("request", {"text": "important request"}, max_chars=1000, priority=100),
        ContextComponent("history", [{"role": "user", "content": "x" * 5000}], max_chars=10000, priority=1),
    ])
    assert sum(len(value) for value in rendered.values()) <= 320
    assert "important request" in rendered["request"]


def test_failed_run_is_journaled_for_continue(tmp_path):
    """A provider failure must leave enough history for a follow-up message."""

    class FailingLLM:
        def tool_definitions(self):
            return []

        def complete(self, *args, **kwargs):
            raise RuntimeError("provider returned error")

    journal = ConversationJournal(tmp_path / "conversation.jsonl")
    runtime = AgentRuntime(
        llm_client=FailingLLM(),
        conversation_journal=journal,
        action_ledger_path=str(tmp_path / "actions.sqlite3"),
    )
    event = Event(
        "message",
        {"text": "Crée une équipe de sous-agents"},
        source="telegram",
        metadata={"channel": "telegram", "conversation_id": "telegram:20"},
    )

    runtime._wake(event)

    history = journal.recent_messages(conversation_id="telegram:20", limit=20)
    contents = [str(item.get("content", "")) for item in history]
    assert any("Crée une équipe de sous-agents" in content for content in contents)
    assert any("RUN a été interrompu" in content for content in contents)
