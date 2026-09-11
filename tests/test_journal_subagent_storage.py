"""Conversation journal invariants used by completed sub-agent deliveries."""

import pytest

from prompt_context import ConversationJournal, SQLiteConversationJournal


@pytest.fixture(params=["jsonl", "sqlite"])
def journal(request, tmp_path):
    """Exercise both journal backends through the same public contract."""
    if request.param == "jsonl":
        yield ConversationJournal(tmp_path / "conversation.jsonl")
        return
    instance = SQLiteConversationJournal(tmp_path / "conversation.sqlite3")
    try:
        yield instance
    finally:
        instance.close()


def test_completed_delivery_is_idempotent_per_event_and_conversation(journal):
    first = journal.append(
        event_id="subagent.completed:job-1",
        task_id=None,
        source="orion",
        conversation_id="telegram:42",
        messages=[{"role": "assistant", "content": "Résultat initial"}],
    )
    retry = journal.append(
        event_id="subagent.completed:job-1",
        task_id=None,
        source="orion",
        conversation_id="telegram:42",
        messages=[{"role": "assistant", "content": "Résultat du retry"}],
    )

    assert retry.id == first.id
    assert retry.messages[0]["content"] == "Résultat initial"
    assert len(journal.after(0)) == 1


def test_same_event_id_can_be_recorded_in_distinct_conversations(journal):
    left = journal.append(
        event_id="subagent.completed:job-2",
        task_id=None,
        conversation_id="telegram:alice",
        messages=[{"role": "assistant", "content": "Résultat Alice"}],
    )
    right = journal.append(
        event_id="subagent.completed:job-2",
        task_id=None,
        conversation_id="telegram:bob",
        messages=[{"role": "assistant", "content": "Résultat Bob"}],
    )

    assert left.id != right.id
    assert [m["content"] for m in journal.recent_messages(conversation_id="telegram:alice")] == [
        "Résultat Alice"
    ]
    assert [m["content"] for m in journal.recent_messages(conversation_id="telegram:bob")] == [
        "Résultat Bob"
    ]


def test_assistant_result_remains_visible_to_next_turn(journal):
    journal.append(
        event_id="request:parent-1",
        task_id=None,
        source="orion",
        channel="cli",
        conversation_id="cli:main",
        messages=[
            {"role": "user", "content": "Lance une recherche"},
            {"role": "assistant", "content": "Je lance deux sous-agents."},
        ],
    )
    journal.append(
        event_id="subagent.completed:job-3",
        task_id=None,
        source="orion",
        channel="cli",
        conversation_id="cli:main",
        messages=[{"role": "assistant", "content": "Canberra est la capitale."}],
    )

    history = journal.recent_messages(conversation_id="cli:main", limit=20)
    assert [(item["role"], item["content"]) for item in history] == [
        ("user", "Lance une recherche"),
        ("assistant", "Je lance deux sous-agents."),
        ("assistant", "Canberra est la capitale."),
    ]


def test_assistant_sender_is_normalized_for_history(journal):
    journal.append(
        event_id="subagent.completed:job-4",
        task_id=None,
        source="orion",
        conversation_id="default",
        messages=[{"role": "assistant", "content": "Synthèse terminée."}],
    )

    message = journal.recent_messages(limit=1)[0]
    assert message["role"] == "assistant"
    assert message["sender"] == "orion"


def test_explicit_worker_sender_is_preserved_for_history(journal):
    journal.append(
        event_id="subagent.completed:job-5",
        task_id=None,
        source="cli",
        conversation_id="cli:main",
        messages=[
            {
                "role": "assistant",
                "sender": "subagent:toml-analyst",
                "content": "Résultat du worker.",
            }
        ],
    )

    message = journal.recent_messages(conversation_id="cli:main", limit=1)[0]
    assert message["role"] == "assistant"
    assert message["sender"] == "subagent:toml-analyst"
