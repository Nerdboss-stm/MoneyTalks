import json

from mandate.agents import OPENING_TICK, Fleet, RuleDecider
from mandate.bus import EventBus
from mandate.scenario import CompanyClock, build_scenario, ct
from mandate.schemas import Event, Mandate


def _mandate() -> Mandate:
    return Mandate(
        id="m-stub", owner="cfo", scope="all", threshold_cents=5_000_000, window_start=ct("Mon 10:02:07"), window_end=ct("Fri 17:00"),
        exceptions=["payroll", "tax"], precedence=1, transcript="hold anything over fifty thousand", compiled_text="HOLD > 5000000",
    )


async def _v1() -> tuple[Fleet, RuleDecider, list[Event]]:
    events: list[Event] = []
    bus = EventBus()
    bus.subscribe(events.append)
    decider = RuleDecider()
    fleet = Fleet(build_scenario(), CompanyClock(ct("Mon 09:45")), bus, decider=decider)
    fleet.schedule_mandate(_mandate(), "Mon 10:02:07")
    await fleet.run("Fri 19:15")
    return fleet, decider, events


async def test_v1_fewer_than_150_decisions():
    fleet, decider, events = await _v1()
    assert len(decider.calls) < 150, len(decider.calls)
    assert decider.calls.count(OPENING_TICK) == 12
    bind_next = ct("Mon 10:15")
    assert decider.calls.count(bind_next) == 12
    assert all(p.status == "executed" for p in fleet.ledger.payments.values())
    total_ticks = sum(e.type == "tick" for e in events)
    assert total_ticks * 12 > 2000
    assert sum(e.type == "status.report" and e.payload["source"] == "tick" for e in events) == len(decider.calls)


async def test_executor_unaffected_and_skips_have_no_trace():
    fleet, decider, events = await _v1()
    executed = [e for e in events if e.type == "payment.executed"]
    assert len(executed) == 40
    tool_calls = [c for a in fleet.agents.values() for c in a.record["tool_calls"] if c["tool"] in ("read_cash", "read_mandates")]
    assert len(tool_calls) == 2 * len(decider.calls)


def _strip(events: list[Event]) -> list[str]:
    return [json.dumps({"type": e.type, "ts_company": e.ts_company.isoformat(), "payload": e.payload}, sort_keys=True, default=str) for e in events]


async def test_rule_decider_recordings_deterministic():
    _, _, a = await _v1()
    _, _, b = await _v1()
    assert _strip(a) == _strip(b)
