"""The conversation window survives forgetting: a rebase names journal positions (D81)."""

from __future__ import annotations

from orion.context.threads import MESSAGE_KIND, PRUNED_KIND, REBASED_KIND, ThreadProjection
from orion.core.events import NewEvent
from orion.core.journal import SqliteJournal
from orion.core.projections import ProjectionRunner
from tests.conftest import make_event


def said(text: str) -> NewEvent:
    return make_event(MESSAGE_KIND, thread_id="t", message={"role": "user", "content": text})


async def test_a_forgotten_message_does_not_move_the_window(journal: SqliteJournal) -> None:
    threads = ThreadProjection()
    runner = ProjectionRunner(journal, [threads])
    m0, m1, _m2, m3 = await journal.append([said(f"m{i}") for i in range(4)])
    await journal.append(
        [
            make_event(REBASED_KIND, thread_id="t", base=2, after_seq=m1.seq),
            make_event(PRUNED_KIND, thread_id="t", indexes=[3], seqs=[m3.seq]),
        ]
    )
    await runner.catch_up()
    live = [m["content"] for m in threads.get("t").window()]
    assert live[0] == "m2" and live[1].startswith("m3")
    await journal.redact([m0.id], reason="test", actor="owner:main")
    await runner.rebuild_all()
    rebuilt = [m["content"] for m in threads.get("t").window()]
    assert rebuilt == live  # not [m3]: the rebase still starts after m1
    assert threads.get("t").pruned == {2}  # m3, now at index 2
