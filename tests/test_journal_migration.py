import json

from prompt_context import SQLiteConversationJournal


def test_jsonl_migration_is_idempotent_and_cursor_paged(tmp_path):
    source = tmp_path / "conversations.jsonl"
    rows = [
        {"id": 4, "event_id": "e1", "task_id": 2, "messages": [{"role": "user", "content": "hello"}], "created_at": "2024-01-01T00:00:00Z"},
        {"id": 5, "event_id": "e1", "task_id": 2, "messages": [{"role": "user", "content": "duplicate"}], "created_at": "2024-01-01T00:00:01Z"},
    ]
    source.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    db = tmp_path / "journal.sqlite3"
    assert SQLiteConversationJournal.migrate_jsonl(source, db) == 1
    assert SQLiteConversationJournal.migrate_jsonl(source, db) == 0
    journal = SQLiteConversationJournal(db)
    assert [entry.id for entry in journal.after(0)] == [4]
    assert journal.after(4) == []


def test_sqlite_append_deduplicates_event(tmp_path):
    journal = SQLiteConversationJournal(tmp_path / "journal.db")
    first = journal.append(event_id="x", task_id=None, messages=[{"role": "user", "content": "ok"}])
    second = journal.append(event_id="x", task_id=None, messages=[{"role": "user", "content": "again"}])
    assert first.id == second.id
    assert journal.recent_messages(limit=5)[0]["content"] == "ok"
