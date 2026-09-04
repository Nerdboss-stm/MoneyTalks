from pathlib import Path

from mandate.scenario import ROLES, THRESHOLD_CENTS, build_scenario, ct

FIXTURE = Path(__file__).resolve().parents[2] / "shared" / "fixtures" / "scenario_seed7.json"


def test_deterministic_bytes():
    a = build_scenario().to_json().encode()
    b = build_scenario().to_json().encode()
    assert a == b
    assert FIXTURE.read_bytes() == a


def test_shape():
    sc = build_scenario()
    assert len(sc.payments) == 40
    assert [a.id for a in sc.agents] == [r for r, _ in ROLES]
    assert {p.agent_id for p in sc.payments} == {r for r, _ in ROLES}
    payroll_run = next(p for p in sc.payments if p.agent_id == "payroll" and p.amount_cents == 41_200_000)
    assert all(200_000 <= p.amount_cents <= 18_000_000 for p in sc.payments if p.id != payroll_run.id)
    assert len({p.po_number for p in sc.payments}) == 40
    assert len({p.id for p in sc.payments}) == 40
    assert sc.company.cash_cents == 124_000_000


def test_guarantee_halden():
    sc = build_scenario()
    h = [p for p in sc.payments if p.vendor == "Halden Logistics"]
    assert len(h) == 1
    p = h[0]
    assert (p.agent_id, p.amount_cents, p.rail, p.po_number) == ("ap_west", 5_100_000, "ACH", "PO-4471")
    assert p.scheduled_at == ct("Mon 10:03:14")


def test_guarantee_three_others_in_window():
    sc = build_scenario()
    inside = [
        p
        for p in sc.payments
        if p.amount_cents > THRESHOLD_CENTS
        and sc.window_start <= p.scheduled_at < sc.window_end
        and p.vendor != "Halden Logistics"
    ]
    assert len(inside) == 3


def test_guarantee_rest_after_window():
    sc = build_scenario()
    rest = [
        p
        for p in sc.payments
        if p.amount_cents > THRESHOLD_CENTS
        and p.vendor != "Halden Logistics"
        and not (sc.window_start <= p.scheduled_at < sc.window_end)
    ]
    assert rest
    assert all(p.scheduled_at >= sc.window_end for p in rest)


def test_guarantee_exceptions():
    sc = build_scenario()
    by_id = sc.by_id()
    assert {by_id[i].agent_id for i in sc.exception_eligible} == {"payroll", "tax"}
    assert sc.company.payroll.exception_eligible and sc.company.tax.exception_eligible
    assert sc.company.payroll.due == ct("Fri 17:00") and sc.company.tax.due == ct("Mon 17:00")
    payroll_run = by_id[sc.exception_eligible[0]]
    assert payroll_run.scheduled_at == sc.window_end == ct("Fri 17:00")
