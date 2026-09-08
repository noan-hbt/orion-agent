from cli_cockpit_events import Transcript
from channels import AgentOutput


def out(text, *, corr, event="message", **meta):
    return AgentOutput(text, correlation_id=corr, event_id="turn-1",
                       metadata={"event_type": event, **meta})


def test_reused_event_id_keeps_progress_and_final_and_replays_are_ignored():
    t = Transcript()
    t.add(out("preparing", corr="a", event="subagent.progress", progress=True))
    t.add(out("answer", corr="a", event="message"))
    t.add(out("answer", corr="a", event="message"))  # exact replay
    t.add(out("done", corr="a", event="subagent.completed", final=True))
    assert len(t.progress) == 1
    assert len(t.messages) == 2
    assert t.final_text("a") == "done"


def test_deltas_recompose_independently_for_concurrent_correlations():
    t = Transcript()
    for item in (out("A1", corr="a", delta=True, sequence=1),
                 out("B1", corr="b", delta=True, sequence=1),
                 out("A2", corr="a", delta=True, sequence=2),
                 out("B2", corr="b", delta=True, sequence=2)):
        t.add(item)
    assert t.final_text("a") == "A1A2"
    assert t.final_text("b") == "B1B2"


def test_failed_final_is_visible_and_replaces_partial():
    t = Transcript()
    t.add(out("partial", corr="x", delta=True, sequence=1))
    t.add(out("boom", corr="x", event="subagent.failed", final=True))
    assert t.final_text("x") == "boom"
    assert t.messages[-1].state is None

