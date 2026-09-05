import json
from pathlib import Path

from explain import datagen

OUT = Path(__file__).resolve().parents[2] / "shared" / "explain"
REV = ("subscription", "services", "one_time")


def _ds():
    return datagen.generate()


def _tot(rows, pred):
    return sum(r["amount_cents"] for r in rows if pred(r))


def test_byte_identical_and_committed():
    a = datagen.files(datagen.generate())
    b = datagen.files(datagen.generate())
    assert a == b
    for name, text in a.items():
        assert (OUT / name).read_text(encoding="utf-8") == text, name


def test_shape():
    ds = _ds()
    for period in ("2026-07", "2026-08"):
        rows = ds.transactions[period]
        assert 600 <= len(rows) <= 900, len(rows)
        assert [list(r) for r in rows[:1]][0] == datagen.TXN_COLUMNS
        assert len({r["txn_id"] for r in rows}) == len(rows)
        assert all(r["date"].startswith(period) for r in rows)
        assert all(r["owner_agent"] in datagen.owners.AGENT_IDS for r in rows)


def test_summary_reconciles():
    ds = _ds()
    for period in ("2026-07", "2026-08"):
        rows = ds.transactions[period]
        summary = ds.summaries[period]
        assert sum(s["amount_cents"] for s in summary) == sum(r["amount_cents"] for r in rows)
        assert sum(s["txn_count"] for s in summary) == len(rows)
        by_acct = {}
        for r in rows:
            by_acct[r["account"]] = by_acct.get(r["account"], 0) + r["amount_cents"]
        for acct, cents in by_acct.items():
            assert sum(s["amount_cents"] for s in summary if s["account"] == acct) == cents


def test_total_revenue_plus_18():
    ds = _ds()
    jul = _tot(ds.transactions["2026-07"], lambda r: r["category"] in REV)
    aug = _tot(ds.transactions["2026-08"], lambda r: r["category"] in REV)
    assert abs(aug / jul - 1.18) < 0.001


def test_enterprise_plus_32_and_three_named_customers():
    ds = _ds()
    jul_all = _tot(ds.transactions["2026-07"], lambda r: r["category"] in REV)
    aug_all = _tot(ds.transactions["2026-08"], lambda r: r["category"] in REV)
    ent_jul = _tot(ds.transactions["2026-07"], lambda r: r["segment"] == "enterprise" and r["category"] in REV)
    ent_aug = _tot(ds.transactions["2026-08"], lambda r: r["segment"] == "enterprise" and r["category"] in REV)
    assert abs(ent_aug / ent_jul - 1.32) < 0.001
    named = [n for n, _ in datagen.NAMED_CUSTOMERS]
    d = sum(_tot(ds.transactions["2026-08"], lambda r, n=n: r["customer"] == n) - _tot(ds.transactions["2026-07"], lambda r, n=n: r["customer"] == n) for n in named)
    assert abs(d / (aug_all - jul_all) - 0.64) < 0.001
    assert all(r["owner_agent"] == "controller_a" for p in ds.transactions.values() for r in p if r["segment"] == "enterprise")
    gt = ds.ground_truth["drivers"][1]
    assert {c["customer"] for c in gt["named_customers"]} == set(named) and gt["owner"] == "controller_a"


def test_smb_subscription_minus_9():
    ds = _ds()
    f = lambda r: r["segment"] == "smb" and r["category"] == "subscription"
    jul, aug = _tot(ds.transactions["2026-07"], f), _tot(ds.transactions["2026-08"], f)
    assert abs(aug / jul - 0.91) < 0.001
    assert all(r["owner_agent"] == "controller_b" for p in ds.transactions.values() for r in p if f(r))


def test_cloud_hosting_plus_41_two_vendors():
    ds = _ds()
    f = lambda r: r["category"] == "cloud_hosting"
    jul, aug = _tot(ds.transactions["2026-07"], f), _tot(ds.transactions["2026-08"], f)
    assert abs(aug / jul - 1.41) < 0.001
    named = [v for v, _ in datagen.NAMED_VENDORS]
    d = sum(_tot(ds.transactions["2026-08"], lambda r, v=v: r["vendor"] == v) - _tot(ds.transactions["2026-07"], lambda r, v=v: r["vendor"] == v) for v in named)
    assert abs(d / (aug - jul) - 0.78) < 0.001
    assert all(r["owner_agent"] == "procurement" for p in ds.transactions.values() for r in p if f(r))


def test_payroll_flat_within_1():
    ds = _ds()
    f = lambda r: r["category"] == "payroll"
    jul, aug = _tot(ds.transactions["2026-07"], f), _tot(ds.transactions["2026-08"], f)
    assert abs(aug / jul - 1) < 0.01
    assert all(r["owner_agent"] == "payroll" for p in ds.transactions.values() for r in p if f(r))


def test_everything_else_within_4():
    ds = _ds()
    planted = {"6100-CLOUD-HOSTING", "6000-PAYROLL", "4010-SUB-SMB", "4010-SUB-ENT", "4100-SVC-ENT", "4200-ONE-ENT"}
    accounts = {r["account"] for r in ds.transactions["2026-07"]}
    checked = 0
    for acct in accounts - planted:
        jul = _tot(ds.transactions["2026-07"], lambda r, a=acct: r["account"] == a)
        aug = _tot(ds.transactions["2026-08"], lambda r, a=acct: r["account"] == a)
        assert abs(aug / jul - 1) <= 0.04, (acct, aug / jul)
        checked += 1
    assert checked == 6


def test_ground_truth_file():
    gt = json.loads((OUT / "ground_truth.json").read_text())
    assert gt["seed"] == 7 and gt["periods"] == ["2026-07", "2026-08"]
    ids = [d["id"] for d in gt["drivers"]]
    assert ids == ["total_revenue", "enterprise_revenue", "smb_subscription", "cloud_hosting", "payroll", "noise"]
