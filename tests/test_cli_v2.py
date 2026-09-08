import io
import json

from channels import AgentOutput
from cli_v2 import OrionCLIAdapter, TranscriptEvent


def test_adapter_is_a_channel_and_submits_normalized_message():
    seen = []
    out = io.StringIO()
    cli = OrionCLIAdapter(output=out, banner=False)
    cli.start(seen.append)
    message = cli.submit("  bonjour  ", correlation_id="corr")
    assert message.channel == "cli"
    assert message.source == "cli"
    assert message.text == "bonjour"
    assert message.correlation_id == "corr"
    assert seen == [message]
    assert out.getvalue() == "❯ bonjour\n"


def test_send_serializes_concurrent_output_as_append_only_events():
    out = io.StringIO()
    cli = OrionCLIAdapter(output=out, banner=False)
    cli.start(lambda _: None)
    event = cli.send(AgentOutput("réponse", event_id="e1", correlation_id="c1"))
    assert isinstance(event, TranscriptEvent)
    assert event.kind == "assistant"
    assert len(cli.transcript) == 1
    assert out.getvalue() == "● Orion\nréponse\n"


def test_jsonl_has_machine_stable_shape():
    out = io.StringIO()
    cli = OrionCLIAdapter(output=out, mode="jsonl", banner=False)
    cli.start(lambda _: None)
    cli.send(AgentOutput("ok", event_id="e2", metadata={"seq": 3}))
    record = json.loads(out.getvalue())
    assert record["kind"] == "assistant"
    assert record["event_id"] == "e2"
    assert record["sequence"] == 3


def test_banner_is_bounded_and_status_provider_is_supported():
    out = io.StringIO()
    cli = OrionCLIAdapter(output=out, width=40)
    cli.set_status_provider(lambda: {"model": "test", "state": "ready"})
    cli.start(lambda _: None)
    lines = out.getvalue().splitlines()
    assert len(lines) == 3
    assert all(len(line) <= 40 for line in lines)
    assert "test" in out.getvalue()


def test_commands_do_not_reach_core():
    seen = []
    cli = OrionCLIAdapter(input=io.StringIO("/help\n/status\n/exit\n"), output=io.StringIO(), banner=False)
    cli.start(seen.append)
    cli.loop()
    assert seen == []
    assert "/help" in cli.output.getvalue()

