import asyncio
import os

os.environ["PRISM_HANDLERS"] = "off"

from mandate import prism_util  # noqa: E402
from mandate.agents import Fleet, RuleDecider  # noqa: E402
from mandate.bus import EventBus  # noqa: E402
from mandate.scenario import CompanyClock, build_scenario, ct  # noqa: E402


async def test_v1_tick_builds_expected_steps():
    fleet = Fleet(build_scenario(), CompanyClock(ct("Mon 09:45")), EventBus(), decider=RuleDecider(), session_id="mandate-v1-test")
    await fleet.run("Mon 10:00")
    ap_west = fleet.agents["ap_west"]
    steps = ap_west.last_steps
    assert [s["step_type"] for s in steps] == ["tool_call", "tool_call", "tool_call", "final_answer"]
    assert [s["tool_name"] for s in steps[:-1]] == ["read_cash", "read_mandates", "schedule_payment"]
    sched = steps[2]
    assert "p-001" in sched["input_summary"] and "10:03:14" in sched["output_summary"]
    assert sched["label"] == "schedule_payment Mon 10:00:00"
    assert steps[-1]["label"].startswith("report_status Mon 10:00:00")
    assert "compliant with active mandates" in steps[-1]["output_summary"]
    assert all(s["status"] == "success" and s["token_count"] == 0 for s in steps)
    assert ap_west.last_trace_id is None  # handlers off: no PRISM trace id, nothing enqueued
    assert fleet.prism.submitted == 0 and fleet.prism.failed == 0
    assert fleet.agents["treasury"].last_steps[-1]["step_type"] == "final_answer"


async def test_executor_fire_builds_one_step():
    fleet = Fleet(build_scenario(), CompanyClock(ct("Mon 09:45")), EventBus(), decider=RuleDecider(), session_id="mandate-v1-test")
    await fleet.run("Mon 10:04")
    steps = fleet.agents["ap_west"].last_steps
    assert steps[0]["tool_name"] == "execute_payment" and "Halden Logistics" in steps[0]["output_summary"]
    assert steps[-1]["step_type"] == "final_answer" and steps[-1]["label"].startswith("report_status Mon 10:03:14")


async def test_queue_never_raises_and_logs_once(capsys):
    q = prism_util.PrismQueue()
    prism_util.BACKOFF = (0.01, 0.02)

    def boom():
        raise RuntimeError("no network")

    q.enqueue("trajectory", boom)
    q.enqueue("trajectory", boom)
    await q.drain()
    assert q.failed == 2 and q.submitted == 0
    err = capsys.readouterr().err
    assert err.count("prism trajectory: giving up") == 1


async def test_queue_retries_then_succeeds():
    q = prism_util.PrismQueue()
    prism_util.BACKOFF = (0.01, 0.02)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("read timed out")
        return {"id": "ok"}

    q.enqueue("trajectory", flaky)
    await q.drain()
    assert calls["n"] == 3 and q.submitted == 1 and q.failed == 0


class FakeHandler:
    def __init__(self):
        self._open_spans = {"r1": {"span_id": "s1", "name": "[langgraph] read", "span_type": "chain", "start_time": "t0"}}
        self._spans = [{"span_id": "s2", "name": "[langgraph] read_cash", "span_type": "tool", "start_time": "t0", "end_time": "t1"}]
        self.trace_id = "trace-1"
        self.project_id = "proj"
        self.session_id = "mandate-v2-test"
        self.source = "langgraph"
        self.agent_name = "ap_west"

    def _now_iso(self):
        return "t2"


async def test_detach_flush_posts_sdk_payload_through_queue():
    q = prism_util.PrismQueue()
    posted: list[dict] = []
    h = prism_util.detach_flush(FakeHandler(), q, poster=lambda payload: posted.append(payload) or {"ok": True})
    assert h.flush() is True
    assert h._spans == [] and h.trace_id != "trace-1"
    assert h.flush() is False  # empty buffer, nothing enqueued
    await q.drain()
    assert len(posted) == 1
    p = posted[0]
    assert p["trace_id"] == "trace-1" and p["project_id"] == "proj" and p["session_id"] == "mandate-v2-test"
    assert p["metadata"] == {"source": "langgraph", "agent_name": "ap_west", "failure_class": "none"}
    assert [s["name"] for s in p["spans"]] == ["[langgraph] read_cash", "[langgraph] read"]
    assert p["spans"][1]["end_time"] == "t2"
    assert q.submitted == 1


def test_build_steps_is_pure():
    calls = [{"tool": "read_cash", "args": {}, "result": "124000000", "at_company": "Mon 10:00:00"}, {"tool": "report_status", "args": {"sentence": "x"}, "result": "ok", "at_company": "Mon 10:00:00"}]
    steps = prism_util.build_steps(calls, "All good; compliant with active mandates.", "Mon 10:00:00")
    assert len(steps) == 2 and steps[0]["tool_name"] == "read_cash" and steps[1]["step_type"] == "final_answer"
    assert asyncio.iscoroutinefunction(prism_util.PrismQueue.drain)
