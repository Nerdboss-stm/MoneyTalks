import csv
import json
import random
from pathlib import Path

from explain import datagen, ingest
from explain.engine import Engine

OUT = Path(__file__).resolve().parents[2] / "shared" / "explain"
GT = json.loads((OUT / "ground_truth.json").read_text())
DRIVERS = {d["id"]: d for d in GT["drivers"]}


def _engine() -> Engine:
    ds = datagen.generate()
    return Engine.from_periods(ds.transactions, ds.summaries)


def _by_key(variances, key):
    return next(v for v in variances if v.key == key)


def test_total_revenue_18():
    e = _engine()
    v = _by_key(e.compare(), "TOTAL:revenue")
    assert v.prior == DRIVERS["total_revenue"]["jul"] and v.current == DRIVERS["total_revenue"]["aug"]
    assert abs(v.delta_pct - 18.0) < 0.01
    ranks = [x.rank for x in e.compare() if not x.key.startswith("TOTAL:")]
    assert ranks == list(range(1, len(ranks) + 1))
    top = [x for x in e.compare() if x.rank == 1][0]
    assert top.key == "4010-SUB-ENT"


def test_enterprise_32_and_three_customers_64():
    e = _engine()
    drilled = e.drill("TOTAL:revenue")
    ent = next(c for c in drilled["segment"] if c.value == "enterprise")
    assert abs(ent.pct - 32.0) < 0.01
    d = e.drivers("TOTAL:revenue")
    conc = next(k for k in d["concentration"] if k.dimension == "customer")
    named = {c["customer"] for c in DRIVERS["enterprise_revenue"]["named_customers"]}
    assert conc.k == 3 and set(conc.values) == named
    assert abs(conc.share - 64.0) <= 1.0
    assert "3 customers account for 64% of the increase" in conc.statement()
    assert d["drivers"][0].dimension == "segment" and d["drivers"][0].value == "enterprise"
    offsets = {(c.dimension, c.value) for c in d["offsets"]}
    assert ("segment", "smb") in offsets or ("category", "subscription") not in offsets


def test_smb_subscription_minus_9():
    e = _engine()
    v = _by_key(e.compare(), "4010-SUB-SMB")
    assert abs(v.delta_pct + 9.0) < 0.01 and v.direction == "down" and v.owner_agent == "controller_b"


def test_cloud_hosting_41_with_78_vendor_concentration():
    e = _engine()
    v = _by_key(e.compare(), "6100-CLOUD-HOSTING")
    assert abs(v.delta_pct - 41.0) < 0.01 and v.owner_agent == "procurement"
    d = e.drivers("6100-CLOUD-HOSTING")
    conc = next(k for k in d["concentration"] if k.dimension == "vendor")
    assert conc.k == 2 and set(conc.values) == {v["vendor"] for v in DRIVERS["cloud_hosting"]["named_vendors"]}
    assert abs(conc.share - 78.0) <= 1.0
    assert all(c.dimension != "customer" for cs in e.drill("6100-CLOUD-HOSTING").values() for c in cs)


def test_payroll_flat_and_evidence_rows():
    e = _engine()
    v = _by_key(e.compare(), "6000-PAYROLL")
    assert abs(v.delta_pct) < 1.0
    ev = e.evidence()
    assert [x.id for x in ev] == [f"E{i}" for i in range(1, len(ev) + 1)]
    kinds = {x.kind for x in ev}
    assert kinds == {"variance", "driver", "offset", "concentration"}
    assert any("Total revenue rose 18.0%" in x.statement for x in ev)
    assert any("3 customers account for 64% of the increase" in x.statement for x in ev)
    assert any("2 vendors account for 78% of the increase" in x.statement for x in ev)
    assert all(x.txn_ids for x in ev)
    js = e.to_evidence_json()
    assert set(js) == {"periods", "variances", "drivers", "offsets", "concentration", "evidence"}
    assert js["periods"] == {"prior": "2026-07", "current": "2026-08"}
    json.dumps(js)


def test_slice_for_owner_isolation():
    e = _engine()
    proc = e.slice_for_owner("procurement")
    assert proc["accounts"] == ["6100-CLOUD-HOSTING"]
    assert abs(proc["totals"]["delta_pct"] - 41.0) < 0.01
    assert proc["evidence"] and all(x["target"] == "6100-CLOUD-HOSTING" and x["owners"] == ["procurement"] for x in proc["evidence"])
    assert any("2 vendors account for 78%" in x["statement"] for x in proc["evidence"])
    ca = e.slice_for_owner("controller_a")
    assert set(ca["accounts"]) == {"4010-SUB-ENT", "4100-SVC-ENT", "4200-ONE-ENT"}
    proc_ids = {t for x in proc["evidence"] for t in x["txn_ids"]}
    ca_ids = {t for x in ca["evidence"] for t in x["txn_ids"]}
    assert not (proc_ids & ca_ids)
    assert not any("TOTAL:" in x["target"] for x in proc["evidence"] + ca["evidence"])
    assert e.slice_for_owner("fx")["accounts"] == ["4200-ONE-MM"]


def test_engine_on_scrambled_ingest(tmp_path):
    rng = random.Random(11)
    paths = []
    for name in ("transactions_jul.csv", "transactions_aug.csv"):
        with (OUT / name).open(newline="") as f:
            rows = list(csv.DictReader(f))
        renamed = {"amount_cents": "amt_usd", "date": "posted_on", "vendor": "supplier", "customer": "client"}
        cols = [renamed.get(c, c) for c in rows[0]]
        rng.shuffle(cols)
        dst = tmp_path / name.replace("transactions", "export")
        with dst.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                out = {renamed.get(k, k): v for k, v in r.items()}
                out["amt_usd"] = f"{int(r['amount_cents']) / 100:.2f}"
                w.writerow(out)
        paths.append(dst)
    norm = ingest.load(paths)
    e = Engine.from_periods(norm.transactions, norm.summaries)
    assert abs(_by_key(e.compare(), "TOTAL:revenue").delta_pct - 18.0) < 0.01
    conc = next(k for k in e.drivers("TOTAL:revenue")["concentration"] if k.dimension == "customer")
    assert conc.k == 3 and abs(conc.share - 64.0) <= 1.0
    assert abs(_by_key(e.compare(), "6100-CLOUD-HOSTING").delta_pct - 41.0) < 0.01
    assert e.slice_for_owner("procurement")["evidence"]
    assert e.to_evidence_json()["evidence"]
