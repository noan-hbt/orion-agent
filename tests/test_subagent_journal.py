"""Contracts for journaling terminal sub-agent notifications.

These tests intentionally exercise the runtime journal boundary rather than
the worker implementation.  A completion is an assistant-facing message in
the parent conversation; worker envelopes and non-terminal notifications are
not conversation history.
"""

import json

from event_handler import Event
from prompt_context import ConversationJournal, SQLiteConversationJournal
from runtime import AgentRuntime, RunContext


def _completion_context(*, result="Canberra est la capitale de l'Australie.", event_id="completion-1"):
    event = Event(
        "subagent.completed",
        {
            "job_id": "job-web-1",
            "session_id": "session-1",
            "agent_name": "web-researcher",
            "result": result,
            "internal_event": True,
            "worker_payload": {"tool_calls": [{"name": "web"}]},
        },
        source="runtime",
        metadata={
            "conversation_id": "telegram:20",
            "parent_event_id": "request-1",
            "internal_event": True,
        },
        id=event_id,
    )
    return RunContext(event=event, task=None, run_id=None, loaded_state={})


def _journal_runtime(journal):
    return AgentRuntime(llm_client=None, conversation_journal=journal)


def test_subagent_completion_is_persisted_with_worker_sender_identity(tmp_path):
    journal = ConversationJournal(tmp_path / "conversation.jsonl")
    context = _completion_context()

    _journal_runtime(journal)._journal_context(context)

    messages = journal.recent_messages(conversation_id="telegram:20")
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["sender"] == "subagent:web-researcher"
    assert messages[0]["content"] == "Canberra est la capitale de l'Australie."


def test_conversational_completion_journals_worker_then_orion_synthesis(tmp_path):
    journal = ConversationJournal(tmp_path / "conversation.jsonl")
    context = _completion_context()
    context.event.metadata["resume_orchestrator"] = True
    context.answer = "Orion confirme la synthèse du worker."

    _journal_runtime(journal)._journal_context(context)

    messages = journal.recent_messages(conversation_id="telegram:20")
    assert [(item["sender"], item["content"]) for item in messages] == [
        ("subagent:web-researcher", "Canberra est la capitale de l'Australie."),
        ("orion", "Orion confirme la synthèse du worker."),
    ]


def test_completion_does_not_inject_internal_worker_envelope(tmp_path):
    journal = ConversationJournal(tmp_path / "conversation.jsonl")
    _journal_runtime(journal)._journal_context(_completion_context())

    raw = (tmp_path / "conversation.jsonl").read_text(encoding="utf-8")
    assert "job-web-1" not in raw
    assert "session-1" not in raw
    assert "worker_payload" not in raw
    assert "tool_calls" not in raw
    assert "internal_event" not in raw


def test_progress_subagent_notification_is_not_journaled(tmp_path):
    journal = ConversationJournal(tmp_path / "conversation.jsonl")
    event = Event(
        "subagent.progress",
        {"job_id": "job-1", "result": "intermediate/internal text"},
        source="runtime",
        metadata={"conversation_id": "telegram:20", "internal_event": True},
        id="notification-1",
    )

    _journal_runtime(journal)._journal_context(
        RunContext(event=event, task=None, run_id=None, loaded_state={})
    )

    assert not (tmp_path / "conversation.jsonl").exists()


def test_taskless_waiting_subagent_notification_is_journaled_when_conversational(tmp_path):
    journal = ConversationJournal(tmp_path / "conversation.jsonl")
    event = Event(
        "subagent.waiting",
        {"job_id": "job-1", "result": "J'ai besoin du numéro de compte."},
        source="runtime",
        metadata={
            "conversation_id": "telegram:20",
            "internal_event": True,
            "resume_orchestrator": True,
        },
        id="waiting-1",
    )
    context = RunContext(event=event, task=None, run_id=None, loaded_state={})
    context.answer = "Le worker attend ton numéro de compte."

    _journal_runtime(journal)._journal_context(context)

    messages = journal.recent_messages(conversation_id="telegram:20")
    assert [item["content"] for item in messages] == [
        "Le worker attend ton numéro de compte."
    ]


def test_jsonl_completion_journaling_is_idempotent(tmp_path):
    path = tmp_path / "conversation.jsonl"
    journal = ConversationJournal(path)
    runtime = _journal_runtime(journal)
    context = _completion_context()

    runtime._journal_context(context)
    runtime._journal_context(context)

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["event_id"] == "completion-1"


def test_sqlite_completion_journaling_is_idempotent(tmp_path):
    journal = SQLiteConversationJournal(tmp_path / "conversation.sqlite3")
    try:
        runtime = _journal_runtime(journal)
        context = _completion_context()

        runtime._journal_context(context)
        runtime._journal_context(context)

        messages = journal.recent_messages(conversation_id="telegram:20")
        assert len(messages) == 1
        assert messages[0]["role"] == "assistant"
        assert messages[0]["content"] == "Canberra est la capitale de l'Australie."
    finally:
        journal.close()
