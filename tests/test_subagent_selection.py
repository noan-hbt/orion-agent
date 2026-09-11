from __future__ import annotations

import pytest

from event_handler import EventHandler
from subagents import SubAgentJobStatus, SubAgentManager, SubAgentStatus


class _LLM:
    model = "stub/model"

    def tool_definitions(self):
        return []


def _manager(tmp_path):
    return SubAgentManager(
        _LLM(),
        EventHandler(workers=0),
        state_path=tmp_path / "subagents.json",
        emit_progress_events=False,
    )


def test_auto_selection_prefers_explicit_capability_match(tmp_path) -> None:
    manager = _manager(tmp_path)
    python = manager.create_agent(
        "Python Worker",
        "general backend work",
        capabilities=["python", "fastapi"],
    )
    manager.create_agent(
        "Researcher",
        "web research and summaries",
        capabilities=["research", "web"],
    )

    job = manager.submit("Build a FastAPI endpoint in Python")

    assert job.agent_id == python.id


def test_auto_selection_prioritizes_name_capability_then_description_matches(tmp_path) -> None:
    manager = _manager(tmp_path)
    named = manager.create_agent("Postgres Expert", "general engineering")
    capable = manager.create_agent(
        "Database Worker",
        "general engineering",
        capabilities=["postgres"],
    )
    described = manager.create_agent(
        "Backend Worker",
        "schema migration specialist",
    )

    with manager._lock:
        assert manager._select_agent_locked("Ask Postgres Expert to investigate").id == named.id
        assert manager._select_agent_locked("Need postgres tuning").id == capable.id
        assert (
            manager._select_agent_locked("Need a schema migration specialist").id
            == described.id
        )


@pytest.mark.parametrize(
    "status",
    [
        SubAgentJobStatus.QUEUED,
        SubAgentJobStatus.RUNNING,
        SubAgentJobStatus.WAITING,
    ],
)
def test_auto_selection_penalizes_loaded_agent_when_specialty_is_equal(
    tmp_path, status
) -> None:
    manager = _manager(tmp_path)
    first = manager.create_agent("Worker A", "python tasks", capabilities=["python"])
    second = manager.create_agent("Worker B", "python tasks", capabilities=["python"])

    busy = manager.submit("existing work", agent_id=first.id)
    with manager._lock:
        manager._jobs[busy.id].status = status
        manager._save_locked()

    selected = manager.submit("python implementation")

    assert selected.agent_id == second.id


def test_auto_selection_tie_break_is_stable_by_agent_id(tmp_path) -> None:
    manager = _manager(tmp_path)
    first = manager.create_agent("General One", "generic work")
    second = manager.create_agent("General Two", "generic work")

    expected = min(first.id, second.id)
    selections = [manager._select_agent_locked("unrelated objective").id for _ in range(5)]

    assert selections == [expected] * 5


def test_auto_selection_excludes_disabled_agents_even_on_stronger_match(tmp_path) -> None:
    manager = _manager(tmp_path)
    disabled = manager.create_agent(
        "Database Expert",
        "postgresql database migrations",
        capabilities=["postgresql", "database"],
    )
    fallback = manager.create_agent("General Worker", "general engineering")
    manager.update_agent(disabled.id, status=SubAgentStatus.DISABLED.value)

    job = manager.submit("Fix this PostgreSQL database migration")

    assert job.agent_id == fallback.id


def test_auto_selection_balances_repeated_equal_capability_jobs(tmp_path) -> None:
    manager = SubAgentManager(
        _LLM(),
        EventHandler(workers=0),
        state_path=tmp_path / "subagents.json",
        emit_progress_events=False,
        workers=2,
    )
    first = manager.create_agent("Python A", "python backend", capabilities=["python"])
    second = manager.create_agent("Python B", "python backend", capabilities=["python"])

    first_job = manager.submit("implement python service")
    second_job = manager.submit("implement python worker")

    assert {first_job.agent_id, second_job.agent_id} == {first.id, second.id}


def test_auto_selection_prefers_relevant_capability_over_idle_unrelated_agent(tmp_path) -> None:
    manager = _manager(tmp_path)
    specialist = manager.create_agent(
        "Database Worker",
        "database engineering",
        capabilities=["postgresql", "sql"],
    )
    fallback = manager.create_agent("General Worker", "generic operations")
    manager.submit("existing task", agent_id=specialist.id)

    selected = manager.submit("optimize this PostgreSQL query")

    assert selected.agent_id == specialist.id
    assert selected.agent_id != fallback.id


def test_auto_selection_avoids_saturated_equivalent_specialist(tmp_path) -> None:
    manager = SubAgentManager(
        _LLM(),
        EventHandler(workers=0),
        state_path=tmp_path / "subagents.json",
        emit_progress_events=False,
        workers=2,
    )
    saturated = manager.create_agent(
        "Python Saturated",
        "python backend",
        capabilities=["python"],
    )
    available = manager.create_agent(
        "Python Available",
        "python backend",
        capabilities=["python"],
    )
    manager.submit("busy one", agent_id=saturated.id)
    busy_two = manager.submit("busy two", agent_id=saturated.id)
    with manager._lock:
        manager._jobs[busy_two.id].status = SubAgentJobStatus.RUNNING
        manager._save_locked()

    selected = manager.submit("implement python endpoint")

    assert selected.agent_id == available.id
