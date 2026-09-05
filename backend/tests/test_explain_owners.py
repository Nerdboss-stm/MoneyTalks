import asyncio
import os
import re
from pathlib import Path

import pytest

from explain import datagen, meeting, owners, verify
from explain.engine import Engine
from mandate import desk as D


def _engine() -> Engine:
    ds = datagen.generate()
    return Engine.from_periods(ds.transactions, ds.summaries)


def _llm_available() -> bool:
    from mandate.runner import load_env

    load_env()
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


async def test_change_question_routes_to_controller_a():
    for q in ("What changed in August?", "Walk me through the month.", "what moved versus July", "Why did revenue jump?"):
        intent = await D.classify(q, D.rules_classify_async)
        assert intent.kind == "CHANGE_QUERY" and intent.agent_id == "controller_a", q
    named = await D.classify("Procurement, what changed in August?", D.rules_classify_async)
    assert named.kind == "AGENT_QUERY" and named.agent_id == "procurement"
    plain = await D.classify("how much cash do we have?", D.rules_classify_async)
    assert plain.kind == "QUERY"


def test_extract_and_check():
    figs = verify.extract_figures("Revenue rose 18.0% to $8,850,000.00 (+$1.35M) in 2026-08; 3 customers [E4] and 64% of it.")
    assert [(f.kind, f.value) for f in figs] == [("percent", 18.0), ("dollar", 8_850_000.0), ("dollar", 1_350_000.0), ("number", 3.0), ("percent", 64.0)]
    e = _engine()
    slice_ = e.slice_for_owner("procurement")
    bad = verify.check("Cloud hosting rose 41% to $592,200.00 driven by $99,999.00 from a new vendor [E1].", slice_)
    assert bad["unverifiable_figures"] == ["$99,999.00"] and not bad["ok"]
    top = owners.top_driver(slice_)
    good = verify.check(f"{top['statement']} [{top['id']}]", slice_)
    assert good["ok"] and good["driver_coverage_pct"] >= 50 and good["citations"] == [top["id"]]
    missing = verify.check("It went up 41% [E999].", slice_)
    assert missing["unresolved_citations"] == ["E999"] and not missing["ok"]
    assert verify.check(owners.NO_RECORDS, slice_)["ok"]


async def test_v2_offline_fallback_is_grounded_and_cited():
    e = _engine()
    for agent in ("procurement", "controller_a", "controller_b", "payroll", "collections"):
        res = await owners.answer_as_owner(agent, "what moved?", "v2", "", e, llm=None)
        assert res["verify"]["ok"] and not res["verify"]["unverifiable_figures"], agent
        assert re.search(r"\[E\d+\]$", res["answer"]), res["answer"]
        ids = {r["id"] for r in e.slice_for_owner(agent)["evidence"]}
        assert set(res["citations"]) <= ids
        assert [s["step_type"] for s in res["steps"]] == ["tool_call", "reasoning", "tool_call", "final_answer"]


@pytest.mark.skipif(not _llm_available(), reason="needs ANTHROPIC_API_KEY")
async def test_v1_freeform_produces_unverifiable_figures():
    e = _engine()
    for limit in (150, 80):
        hits = 0
        for _ in range(3):
            res = await owners.answer_as_owner("controller_a", "what drove enterprise revenue in percentage terms?", "v1", "", e, rows_limit=limit)
            if res["verify"]["unverifiable_figures"]:
                hits += 1
        if hits >= 2:
            break
    assert hits >= 2, f"v1 stayed verifiable at rows_limit={limit}: {hits}/3"


@pytest.mark.skipif(not _llm_available(), reason="needs ANTHROPIC_API_KEY")
async def test_v2_grounded_three_runs_zero_unverifiable():
    e = _engine()
    ids = {r["id"] for r in e.slice_for_owner("procurement")["evidence"]}
    for _ in range(3):
        res = await owners.answer_as_owner("procurement", "why is cloud hosting up?", "v2", owners.read_memory(), e)
        assert res["verify"]["ok"] and res["verify"]["unverifiable_figures"] == [], res
        assert res["citations"] and set(res["citations"]) <= ids, res["answer"]
        assert res["verify"]["driver_coverage_pct"] >= 50


async def test_memory_grows_one_line_per_v2_run(tmp_path):
    mem = tmp_path / "context-memory.md"

    async def grounded(system, user, temperature):
        return owners.NO_RECORDS

    for i in (1, 2):
        out = await meeting.run_meeting("v2", i, meeting.DATA, llm=grounded, memory_path=mem, questions=["What changed in August?", "Procurement, why is cloud hosting up?"])
        assert out["learned"] and "Northgate Financial" in out["learned"]
        lines = [l for l in mem.read_text().splitlines() if l.startswith("- ")]
        assert len(lines) == i
    v1 = await meeting.run_meeting("v1", 1, meeting.DATA, llm=grounded, memory_path=mem, questions=["Payroll, anything to flag?"])
    assert v1["learned"] is None and len([l for l in mem.read_text().splitlines() if l.startswith("- ")]) == 2
    for sid in ("explain-v2-01", "explain-v2-02", "explain-v1-01"):
        assert (meeting.RECORDINGS / f"{sid}.jsonl").exists()


async def test_voice_path_speaks_v2_answer_end_to_end():
    from mandate.agents import Fleet, RuleDecider
    from mandate.bus import EventBus
    from mandate.scenario import CompanyClock, build_scenario, ct

    events = []
    bus = EventBus()
    bus.subscribe(events.append)
    fleet = Fleet(build_scenario(), CompanyClock(ct("Mon 10:00")), bus, decider=RuleDecider(), session_id="explain-v2-test")
    desk = D.Desk(fleet)
    desk.load_meeting(meeting.DATA, "v2", 1)
    spoken = []

    async def speak(text, voice_id):
        spoken.append((text, voice_id))
        return meeting.QuietJob(text, voice_id)

    out = await desk.voice_turn("Procurement, why is cloud hosting up?", speak=speak, explain_llm=None)
    assert out["intent"] == "AGENT_QUERY" and out["agent_id"] == "procurement" and out["mode"] == "v2"
    assert out["verify"]["ok"] and spoken == [(out["answer"], owners.__dict__ and __import__("mandate.voice", fromlist=["VOICES"]).VOICES.for_agent("procurement"))]
    assert "2 vendors account for 78%" in out["answer"] or "[E" in out["answer"]
    kinds = [e.type for e in events]
    assert kinds.index("agent.addressed") < kinds.index("agent.answer")
    ans = next(e for e in events if e.type == "agent.answer")
    assert ans.payload["text"] == out["answer"] and ans.payload["verify"]["ok"] and ans.payload["mode"] == "v2"
    change = await desk.voice_turn("What changed in August?", speak=speak, explain_llm=None)
    assert change["intent"] == "CHANGE_QUERY" and change["agent_id"] == "controller_a" and change["verify"]["ok"]
