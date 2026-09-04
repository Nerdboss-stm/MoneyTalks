import random
from datetime import date, datetime, timedelta

from mandate import boundary
from mandate.agents import Fleet, RuleDecider
from mandate.bus import EventBus
from mandate.compiler import canonical_mandate
from mandate.desk import Desk
from mandate.scenario import CompanyClock, build_scenario, ct
from mandate.schemas import Event, Mandate, Payment


async def _run(version: str, until: str = "Fri 17:05") -> tuple[Fleet, list[Event]]:
    events: list[Event] = []
    bus = EventBus()
    bus.subscribe(events.append)
    fleet = Fleet(build_scenario(), CompanyClock(ct("Mon 09:45")), bus, decider=RuleDecider(), run_version=version, session_id=f"mandate-{version}-test")
    desk = Desk(fleet)
    staged = {}

    async def inject(now):
        staged["id"] = (await desk.inject())["mandate_id"]

    async def confirm(now):
        await desk.confirm(staged["id"])

    fleet.at("Mon 10:02:07", inject)
    fleet.at("Mon 10:02:09", confirm)
    await fleet.run(until)
    return fleet, events


async def test_v2_zero_already_executed_and_holds():
    fleet, events = await _run("v2")
    exposures = [e for e in events if e.type == "exposure.report"]
    assert exposures and all(e.payload["counts"]["already_executed"] == 0 for e in exposures)
    bound = next(e for e in events if e.type == "mandate.bound")
    assert bound.ts_company == ct("Mon 10:02:09")
    held = [e for e in events if e.type == "payment.held"]
    assert len(held) == 3
    by_vendor = {e.payload["vendor"]: e for e in held}
    assert set(by_vendor) == {"Halden Logistics", "Vantage Steel Supply", "Brightline Courier"}
    halden = by_vendor["Halden Logistics"]
    assert halden.ts_company == ct("Mon 10:03:14") and halden.payload["source"] == "boundary"
    stamps = [s for s in fleet.get_record("ap_west")["stamps"] if s["payment_id"] == halden.payload["payment_id"]]
    assert stamps and stamps[0]["at_company"] == "Mon 10:03:14" and stamps[0]["decision"] == "hold"
    assert "fifty thousand" in stamps[0]["transcript_excerpt"]
    executed_over = [e for e in events if e.type == "payment.executed" and e.payload["over_threshold"] and not e.payload["exception_eligible"] and e.ts_company < ct("Fri 17:00")]
    assert executed_over == []


async def test_v2_payroll_and_release():
    fleet, events = await _run("v2", until="Fri 19:15")
    payroll = next(e for e in events if e.type == "payment.executed" and e.payload["payroll_run"])
    assert payroll.ts_company == ct("Fri 17:00")
    assert any(e.type == "payroll.cleared" for e in events)
    assert next(e for e in events if e.type == "mandate.expired").payload["cause"] == "payroll_cleared"
    released = [e for e in events if e.type == "payment.released"]
    assert [e.payload["prior_status"] for e in released].count("held") == 3
    assert [e.payload["prior_status"] for e in released].count("escalated") == 1
    amounts = [e.payload["amount_cents"] for e in released]
    assert amounts == sorted(amounts, reverse=True)
    assert all(e.ts_company == ct("Fri 17:00") for e in released)
    for e in released:
        p = fleet.ledger.payments[e.payload["payment_id"]]
        assert p.status == "executed" and p.executed_at == ct("Fri 17:00") and p.stamp.decision == "allow"
    assert all(p.status == "executed" for p in fleet.ledger.payments.values())
    assert all(p.stamp is not None for p in fleet.ledger.payments.values())


async def test_v1_still_selectable():
    fleet, events = await _run("v1", until="Mon 10:30")
    executed = [e for e in events if e.type == "payment.executed" and e.payload["vendor"] == "Halden Logistics"]
    assert len(executed) == 1 and executed[0].ts_company == ct("Mon 10:03:14")
    surfaced = next(e for e in events if e.type == "exposure.report" and e.payload["already_executed"])
    assert surfaced.payload["already_executed"][0]["seconds_since_order"] == 67.0
    assert surfaced.payload["already_executed"][0]["seconds_since_bound"] == 65.0


def _random_payment(rng: random.Random, i: int) -> Payment:
    day = date(2026, 9, 7) + timedelta(days=rng.randrange(5))
    return Payment(
        id=f"f-{i}", agent_id=rng.choice(["ap_west", "payroll", "fx", "tax"]), vendor=rng.choice(["A", "B", "Halden Logistics"]),
        amount_cents=rng.randrange(1, 60_000_000), rail=rng.choice(["ACH", "WIRE", "CARD"]), po_number="PO-0000",
        settle_date=day, scheduled_at=datetime.combine(day, datetime.min.time()) + timedelta(seconds=rng.randrange(86400)),
    )


def test_fuzz_hold_on_error_and_never_allow_over_threshold():
    rng = random.Random(7)
    m = canonical_mandate(ct("Mon 10:02:07"))
    m.bound_at = ct("Mon 10:02:09")

    broken = Mandate.model_construct(**{**m.model_dump(), "window_start": "corrupt", "threshold_cents": "fifty"})
    for i in range(200):
        p = _random_payment(rng, i)
        v = boundary.check(p, [broken], p.scheduled_at, frozenset(), {"ap_west", "payroll", "fx", "tax"}, {"A", "B", "Halden Logistics"})
        assert v.decision == "hold" and v.mandate_id == "error"
        good = boundary.check(p, [m], p.scheduled_at, frozenset(), {"ap_west", "payroll", "fx", "tax"}, {"A", "B", "Halden Logistics"})
        inside = m.window_start <= p.scheduled_at < m.window_end
        if inside and p.amount_cents > m.threshold_cents:
            assert good.decision in ("hold", "escalate"), (p, good)
            if p.agent_id not in m.exceptions:
                assert good.decision == "hold", (p, good)
        elif inside and p.amount_cents > m.threshold_cents * 0.95:
            assert good.decision == "escalate", (p, good)
        else:
            assert good.decision == "allow", (p, good)
