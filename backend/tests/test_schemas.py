from datetime import date, datetime

from mandate.schemas import Event, Mandate, Payment, Plan, Stamp

T = datetime(2026, 9, 5, 11, 0, 0)


def _stamp() -> Stamp:
    return Stamp(
        mandate_id="m-01",
        checked_at=T,
        decision="hold",
        reason="over threshold",
        transcript_excerpt="hold anything over fifty thousand",
    )


def _payment() -> Payment:
    return Payment(
        id="p-001",
        agent_id="ap-03",
        vendor="Northwind Freight",
        amount_cents=5_100_000,
        rail="WIRE",
        po_number="PO-44812",
        settle_date=date(2026, 9, 8),
        scheduled_at=T,
    )


def test_stamp():
    s = _stamp()
    assert s.decision == "hold"


def test_payment():
    p = _payment()
    assert p.status == "planned"
    assert p.executed_at is None
    assert p.stamp is None


def test_plan():
    plan = Plan(agent_id="ap-03", payments=[_payment()])
    assert plan.payments[0].agent_id == "ap-03"


def test_mandate():
    m = Mandate(
        id="m-01",
        owner="cfo",
        scope="all",
        threshold_cents=5_000_000,
        window_start=T,
        window_end=datetime(2026, 9, 10, 11, 0, 0),
        exceptions=["payroll"],
        precedence=1,
        transcript="hold anything over fifty thousand until Friday",
        compiled_text="HOLD amount > 50000.00 USD scope=all until 2026-09-10",
    )
    assert m.bound_at is None
    assert m.exceptions == ["payroll"]


def test_event():
    e = Event(type="payment.executed", ts_wall=T, ts_company=T, payload={"id": "p-001"})
    assert e.payload["id"] == "p-001"
