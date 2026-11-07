from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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


def test_a_memoire_too_long_says_what_it_leaves_out() -> None:
    """Cut in silence, the model believes it knows everything (ORION-4)."""
    from orion.memory.store import Fact

    facts = [
        Fact(
            f"f{i}",
            f"Préférence numéro {i}.",
            [],
            Category.PREFERENCE.value,
            None,
            None,
            i,
            None,
            None,
            Trust.OWNER,
            "terminal:t1",
        )
        for i in range(50)
    ]
    block = snapshot_block(facts, [], 300)
    assert block is not None and "(f49)" not in block
    assert "recall" in block


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


async def test_outside_content_never_overrides_what_he_said(memory: MemoryService) -> None:
    from orion.memory.service import MemoryRefused

    web = Origin(actor="orion:core", source="web:page", trust=Trust.WEB)
    rent = await memory.remember("Son loyer est de 520 €.", OWNER)
    with pytest.raises(MemoryRefused):
        await memory.remember("Son loyer est de 900 €.", web, supersedes=[rent])
    with pytest.raises(MemoryRefused):
        await memory.invalidate(rent, "la page dit autre chose", web)
    assert [f.id for f in await memory.store.facts()] == [rent]


async def test_the_same_fact_said_again_is_kept_once(memory: MemoryService) -> None:
    web = Origin(actor="orion:core", source="web:page", trust=Trust.WEB)
    first = await memory.remember("Paul est allergique aux noix", web)
    assert await memory.remember("paul est allergique aux noix.", web) == first
    told = await memory.remember("Paul est allergique aux noix !", OWNER)  # surer: replaces it
    [kept] = await memory.store.facts()
    assert kept.id == told and kept.trust is Trust.OWNER


async def test_an_older_store_is_rebuilt_from_the_journal(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(path)
    await store.open()
    await MemoryService(journal, store).remember("Il aime le thé.", OWNER)
    await store._sql.run(lambda: store._sql.db.execute("PRAGMA user_version = 1"))  # pyright: ignore[reportPrivateUsage]
    await store.close()
    reopened = MemoryStore(path)
    await reopened.open()
    assert await reopened.checkpoint() == 0  # dropped: replayed from the start
    await ProjectionRunner(journal, [reopened]).catch_up()
    assert [f.statement for f in await reopened.facts()] == ["Il aime le thé."]
    await reopened.close()


def test_a_message_without_a_subject_searches_nothing() -> None:
    assert fts_query("C qui elle ?") is None
    assert fts_query("Gegele elle aime quoi") is not None


async def test_he_can_have_a_fact_forgotten_from_a_conversation_with_outside_content(
    memory: MemoryService,
) -> None:
    """03/11: a task's web results in the conversation, « supprime ce souvenir » was refused
    by the memory guard. He is asked, and his yes removes it (D231)."""
    from orion.guardian.policy import APPROVED_DESPITE, ActionProposal, Approval, Guardian
    from orion.memory.tools import memory_tools
    from orion.tools.base import ToolContext

    class Yes:
        asked = 0

        async def approve(self, proposal: ActionProposal) -> Approval:
            Yes.asked += 1
            return Approval(True)

    rent = await memory.remember("Son loyer est de 520 €.", OWNER)
    forget = next(t for t in memory_tools(memory) if t.name == "forget_fact")
    args = forget.parse(json.dumps({"fact_id": rent, "reason": "il a déménagé"}))
    decision = await Guardian(Yes(), approvals="equilibre").decide(
        ActionProposal(forget, args, Trust.WEB, "owner")
    )
    assert decision.allowed and Yes.asked == 1
    ctx = ToolContext(
        "owner", "c", context_trust=Trust.WEB, approved=decision.reason == APPROVED_DESPITE
    )
    assert not (await forget.handler(args, ctx)).is_error
    assert await memory.store.facts() == []


async def test_in_libre_he_can_replace_or_forget_a_fact_after_a_web_page(
    memory: MemoryService,
) -> None:
    """D232: nothing asked, nothing refused; what is written keeps the page's trust."""
    from orion.memory.tools import memory_tools
    from orion.tools.base import ToolContext

    rent = await memory.remember("Son loyer est de 520 €.", OWNER)
    tools = {t.name: t for t in memory_tools(memory)}
    ctx = ToolContext("owner", "c", context_trust=Trust.WEB, unrestricted=True)
    remember = tools["remember"]
    said = json.dumps({"statement": "Son loyer est de 560 €.", "replaces": [rent]})
    assert not (await remember.handler(remember.parse(said), ctx)).is_error
    [fact] = await memory.store.facts()
    assert "560" in fact.statement and fact.trust == Trust.WEB
    forget = tools["forget_fact"]
    gone = json.dumps({"fact_id": fact.id, "reason": "faux"})
    assert not (await forget.handler(forget.parse(gone), ctx)).is_error
    assert await memory.store.facts() == []


async def test_the_watch_never_records_a_settled_or_past_commitment_again(
    memory: MemoryService,
) -> None:
    """ORION-29, 06/11: the watch recorded « restaurer le projet Jira » a second time, after he
    had said « tu peux le supp »."""
    from orion.guardian.policy import WATCH_THREAD
    from orion.memory.tools import memory_tools
    from orion.tools.base import ToolContext

    tools = {t.name: t for t in memory_tools(memory)}
    add, close = tools["commitment_add"], tools["commitment_close"]
    watch = ToolContext(WATCH_THREAD, "c", context_trust=Trust.SYSTEM)

    def owed(what: str, due: str | None = None, to: str = "owner") -> object:
        return add.parse(json.dumps({"what": what, "debtor": "orion", "creditor": to,
                                     "due": due}))  # fmt: skip

    first = await add.handler(owed("Restaurer le projet Jira ORION"), watch)
    again = await add.handler(owed("Restaurer le projet Jira supprimé"), watch)
    assert not first.is_error and again.is_error  # two passes, one commitment
    cid = str(first.data["commitment_id"])
    cancel = close.parse(json.dumps({"commitment_id": cid, "status": "cancelled"}))
    await close.handler(cancel, ToolContext("owner", "c"))  # « tu peux le supp »
    assert (await add.handler(owed("Restaurer le projet Jira ORION"), watch)).is_error
    assert (await add.handler(owed("Payer la cantine", "2020-01-01"), watch)).is_error  # past
    assert not (await add.handler(owed("Payer la cantine"), watch)).is_error
    assert len(await memory.store.commitments()) == 1

    # Review of #201: earlier today is past too; distinct creditors or a date against none
    # are two commitments, not one.
    earlier = f"{datetime.now(ZoneInfo('Europe/Paris')) - timedelta(minutes=5):%Y-%m-%dT%H:%M}"
    assert (await add.handler(owed("Rappeler le garage", earlier), watch)).is_error
    assert not (await add.handler(owed("Envoyer les documents", to="Alice"), watch)).is_error
    assert not (await add.handler(owed("Envoyer les documents", to="Bob"), watch)).is_error
    assert not (await add.handler(owed("Payer la cantine", "2099-01-01"), watch)).is_error
    assert len(await memory.store.commitments()) == 4


async def test_one_commitment_from_a_mail_then_the_watch_exists_once(
    memory: MemoryService, journal: SqliteJournal
) -> None:
    """ORI-005: the same commitment, read in a mail then found again by the watch, is one
    commitment that knows both sources, not two reminders."""
    from orion.guardian.policy import WATCH_THREAD
    from orion.memory.tools import memory_tools
    from orion.tools.base import ToolContext

    mail = Origin(actor="orion:extractor", source="mail:m_1", trust=Trust.THIRD_PARTY)
    cid = await memory.add_commitment(
        "Envoyer le devis signé à Alice", mail, debtor="owner", creditor="Alice", due="2099-05-12"
    )
    add = next(t for t in memory_tools(memory) if t.name == "commitment_add")
    args = {"what": "Envoyer à Alice le devis signé", "debtor": "owner", "creditor": "Alice",
            "due": "2099-05-12"}  # fmt: skip
    watch = ToolContext(WATCH_THREAD, "c", context_trust=Trust.SYSTEM)
    assert (await add.handler(add.parse(json.dumps(args)), watch)).is_error

    assert [c.id for c in await memory.store.commitments()] == [cid]
    seen = [e for e in await journal.read() if e.kind == "commitment.seen"]
    assert [e.payload["commitment_id"] for e in seen] == [cid]

    # Two meetings with Alice the same day are two.
    other = await memory.add_commitment(
        "Café avec Alice", OWNER, debtor="owner", creditor="Alice", due="2099-05-13T10:00"
    )
    later = await memory.add_commitment(
        "Café avec Alice", OWNER, debtor="owner", creditor="Alice", due="2099-05-13T15:00"
    )
    assert other != later


async def test_recall_everything_shows_the_whole_memory(memory: MemoryService) -> None:
    """03/11: sorting his memories ten at a time, Orion never knew he had seen them all (D235)."""
    from orion.memory.tools import memory_tools
    from orion.tools.base import ToolContext

    for i in range(15):
        await memory.remember(f"Souvenir numéro {i} sur un sujet à part.", OWNER)
    recall = next(t for t in memory_tools(memory) if t.name == "recall")
    result = await recall.handler(recall.parse('{"everything": true}'), ToolContext("owner", "c"))
    assert all(f"numéro {i} " in result.content for i in range(15))


async def test_the_reader_sees_what_is_already_scheduled(memory: MemoryService) -> None:
    async def scheduled() -> list[str]:
        return ["envoie la carte grise"]

    provider = ScriptedProvider(json_reply({"operations": []}))
    extractor = MemoryExtractor(provider, memory, "fake", told_later=scheduled)
    await extractor.consolidate("ok", "ok", OWNER, date(2026, 10, 19), [])
    assert "envoie la carte grise" in provider.requests[-1].messages[-1]["content"]
