import csv
import random
from pathlib import Path

from explain import datagen, ingest

OUT = Path(__file__).resolve().parents[2] / "shared" / "explain"
FILES = [OUT / n for n in ("transactions_jul.csv", "transactions_aug.csv", "summary_jul.csv", "summary_aug.csv")]


def test_ingest_datagen_output_is_identical():
    ds = datagen.generate()
    norm = ingest.load(FILES)
    assert {m["kind"] for m in norm.sources} == {"transactions", "summary"}
    assert norm.transactions == ds.transactions
    assert norm.summaries == ds.summaries


def test_transactions_only_derives_summary():
    ds = datagen.generate()
    norm = ingest.load(FILES[:2])
    assert norm.summaries == ds.summaries


def test_shuffled_dollars_recovers_plus_18(tmp_path):
    rng = random.Random(3)
    paths = []
    for src in FILES[:2]:
        with src.open(newline="") as f:
            rows = list(csv.DictReader(f))
        cols = list(rows[0].keys())
        renamed = {"amount_cents": "amt_usd", "date": "posted_on"}
        new_cols = [renamed.get(c, c) for c in cols]
        rng.shuffle(new_cols)
        dst = tmp_path / src.name.replace("transactions", "ledger_export")
        with dst.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=new_cols)
            w.writeheader()
            for r in rows:
                out = {renamed.get(k, k): v for k, v in r.items()}
                out["amt_usd"] = f"{int(r['amount_cents']) / 100:.2f}"
                w.writerow(out)
        paths.append(dst)
    norm = ingest.load(paths)
    src = {m["file"]: m for m in norm.sources}
    assert all(m["amount"] == "amt_usd" and m["amount_unit"] == "dollars" and m["date"] == "posted_on" for m in src.values())
    rev = ingest.revenue_by_period(norm)
    assert abs(rev["2026-08"] / rev["2026-07"] - 1.18) < 0.001
    ds = datagen.generate()
    assert rev["2026-07"] == sum(r["amount_cents"] for r in ds.transactions["2026-07"] if r["category"] in ("subscription", "services", "one_time"))
    assert norm.transactions == ds.transactions


def test_foreign_shape_without_ids_or_owners(tmp_path):
    p = tmp_path / "bank_export_2026-08.csv"
    p.write_text("Posted,Description,Debit,Category,Region\n08/02/2026,Nimbus Cloud Platform,1200.50,cloud_hosting,us-west\n08/03/2026,Payroll run,50000,payroll,us-west\n08/04/2026,Eastfield Packaging,320.25,vendor_spend,us-east\n")
    norm = ingest.load([p])
    rows = norm.transactions["2026-08"]
    assert [r["amount_cents"] for r in rows] == [120050, 5000000, 32025]
    assert [r["owner_agent"] for r in rows] == ["procurement", "payroll", "ap_east"]
    assert all(r["txn_id"].startswith("X2608-") for r in rows)
    assert rows[0]["region"] == "us-west"
    assert "2026-08" in norm.summaries and sum(s["amount_cents"] for s in norm.summaries["2026-08"]) == 5152075
