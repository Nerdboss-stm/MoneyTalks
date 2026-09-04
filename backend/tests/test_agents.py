import json

from mandate.agents import Fleet, RuleDecider
from mandate.bus import EventBus
from mandate.scenario import CompanyClock, build_scenario, ct
from mandate.schemas import Event, Mandate

MANDATE_TEXT = "hold anything over fifty thousand until Friday close, except payroll and tax"


def _mandate() -> Mandate:
    return Mandate(
        id="m-stub",
        owner="cfo",
        scope="all",
        threshold_cents=5_000_000,
        window_start=ct("Mon 10:02:07"),
        window_end=ct("Fri 17:00"),
        exceptions=["payroll", "tax"],
        precedence=1,
        transcript=MANDATE_TEXT,
        compiled_text="HOLD amount > 5000000 cents scope=all window Mon 10:02:07..Fri 17:00 except payroll,tax",
    )


def _fleet(events: list[Event]) -> tuple[Fleet, RuleDecider]:
    bus = EventBus()
    bus.subscribe(events.append)
    decider = RuleDecider()
    fleet = Fleet(build_scenario(), CompanyClock(ct("Mon 09:00")), bus, decider=decider)
    return fleet, decider


async def test_no_mandate_all_forty_execute_on_schedule():
    events: list[Event] = []
    fleet, _ = _fleet(events)
    await fleet.run("Fri 19:15")
    for p in fleet.ledger.payments.values():
        assert p.status == "executed", p
        assert p.executed_at == p.scheduled_at, p
    assert sum(e.type == "payment.executed" for e in events) == 40


async def test_stub_mandate_race():
    events: list[Event] = []
    fleet, _ = _fleet(events)
    fleet.schedule_mandate(_mandate(), "Mon 10:02:07")
    await fleet.run("Fri 19:15")
    sc = fleet.scenario
    executed_over = [
        p
        for p in fleet.ledger.payments.values()
        if p.status == "executed"
        and p.amount_cents > sc.threshold_cents
        and sc.window_start <= p.scheduled_at < sc.window_end
        and p.id not in sc.exception_eligible
    ]
    assert [p.vendor for p in executed_over] == ["Halden Logistics"]
    assert executed_over[0].executed_at == ct("Mon 10:03:14")
    assert executed_over[0].stamp is not None and executed_over[0].stamp.mandate_id == "none"
    held = [p for p in fleet.ledger.payments.values() if p.status == "held"]
    assert [p.vendor for p in held] == ["Vantage Steel Supply"]
    assert all(fleet.ledger.payments[i].status == "executed" for i in sc.exception_eligible)
    reports = [e for e in events if e.type == "status.report" and e.payload["agent_id"] == "ap_west"]
    stale = [e for e in reports if e.payload["source"] == "executor" and e.ts_company == ct("Mon 10:03:14")]
    assert len(stale) == 1
    assert "compliant with active mandates" in stale[0].payload["sentence"]
    assert stale[0].payload["scheduled_at_tick"] == "Mon 10:00:00"


async def test_record_as_of():
    events: list[Event] = []
    fleet, _ = _fleet(events)
    fleet.schedule_mandate(_mandate(), "Mon 10:02:07")
    await fleet.run("Mon 12:00")
    rec = fleet.get_record("ap_west", as_of="10:04:00")
    stale = [r for r in rec["reports"] if r["source"] == "executor"]
    assert len(stale) == 1 and "compliant with active mandates" in stale[0]["sentence"]
    execs = [c for c in rec["tool_calls"] if c["tool"] == "execute_payment"]
    assert len(execs) == 1 and execs[0]["at_company"] == "Mon 10:03:14"
    blob = json.dumps(rec)
    assert MANDATE_TEXT not in blob and "m-stub" not in blob
    assert rec["mandates_seen"] == []
    later = fleet.get_record("ap_west", as_of="10:16:00")
    assert any(m["mandate_id"] == "m-stub" for m in later["mandates_seen"])


async def test_executor_never_calls_llm():
    events: list[Event] = []
    fleet, decider = _fleet(events)
    fleet.schedule_mandate(_mandate(), "Mon 10:02:07")
    await fleet.run("Fri 19:15")
    assert decider.calls
    assert all(t.second == 0 and t.minute % 15 == 0 for t in decider.calls)
    exec_times = {e.ts_company for e in events if e.type == "payment.executed"}
    off_tick = [t for t in exec_times if not (t.second == 0 and t.minute % 15 == 0)]
    assert off_tick
    assert not any(t in decider.calls for t in off_tick)
