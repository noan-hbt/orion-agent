import io
import threading

from cli_cockpit import CockpitCLIAdapter, TranscriptEvent
from channels import AgentOutput


class Backend:
    def snapshot(self):
        return {"agents": [{"name": "worker", "state": "running"}]}

    def execute(self, command):
        return {"title": "RESULT", "data": {"command": command}}


def test_commands_and_conversation_use_backend_and_route_input():
    out, incoming = io.StringIO(), []
    cli = CockpitCLIAdapter(Backend(), input=io.StringIO("/commands\nhello\n/exit\n"), output=out)
    cli.start(incoming.append)
    cli.loop()
    assert incoming[0].text == "hello"
    assert "RESULT" in out.getvalue()
    assert "/status" in out.getvalue()


def test_send_is_atomic_and_records_distinct_transcript_event():
    out = io.StringIO()
    cli = CockpitCLIAdapter(Backend(), output=out)
    cli.start(lambda _: None)
    cli.send(AgentOutput("async output"))
    assert cli.transcript_events == [TranscriptEvent("assistant", "async output")]
    assert out.getvalue() == "● Orion\nasync output\n"


def test_concurrent_sends_do_not_interleave():
    out = io.StringIO()
    cli = CockpitCLIAdapter(Backend(), output=out)
    cli.start(lambda _: None)
    threads = [threading.Thread(target=cli.send, args=(AgentOutput(str(i)),)) for i in range(10)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert out.getvalue().count("● Orion\n") == 10
