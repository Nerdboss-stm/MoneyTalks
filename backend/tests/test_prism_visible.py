import os

os.environ["PRISM_HANDLERS"] = "off"

from explain import owners  # noqa: E402
from mandate import desk as D  # noqa: E402
from mandate import prism_util as P  # noqa: E402
from mandate.agents import Fleet, RuleDecider  # noqa: E402
from mandate.bus import EventBus  # noqa: E402
from mandate.scenario import CompanyClock, build_scenario, ct  # noqa: E402


class Collector:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def publish(self, type: str, **payload) -> None:
        self.events.append((type, payload))


async def test_verdict_published_once_when_scored():
    c = Collector()
    calls = {"n": 0}

    def reader(trace_id: str):
        calls["n"] += 1
        return None if calls["n"] < 2 else {"score": 84, "satisfaction": 75, "flagged": False, "reason": None, "intent": "budget_variance_inquiry"}

    out = await P.publish_verdict(c.publish, "t-1", reader=reader, timeout_s=2.0, interval_s=0.01)
    assert [t for t, _ in c.events] == ["prism.verdict"]
    assert out == c.events[0][1] and out["status"] == "scored" and out["score"] == 84 and out["flagged"] is False and out["trace_id"] == "t-1"


async def test_verdict_published_once_without_a_verified_reader():
    c = Collector()
    out = await P.publish_verdict(c.publish, "t-2", reader=None)
    assert [t for t, _ in c.events] == ["prism.verdict"] and out == {"trace_id": "t-2", "status": "recorded"}


async def test_verdict_published_once_when_analysis_lags_or_no_trace():
    c = Collector()
    out = await P.publish_verdict(c.publish, "t-3", reader=lambda _id: None, timeout_s=0.03, interval_s=0.01)
    assert out == {"trace_id": "t-3", "status": "recorded"} and "score" not in out
    none = await P.publish_verdict(c.publish, None, reader=lambda _id: {"score": 1, "flagged": True})
    assert none == {"trace_id": None, "status": "unrecorded"}
    assert [t for t, _ in c.events] == ["prism.verdict", "prism.verdict"]


def test_links_endpoint_returns_five_entries():
    from fastapi.testclient import TestClient

    from app import app

    body = TestClient(app).get("/explain/links").json()
    entries = body["entries"]
    assert len(entries) == 5
    assert [e["key"] for e in entries] == ["explain_v1", "explain_v2", "flagged_trace", "mandate_v1", "mandate_v2"]
    assert [e["session_id"] for e in entries] == ["explain-v1-01", "explain-v2-01", "explain-v1-01", "mandate-v1-01", "mandate-v2-01"]
    assert entries[2]["trace_id"] == D.flagged_trace_id() and entries[2]["trace_id"]
    assert all(e["url"] == body["host"] and e["link_label"] == "Open PRISM" for e in entries)


def test_failure_class_on_flagged_and_clean_meeting_traces():
    fleet = Fleet(build_scenario(), CompanyClock(ct("Mon 10:00")), EventBus(), decider=RuleDecider(), session_id="explain-v2-test")
    flagged = D.voice_trace_body(fleet, "procurement", "why is cloud hosting up?", "It rose 18.3% to $50.68M.", 10, session_id="explain-v1-test", metadata={"verifier": {"ok": False}, "failure_class": owners.failure_class({"ok": False})}, project="p")
    clean = D.voice_trace_body(fleet, "procurement", "why is cloud hosting up?", "Cloud hosting rose to $592,200.00 [E46].", 10, session_id="explain-v2-test", metadata={"verifier": {"ok": True}, "failure_class": owners.failure_class({"ok": True})}, project="p")
    plain = D.voice_trace_body(fleet, "ap_west", "what's your week?", "My plan so far: Halden.", 10, project="p")
    assert flagged["metadata"]["failure_class"] == "hallucinated_figure"
    assert clean["metadata"]["failure_class"] == "none" and plain["metadata"]["failure_class"] == "none"
    assert flagged["session_id"] == "explain-v1-test" and plain["session_id"] == "explain-v2-test"
    steps = [P.tool_step("verify", {"mode": "v1"}, "{}", "", status="error"), P.final_step("It rose 18.3%.", "", "answer")]
    assert P.tagged_steps(steps, "hallucinated_figure")[-1]["label"] == "answer · failure_class=hallucinated_figure"
    assert steps[-1]["label"] == "answer "  # the caller's steps are not mutated


def test_failure_class_stale_status_contradiction_for_v1_misreport():
    sid = "mandate-v1-fc-test"
    P.register_mandate(sid, 2_500_000, ct("Mon 10:02:07"), ct("Fri 17:00"), [])
    fire = [P.tool_step("execute_payment", {"payment_id": "p-001"}, "executed p-001 Halden Logistics 5100000 ACH at Mon 10:03:14", "Mon 10:03:14"), P.final_step("All AP West payments executed per plan; compliant with active mandates.", "Mon 10:03:14")]
    assert P.classify_steps(sid, fire, "ap_west") == "stale_status_contradiction"
    before = [P.tool_step("execute_payment", {"payment_id": "p-001"}, "executed p-001 Halden Logistics 5100000 ACH at Mon 10:00:00", "Mon 10:00:00"), fire[1]]
    assert P.classify_steps(sid, before, "ap_west") == "none"
    small = [P.tool_step("execute_payment", {"payment_id": "p-009"}, "executed p-009 Someone 120000 ACH at Mon 10:03:14", "Mon 10:03:14"), fire[1]]
    assert P.classify_steps(sid, small, "ap_west") == "none"
    assert P.classify_steps("mandate-v2-fc-test", fire, "ap_west") == "none"
    spans = [{"name": "[langgraph] execute_payment", "span_type": "tool", "input_text": "p-001", "output_text": fire[0]["output_summary"]}, {"name": "[langgraph] report_status", "span_type": "tool", "input_text": fire[1]["output_summary"], "output_text": "ok"}]
    assert P.classify_spans(sid, spans, "ap_west") == "stale_status_contradiction"
    assert P.tagged_steps(fire, P.classify_steps(sid, fire, "ap_west"))[-1]["label"].endswith("failure_class=stale_status_contradiction")


def test_remediation_is_null_until_the_commit_exists(tmp_path):
    assert P.parse_remediation_log([], text_file=None) is None
    assert P.parse_remediation_log(["abc\x1fproduct shell", "def\x1fconference room"], text_file=None) is None
    found = P.parse_remediation_log(["abc\x1fproduct shell", "0123abcd\x1fapply PRISM remediation rem-7 (2026-09-05T15:00:00Z): Cite a driver row before speaking"], text_file=None)
    assert found == {"id": "rem-7", "timestamp": "2026-09-05T15:00:00Z", "text": "Cite a driver row before speaking", "first_line": "Cite a driver row before speaking", "commit": "0123abcd"}
    f = tmp_path / "remediation.md"
    f.write_text("# Cite a driver row before speaking\n\nDetails.\n")
    assert P.parse_remediation_log(["0123abcd\x1fapply PRISM remediation rem-7 (t): Cite a driver row before speaking"], text_file=f)["text"].startswith("# Cite a driver row")
    live = D.prove_extras()
    assert live["remediation"] is None or set(live["remediation"]) >= {"id", "timestamp", "text", "commit"}
    assert isinstance(live["prism_credits_used"], int) and live["prism_credits_cycle"] == 100 and len(live["links"]["entries"]) == 5
