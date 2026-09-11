import io
import threading

from cli_cockpit import CockpitCLIAdapter, TranscriptEvent
from channels import AgentOutput, ChannelRouter, InboundMessage
from event_handler import EventHandler


class Backend:
    def __init__(self):
        self.executed = []

    def snapshot(self):
        return {"agents": [{"name": "worker", "state": "running"}]}

    def execute(self, command):
        self.executed.append(command)
        return {"title": "RESULT", "data": {"command": command}}


def test_commands_and_conversation_use_backend_and_route_input():
    out, incoming = io.StringIO(), []
    backend = Backend()
    cli = CockpitCLIAdapter(backend, input=io.StringIO("/commands\nhello\n/exit\n"), output=out)
    cli.start(incoming.append)
    cli.loop()
    assert incoming[0].text == "hello"
    assert backend.executed == []
    assert "RESULT" not in out.getvalue()
    assert "/status" in out.getvalue()


def test_send_is_atomic_and_records_distinct_transcript_event():
    out = io.StringIO()
    cli = CockpitCLIAdapter(Backend(), output=out)
    cli.start(lambda _: None)
    cli.send(AgentOutput("async output"))
    assert cli.transcript_events == [TranscriptEvent("assistant", "async output")]
    assert out.getvalue() == "● Orion\nasync output\n"


def test_subagent_output_keeps_worker_identity_in_transcript_and_stream():
    out = io.StringIO()
    cli = CockpitCLIAdapter(Backend(), output=out)
    cli.start(lambda _: None)

    cli.send(
        AgentOutput(
            "worker result",
            metadata={
                "output_origin": "subagent",
                "sender_name": "toml-analyst",
            },
        )
    )

    assert cli.transcript_events == [
        TranscriptEvent("worker", "worker result", speaker="Worker · toml-analyst")
    ]
    assert cli._transcript_text() == "WORKER · TOML-ANALYST\nworker result"
    assert out.getvalue() == "● Worker · toml-analyst\nworker result\n"
    assert "Orion" not in out.getvalue()


def test_conversational_subagent_result_is_compact_notification_not_message():
    out = io.StringIO()
    cli = CockpitCLIAdapter(Backend(), output=out)
    cli.start(lambda _: None)

    cli.send(
        AgentOutput(
            "raw worker result that Orion will synthesize",
            metadata={
                "output_origin": "subagent",
                "sender_name": "toml-analyst",
                "intermediate": True,
                "phase": "subagent_result",
            },
        )
    )

    assert cli.transcript_events == [
        TranscriptEvent("notification", "Worker · toml-analyst · résultat reçu")
    ]
    assert cli._transcript_text() == "• Worker · toml-analyst · résultat reçu"
    assert out.getvalue() == "● Worker · toml-analyst · résultat reçu\n"
    assert "raw worker result" not in cli._transcript_text()
    assert "raw worker result" not in out.getvalue()


def test_tool_preamble_is_compact_notification_and_strict_replay_is_deduped():
    out = io.StringIO()
    cli = CockpitCLIAdapter(Backend(), output=out)
    cli.start(lambda _: None)
    progress = AgentOutput(
        "Je vais effectuer trois recherches complémentaires.",
        correlation_id="cli-4",
        metadata={"intermediate": True, "phase": "tool_preamble"},
    )

    cli.send(progress)
    cli.send(progress)

    assert cli.transcript_events == [
        TranscriptEvent(
            "notification",
            "Orion · Je vais effectuer trois recherches complémentaires.",
            "cli-4",
        )
    ]
    assert out.getvalue() == "● Orion · Je vais effectuer trois recherches complémentaires.\n"


def test_identical_final_text_is_only_deduped_within_same_correlation():
    out = io.StringIO()
    cli = CockpitCLIAdapter(Backend(), output=out)
    cli.start(lambda _: None)

    cli.send(AgentOutput("Même réponse", correlation_id="cli-1"))
    cli.send(AgentOutput("Même réponse", correlation_id="cli-1"))
    cli.send(AgentOutput("Même réponse", correlation_id="cli-2"))

    assert [event.correlation_id for event in cli.transcript_events] == ["cli-1", "cli-2"]
    assert out.getvalue().count("Même réponse") == 2


def test_concurrent_sends_do_not_interleave():
    out = io.StringIO()
    cli = CockpitCLIAdapter(Backend(), output=out)
    cli.start(lambda _: None)
    threads = [threading.Thread(target=cli.send, args=(AgentOutput(str(i)),)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert out.getvalue().count("● Orion\n") == 10


def test_cockpit_message_is_json_safe_for_durable_event_handler(tmp_path):
    handler = EventHandler(workers=0, durable_path=str(tmp_path / "events.sqlite3"))
    router = ChannelRouter(handler)
    cli = CockpitCLIAdapter(Backend(), output=io.StringIO())
    cli.start(router.receive)
    try:
        message = cli.submit("hello")
        assert isinstance(message, InboundMessage)
        accepted = handler.queue.get_nowait()
        try:
            assert accepted.payload == {"text": "hello"}
            assert isinstance(accepted.metadata["received_at"], str)
            assert accepted.metadata["received_at"].endswith("+00:00")
        finally:
            handler.queue.task_done()
    finally:
        handler.close()
