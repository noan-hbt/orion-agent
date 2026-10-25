from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import pytest

from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.core.projections import ProjectionRunner
from orion.memory.extractor import MemoryExtractor
from orion.memory.model import Category, CommitmentStatus
from orion.memory.render import profile_markdown, snapshot_block
from orion.memory.service import MemoryService, Origin
from orion.memory.store import MemoryStore, fts_query
from orion.providers.base import ModelRequest, ModelResponse, Usage
from tests.conftest import make_event
from tests.fakes import Script, ScriptedProvider

OWNER = Origin(actor="orion:core", source="terminal:t1", trust=Trust.OWNER)


@pytest.fixture
async def memory(journal: SqliteJournal) -> AsyncIterator[MemoryService]:
    store = MemoryStore(":memory:")
    await store.open()
    yield MemoryService(journal, store)
    await store.close()


async def test_remember_and_search_ignores_accents_and_case(memory: MemoryService) -> None:
    await memory.remember(
        "Sa sœur Léa habite à Lyon.", OWNER, about=["Léa"], category=Category.PERSON
    )
    await memory.remember("Il est allergique aux noix de cajou.", OWNER, category=Category.PROFILE)
    found = await memory.store.search("Comment va lea ?")
    assert [f.statement for f in found] == ["Sa sœur Léa habite à Lyon."]
    assert found[0].about == ["Léa"]
    assert found[0].trust is Trust.OWNER


async def test_update_keeps_history_bitemporally(memory: MemoryService) -> None:
    old = await memory.remember("Noan habite à Paris.", OWNER, category=Category.PROFILE)
    new = await memory.remember(
        "Noan habite à Lyon.",
        OWNER,
        category=Category.PROFILE,
        valid_from="2026-09-01",
        supersedes=[old],
    )
    current = await memory.store.facts()
    assert [f.id for f in current] == [new]
    history = {f.id: f for f in await memory.store.facts(include_past=True)}
    assert history[old].known_to_seq is not None
    assert history[old].valid_to == "2026-09-01"
    assert "superseded" in (history[old].invalid_reason or "")
    assert [f.id for f in await memory.store.search("habite", include_past=True)] != []
    assert [f.id for f in await memory.store.search("Paris")] == []


async def test_commitments_open_sorted_by_due_and_close(memory: MemoryService) -> None:
    late = await memory.add_commitment("Rendre le livre", OWNER, debtor="Léa", creditor="owner")
    soon = await memory.add_commitment(
        "Envoyer le devis à Paul", OWNER, debtor="owner", creditor="Paul", due="2026-10-23"
    )
    assert [c.id for c in await memory.store.commitments()] == [soon, late]
    await memory.close_commitment(soon, CommitmentStatus.DONE, OWNER, evidence="mail envoyé")
    assert [c.id for c in await memory.store.commitments()] == [late]
    closed = await memory.store.commitment(soon)
    assert closed is not None and closed.status is CommitmentStatus.DONE


async def test_projection_rebuilds_identically(
    memory: MemoryService, journal: SqliteJournal
) -> None:
    a = await memory.remember("Fait A", OWNER)
    await memory.remember("Fait B", OWNER, supersedes=[a])
    await memory.add_commitment("Truc", OWNER, debtor="owner", creditor="Paul")
    fresh = MemoryStore(":memory:")
    await fresh.open()
    await ProjectionRunner(journal, [fresh]).catch_up()
    assert await fresh.facts(include_past=True) == await memory.store.facts(include_past=True)
    assert await fresh.commitments(status=None) == await memory.store.commitments(status=None)
    await fresh.close()


async def test_forget_source_removes_derived_memory_and_verifies(
    memory: MemoryService, journal: SqliteJournal
) -> None:
    [mail, neighbour] = await journal.append(
        [
            make_event("mail.received", source="mail:42", body="RDV dentiste"),
            make_event("mail.received", source="mail:420", body="Facture"),  # not mail:42
        ]
    )
    from_mail = Origin("orion:core", "thread:t1", Trust.THIRD_PARTY, derived_from=(mail.id,))
    await memory.remember("Rendez-vous chez le dentiste le 3 novembre.", from_mail)
    await memory.add_commitment(
        "Aller chez le dentiste", from_mail, debtor="owner", creditor="owner"
    )
    kept = await memory.remember("Il aime le café serré.", OWNER, category=Category.PREFERENCE)

    report = await memory.forget_source("mail:42", reason="test", actor="owner:main")

    assert report.verified
    assert (report.facts_removed, report.commitments_removed) == (1, 1)
    assert report.redacted_events == 3  # the mail + the two memory events derived from it
    assert [f.id for f in await memory.store.facts(include_past=True)] == [kept]
    assert await memory.store.search("dentiste", include_past=True) == []
    stored_mail = await journal.get(mail.id)
    assert stored_mail is not None and stored_mail.payload == {}
    other = await journal.get(neighbour.id)
    assert other is not None and other.payload == {"body": "Facture"}


@pytest.mark.parametrize("text", ['"; DROP TABLE facts; --', "NEAR(a b) OR *", "a b c"])
def test_fts_query_is_always_safe(text: str) -> None:
    query = fts_query(text)
    assert query is None or all(part.startswith('"') for part in query.split(" OR "))


def test_snapshot_and_profile_rendering() -> None:
    assert snapshot_block([], [], 1000) is None
    assert profile_markdown([], []).startswith("# Ce qu'Orion sait")


def json_reply(payload: object) -> Script:
    def make(_request: ModelRequest) -> ModelResponse:
        text = json.dumps(payload)
        return ModelResponse(
            message={"role": "assistant", "content": text},
            text=text,
            tool_calls=[],
            finish_reason="stop",
            usage=Usage(),
            model="fake",
        )

    return make


async def test_extractor_applies_valid_operations_and_rejects_the_rest(
    memory: MemoryService,
) -> None:
    old = await memory.remember("Noan travaille chez Acme.", OWNER, category=Category.PROFILE)
    commitment = await memory.add_commitment("Appeler Paul", OWNER, debtor="owner", creditor="Paul")
    provider = ScriptedProvider(
        json_reply(
            {
                "operations": [
                    {
                        "op": "update_fact",
                        "replaces": [old],
                        "statement": "Noan travaille chez Globex depuis octobre 2026.",
                        "category": "profile",
                    },
                    {
                        "op": "add_commitment",
                        "what": "Envoyer le devis",
                        "debtor": "owner",
                        "creditor": "Paul",
                        "due": "2026-10-23",
                    },
                    {"op": "close_commitment", "commitment_id": commitment, "status": "done"},
                    {"op": "invalidate_fact", "fact_id": "f_made_up", "reason": "?"},
                    {"op": "delete_everything"},
                ]
            }
        )
    )
    extractor = MemoryExtractor(provider, memory, "fake")
    report = await extractor.consolidate(
        "J'ai changé de boulot, je suis chez Globex. Et j'ai appelé Paul, je lui envoie le devis "
        "jeudi.",
        "Noté.",
        OWNER,
        date(2026, 10, 19),
        [],
    )
    assert len(report.applied) == 3
    assert len(report.rejected) == 2
    assert [f.statement for f in await memory.store.facts()] == [
        "Noan travaille chez Globex depuis octobre 2026."
    ]
    assert [c.what for c in await memory.store.commitments()] == ["Envoyer le devis"]
    request = provider.requests[0]
    assert request.json_output and request.session_id == "orion:memory-extractor"
    assert old in request.messages[1]["content"]  # existing related memory was shown


async def test_extractor_survives_garbage(memory: MemoryService) -> None:
    def garbage(_request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            message={"role": "assistant", "content": "pas du json"},
            text="pas du json",
            tool_calls=[],
            finish_reason="stop",
            usage=Usage(),
            model="fake",
        )

    report = await MemoryExtractor(ScriptedProvider(garbage), memory, "fake").consolidate(
        "salut", "salut", OWNER, date(2026, 10, 19), []
    )
    assert report.applied == [] and report.rejected == ["invalid JSON"]
    assert await memory.store.facts() == []


async def test_a_fact_asserted_twice_is_found_once(journal: SqliteJournal) -> None:
    from orion.memory.model import FACT_ASSERTED

    store = MemoryStore(":memory:")
    await store.open()
    fact = {"fact_id": "f1", "statement": "Il aime les noix."}
    await journal.append([make_event(FACT_ASSERTED, **fact), make_event(FACT_ASSERTED, **fact)])
    runner = ProjectionRunner(journal, [store])
    await runner.catch_up()
    assert len(await store.search("noix")) == 1
    for event in await journal.read():  # applied again: already in, nothing changes
        await store.apply(event)
    assert len(await store.search("noix")) == 1
    await store.close()


async def test_a_failed_transaction_keeps_its_own_error(tmp_path: Path) -> None:
    from orion.core.sqlite import SqliteWorker

    worker = SqliteWorker(tmp_path / "x.sqlite3", "test", "CREATE TABLE t (x INTEGER);")
    await worker.open()

    def body() -> None:
        with worker.transaction() as db:
            db.execute("ROLLBACK")  # what SQLite does on its own after some failures
            raise ValueError("the real cause")

    with pytest.raises(ValueError, match="the real cause"):
        await worker.run(body)

    def after() -> None:
        with worker.transaction() as db:  # the connection is usable again
            db.execute("INSERT INTO t VALUES (1)")

    await worker.run(after)
    await worker.close()


def test_a_commitment_closes_done_or_cancelled_never_open() -> None:
    from pydantic import ValidationError

    from orion.memory.tools import CommitmentCloseInput

    assert CommitmentCloseInput(commitment_id="c1", status=CommitmentStatus.CANCELLED)
    with pytest.raises(ValidationError):
        CommitmentCloseInput.model_validate({"commitment_id": "c1", "status": "open"})
