import pytest

from mandate import ledger as L
from mandate.scenario import build_scenario, ct
from mandate.schemas import Stamp


def _stamp(decision: str) -> Stamp:
    return Stamp(mandate_id="m-01", checked_at=ct("10:00"), decision=decision, reason="r", transcript_excerpt="t")


def test_execute_is_pure_and_debits():
    sc = build_scenario()
    led = L.from_scenario(sc)
    pid = next(p.id for p in sc.payments if p.vendor == "Halden Logistics")
    new = L.execute(led, pid, ct("10:03:14"), _stamp("allow"))
    assert L.balance(new) == 124_000_000 - 5_100_000
    assert new.payments[pid].status == "executed"
    assert new.payments[pid].executed_at == ct("10:03:14")
    assert led.payments[pid].status == "planned"
    assert L.balance(led) == 124_000_000


def test_hold_escalate_release():
    sc = build_scenario()
    led = L.from_scenario(sc)
    pid = sc.payments[0].id
    held = L.hold(led, pid, ct("10:00"), _stamp("hold"))
    assert held.payments[pid].status == "held"
    esc = L.escalate(held, pid, ct("10:01"), _stamp("escalate"))
    assert esc.payments[pid].status == "escalated"
    rel = L.release(esc, pid, ct("10:02"), _stamp("release"))
    assert rel.payments[pid].status == "planned"
    with pytest.raises(ValueError):
        L.release(rel, pid, ct("10:03"))
    with pytest.raises(ValueError):
        L.execute(L.execute(led, pid, ct("10:00")), pid, ct("10:01"))


def test_payroll_at_risk():
    sc = build_scenario()
    led = L.from_scenario(sc)
    payroll = sc.company.payroll
    exclude = frozenset(sc.exception_eligible)
    assert L.payroll_at_risk(led, payroll.amount_cents, payroll.due, exclude)
    rich = L.Ledger(cash_cents=1_000_000_000, payments=led.payments)
    assert not L.payroll_at_risk(rich, payroll.amount_cents, payroll.due, exclude)
    assert L.committed_before(led, payroll.due, exclude) < L.committed_before(led, payroll.due)
