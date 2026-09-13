"""Regression: worker output must not be delivered as an Orion message.

On Telegram (and every other chat adapter) a sub-agent's report arrived as an
unlabelled message, so it read as though Orion had written the worker's text
verbatim. The cockpit fixed the same confusion by heading worker output with
its speaker; these tests pin the equivalent behaviour for outbound channels.
"""
from __future__ import annotations

import pytest

from channels import AgentOutput
from channel_adapters import (
    is_intermediate_worker_artifact,
    outbound_display_text,
    worker_attribution,
)

REPORT = "# Rapport OSINT\n\n## 1. Entite\n\ncontenu du rapport\n"


def _worker(metadata):
    return AgentOutput(REPORT, metadata=metadata)


WORKER_INTERMEDIATE = {
    "output_origin": "subagent",
    "sender_name": "osint_web",
    "intermediate": True,
    "phase": "subagent_result",
}


def test_worker_attribution_names_the_subagent():
    assert worker_attribution(_worker(WORKER_INTERMEDIATE)) == "osint_web"
    assert worker_attribution(_worker({"output_origin": "subagent"})) == "sous-agent"
    # Orion's own output carries no attribution.
    assert worker_attribution(AgentOutput("reponse", metadata={})) is None
    assert worker_attribution(AgentOutput("reponse")) is None


def test_intermediate_worker_artifact_is_recognised():
    assert is_intermediate_worker_artifact(_worker(WORKER_INTERMEDIATE)) is True
    # A standalone worker result is a real delivery, not suppressed progress.
    assert (
        is_intermediate_worker_artifact(
            _worker({"output_origin": "subagent", "sender_name": "w"})
        )
        is False
    )
    # A tool preamble is Orion's, not a worker's.
    assert (
        is_intermediate_worker_artifact(
            AgentOutput("je lance", metadata={"intermediate": True, "phase": "tool_preamble"})
        )
        is False
    )


def test_worker_progress_is_not_sent_to_a_chat_channel():
    """The reported bug: the sub-agent report was delivered as Orion's text."""
    assert outbound_display_text(_worker(WORKER_INTERMEDIATE)) is None


def test_standalone_worker_output_is_labelled():
    display = outbound_display_text(
        _worker({"output_origin": "subagent", "sender_name": "osint_web"})
    )
    assert display is not None
    assert display.startswith("🔧 Sous-agent · osint_web")
    assert "Rapport OSINT" in display


def test_orion_output_is_untouched():
    assert outbound_display_text(AgentOutput("ma synthese", metadata={})) == "ma synthese"
    preamble = AgentOutput("je lance", metadata={"intermediate": True, "phase": "tool_preamble"})
    assert outbound_display_text(preamble) == "je lance"


def test_labelling_can_be_disabled_without_changing_collapsing():
    standalone = _worker({"output_origin": "subagent", "sender_name": "w"})
    assert outbound_display_text(standalone, label_workers=False) == REPORT
    assert outbound_display_text(_worker(WORKER_INTERMEDIATE), label_workers=False) is None


def test_telegram_send_skips_worker_progress_and_labels_the_rest():
    """Drive the real adapter with a stubbed API call."""
    from channel_adapters import TelegramAdapter

    sent: list[dict] = []

    adapter = TelegramAdapter(
        "token",
        allow_all_chats=True,
    )
    adapter._api = lambda method, payload: sent.append((method, payload))  # type: ignore[method-assign]

    # Worker progress: nothing sent, and no exception (so the ledger ACKs).
    adapter.send(
        AgentOutput(REPORT, recipient="4242", metadata=dict(WORKER_INTERMEDIATE))
    )
    assert sent == [], "worker progress was delivered to Telegram"

    # Orion's own answer still goes out.
    adapter.send(AgentOutput("ma synthese", recipient="4242", metadata={}))
    assert len(sent) == 1
    assert sent[0][1]["text"] == "ma synthese"

    # A standalone worker delivery is labelled.
    adapter.send(
        AgentOutput(
            REPORT,
            recipient="4242",
            metadata={"output_origin": "subagent", "sender_name": "osint_web"},
        )
    )
    assert len(sent) == 2
    assert "Sous-agent" in sent[1][1]["text"]
    assert "osint_web" in sent[1][1]["text"]


def test_suppressed_delivery_does_not_raise_so_the_ledger_acks():
    """A collapsed output must return normally, not raise.

    ``ChannelRouter``'s outbound worker ACKs the ledger row when ``send``
    returns and fails it when it raises, so suppressing via an exception would
    retry the same artifact forever instead of dropping it once.
    """
    from channel_adapters import TelegramAdapter

    adapter = TelegramAdapter("token", allow_all_chats=True)
    adapter._api = lambda method, payload: pytest.fail("Telegram was called")  # type: ignore[method-assign]

    # Must return None without raising.
    assert (
        adapter.send(
            AgentOutput(REPORT, recipient="4242", metadata=dict(WORKER_INTERMEDIATE))
        )
        is None
    )
