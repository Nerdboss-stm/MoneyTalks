"""Seeded two-month ledger with planted drivers. Same seed, byte-identical CSVs."""

from __future__ import annotations

import csv
import io
import json
import random
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from explain import owners
from mandate.scenario import MANDATE_SEED

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "shared" / "explain"

PERIODS: list[tuple[str, date, int, str]] = [("2026-07", date(2026, 7, 1), 31, "jul"), ("2026-08", date(2026, 8, 1), 31, "aug")]
SEGMENTS = ["enterprise", "mid_market", "smb"]
REV_CATS = ["subscription", "services", "one_time"]
EXP_CATS = ["cloud_hosting", "payroll", "vendor_spend"]
TXN_COLUMNS = ["txn_id", "date", "customer", "segment", "category", "account", "vendor", "amount_cents", "owner_agent"]
SUMMARY_COLUMNS = ["period", "account", "category", "segment", "owner_agent", "txn_count", "amount_cents"]

ACCOUNT_CODE = {"subscription": "4010-SUB", "services": "4100-SVC", "one_time": "4200-ONE"}
SEG_CODE = {"enterprise": "ENT", "mid_market": "MM", "smb": "SMB"}
EXP_ACCOUNT = {"cloud_hosting": "6100-CLOUD-HOSTING", "payroll": "6000-PAYROLL", "vendor_spend": "6200-VENDOR-SPEND"}

# ---- planted drivers (cents, July baselines)
REV_BASE: dict[tuple[str, str], int] = {
    ("enterprise", "subscription"): 290_000_000,
    ("enterprise", "services"): 105_000_000,
    ("enterprise", "one_time"): 40_000_000,
    ("mid_market", "subscription"): 120_000_000,
    ("mid_market", "services"): 45_000_000,
    ("mid_market", "one_time"): 20_000_000,
    ("smb", "subscription"): 90_000_000,
    ("smb", "services"): 25_000_000,
    ("smb", "one_time"): 15_000_000,
}
EXP_BASE = {"cloud_hosting": 42_000_000, "payroll": 215_000_000, "vendor_spend": 90_000_000}
TOTAL_REVENUE_FACTOR = 1.18
ENTERPRISE_FACTOR = 1.32
SMB_SUB_FACTOR = 0.91
CLOUD_FACTOR = 1.41
PAYROLL_FACTOR = 1.004
VENDOR_SPEND_FACTOR = 1.023
NOISE_JITTER = {("mid_market", "subscription"): 0.012, ("mid_market", "services"): -0.018, ("mid_market", "one_time"): 0.021, ("smb", "services"): -0.011, ("smb", "one_time"): 0.014}
NOISE_LIMIT = 0.04
ENTERPRISE_CAT_FACTOR = {"subscription": 1.30, "services": 1.38, "one_time": 1.30}
NAMED_CUSTOMERS = [("Northgate Financial", 0.42), ("Halvorsen Industries", 0.33), ("Meridian Health Systems", 0.25)]
NAMED_SHARE = 0.64
NAMED_VENDORS = [("Nimbus Cloud Platform", 0.60), ("Stratus Compute", 0.40)]
NAMED_VENDOR_SHARE = 0.78

FIRST = ["Ashford", "Brightwater", "Caldera", "Dunmore", "Ellery", "Fairholm", "Greymont", "Hartwell", "Ironside", "Juniper", "Kestrel", "Lindqvist", "Marlowe", "Norbeck", "Oakridge", "Pemberton", "Quarry", "Redfern", "Sable", "Thornbury", "Underhill", "Vantage", "Westcott", "Yardley", "Zephyr", "Alder", "Beacon", "Cobalt", "Driftwood", "Emberly", "Foxglove", "Granite", "Harbor", "Isla", "Jasper", "Kingsley", "Lumen", "Mosaic", "Nettle", "Orchard", "Pinecrest", "Quill", "Ridgeway", "Summit", "Tidewater", "Umber", "Verity", "Wren"]
ENT_SUFFIX = ["Financial", "Industries", "Health Systems", "Logistics Group", "Energy", "Holdings", "Manufacturing", "Insurance", "Retail Group", "Pharmaceuticals", "Aerospace", "Telecom"]
MM_SUFFIX = ["Media", "Foods", "Labs", "Analytics", "Robotics", "Apparel", "Freight", "Dental", "Realty", "Brewing", "Optics", "Marine"]
SMB_SUFFIX = ["Studio", "& Sons", "Bakery", "Consulting", "Bikes", "Books", "Coffee", "Design", "Fitness", "Garden", "Kitchen", "Music", "Pets", "Print", "Tutoring", "Yoga"]
CLOUD_VENDORS = ["Nimbus Cloud Platform", "Stratus Compute", "Vaultspace Storage", "Lattice Edge CDN", "Orbital Object Store", "Helix Managed Kubernetes"]
VENDOR_SPEND_VENDORS = [
    "Eastfield Packaging", "Eastern Logistics", "East Harbor Print", "Atlantic Freight", "Atlantic Office Supply", "Northeast Courier",
    "Westbay Janitorial", "Westline Courier", "West Ridge Fleet Fuel", "Pacific Pallet Co", "Pacific Signage", "Westgate Security",
    "Lumen Analytics Software", "Relay CRM", "Figment Design Software", "Ledgerline Accounting Software", "Beacon Analytics", "Quill Docs Software",
    "Travel Desk Remit", "Corporate Card Settlement", "Summit Travel Agency", "Harbor Card Services",
    "State Revenue Dept", "County Tax Collector", "Franchise Tax Board",
    "Audit Partners LLP", "Bond Insurance Premium", "Registrar Filing Fee", "Notary Services Inc", "Redfern Legal", "Kestrel Recruiting", "Oakridge Facilities", "Granite Utilities", "Verity Background Checks", "Mosaic Catering", "Tidewater Waste Services",
]


@dataclass
class Dataset:
    transactions: dict[str, list[dict]] = field(default_factory=dict)  # period -> rows
    summaries: dict[str, list[dict]] = field(default_factory=dict)
    ground_truth: dict = field(default_factory=dict)


def account_for(category: str, segment: str) -> str:
    if category in EXP_ACCOUNT:
        return EXP_ACCOUNT[category]
    return f"{ACCOUNT_CODE[category]}-{SEG_CODE[segment]}"


def _split(rng: random.Random, total: int, k: int) -> list[int]:
    if k <= 1 or total < k:
        return [total]
    w = [rng.uniform(0.6, 1.4) for _ in range(k)]
    s = sum(w)
    parts = [int(total * x / s) for x in w]
    parts[-1] += total - sum(parts)
    return parts


def _allocate(rng: random.Random, total: int, names: list[str]) -> dict[str, int]:
    w = {n: rng.uniform(0.5, 2.0) for n in names}
    s = sum(w.values())
    out = {n: int(total * w[n] / s) for n in names}
    out[names[0]] += total - sum(out.values())
    return out


def _names(rng: random.Random, n: int, suffixes: list[str], fixed: list[str] = ()) -> list[str]:
    out = list(fixed)
    seen = set(out)
    while len(out) < n:
        name = f"{rng.choice(FIRST)} {rng.choice(suffixes)}"
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _plan_revenue(rng: random.Random, customers: dict[str, list[str]]) -> tuple[dict[str, dict[tuple[str, str, str], int]], dict]:
    """Per-period, per-(segment, category, customer) totals that satisfy every planted driver exactly."""
    jul: dict[tuple[str, str, str], int] = {}
    participants: dict[tuple[str, str], list[str]] = {}
    for (seg, cat), total in REV_BASE.items():
        names = customers[seg]
        if cat == "services":
            names = [n for i, n in enumerate(names) if i % 2 == 0]
        elif cat == "one_time":
            names = [n for i, n in enumerate(names) if i % 3 == 0]
        participants[(seg, cat)] = names
        for n, amt in _allocate(rng, total, names).items():
            jul[(seg, cat, n)] = amt
    T = sum(REV_BASE.values())
    E = sum(v for (s, _), v in REV_BASE.items() if s == "enterprise")
    S = REV_BASE[("smb", "subscription")]
    T_aug = round(T * TOTAL_REVENUE_FACTOR)
    E_aug = round(E * ENTERPRISE_FACTOR)
    S_aug = round(S * SMB_SUB_FACTOR)
    O_lines = [k for k in REV_BASE if k[0] != "enterprise" and k != ("smb", "subscription")]
    O = sum(REV_BASE[k] for k in O_lines)
    O_aug = T_aug - E_aug - S_aug
    jitter_sum = sum(REV_BASE[k] * NOISE_JITTER[k] for k in O_lines)
    shift = (O_aug - O - jitter_sum) / O
    line_aug: dict[tuple[str, str], int] = {}
    for k in O_lines:
        f = 1 + NOISE_JITTER[k] + shift
        assert abs(f - 1) <= NOISE_LIMIT, (k, f)
        line_aug[k] = round(REV_BASE[k] * f)
    line_aug[max(O_lines, key=lambda k: REV_BASE[k])] += O_aug - sum(line_aug[k] for k in O_lines)
    line_aug[("smb", "subscription")] = S_aug
    ent_raw = {c: round(REV_BASE[("enterprise", c)] * ENTERPRISE_CAT_FACTOR[c]) for c in REV_CATS}
    ent_raw["services"] += E_aug - sum(ent_raw.values())
    for c in REV_CATS:
        line_aug[("enterprise", c)] = ent_raw[c]
    named_delta_total = round((T_aug - T) * NAMED_SHARE)
    named_delta = {n: round(named_delta_total * share) for n, share in NAMED_CUSTOMERS}
    named_delta[NAMED_CUSTOMERS[0][0]] += named_delta_total - sum(named_delta.values())
    aug: dict[tuple[str, str, str], int] = {}
    for (seg, cat), target in line_aug.items():
        names = participants[(seg, cat)]
        base = {n: jul[(seg, cat, n)] for n in names}
        # the three named customers carry their whole planted delta on subscription and stay flat elsewhere
        fixed = {n: base[n] + (named_delta[n] if cat == "subscription" else 0) for n in names if seg == "enterprise" and n in named_delta}
        rest = [n for n in names if n not in fixed]
        rest_target = target - sum(fixed.values())
        rest_base = sum(base[n] for n in rest)
        scaled = {n: int(base[n] * rest_target / rest_base) for n in rest}
        scaled[rest[0]] += rest_target - sum(scaled.values())
        for n in names:
            aug[(seg, cat, n)] = fixed.get(n, scaled.get(n, 0))
    truth = {"total_revenue": {"jul": T, "aug": T_aug}, "enterprise": {"jul": E, "aug": E_aug}, "smb_subscription": {"jul": S, "aug": S_aug}, "named_customers": [{"customer": n, "delta_cents": d} for n, d in named_delta.items()], "named_share_of_total_increase": NAMED_SHARE, "noise_lines": {f"{s}/{c}": {"jul": REV_BASE[(s, c)], "aug": line_aug[(s, c)]} for (s, c) in O_lines}}
    return {"2026-07": jul, "2026-08": aug}, truth


def _plan_expenses(rng: random.Random) -> tuple[dict[str, dict[tuple[str, str], int]], dict]:
    jul: dict[tuple[str, str], int] = {}
    cloud_jul = _allocate(rng, EXP_BASE["cloud_hosting"], CLOUD_VENDORS)
    for v, a in cloud_jul.items():
        jul[("cloud_hosting", v)] = a
    jul[("payroll", "ADP Payroll Services")] = EXP_BASE["payroll"]
    vs_jul = _allocate(rng, EXP_BASE["vendor_spend"], VENDOR_SPEND_VENDORS)
    for v, a in vs_jul.items():
        jul[("vendor_spend", v)] = a
    C = EXP_BASE["cloud_hosting"]
    C_aug = round(C * CLOUD_FACTOR)
    named_total = round((C_aug - C) * NAMED_VENDOR_SHARE)
    named = {v: round(named_total * share) for v, share in NAMED_VENDORS}
    named[NAMED_VENDORS[0][0]] += named_total - sum(named.values())
    others = [v for v in CLOUD_VENDORS if v not in named]
    rest_target = C_aug - sum(cloud_jul[v] + named[v] for v in named)
    rest_base = sum(cloud_jul[v] for v in others)
    aug: dict[tuple[str, str], int] = {}
    for v in named:
        aug[("cloud_hosting", v)] = cloud_jul[v] + named[v]
    scaled = {v: int(cloud_jul[v] * rest_target / rest_base) for v in others}
    scaled[others[0]] += rest_target - sum(scaled.values())
    for v in others:
        aug[("cloud_hosting", v)] = scaled[v]
    aug[("payroll", "ADP Payroll Services")] = round(EXP_BASE["payroll"] * PAYROLL_FACTOR)
    VS_aug = round(EXP_BASE["vendor_spend"] * VENDOR_SPEND_FACTOR)
    vs_scaled = {v: int(vs_jul[v] * (1 + rng.uniform(-0.10, 0.10))) for v in VENDOR_SPEND_VENDORS}
    k = VS_aug / sum(vs_scaled.values())
    vs_aug = {v: int(a * k) for v, a in vs_scaled.items()}
    vs_aug[VENDOR_SPEND_VENDORS[0]] += VS_aug - sum(vs_aug.values())
    for v, a in vs_aug.items():
        aug[("vendor_spend", v)] = a
    truth = {"cloud_hosting": {"jul": C, "aug": C_aug}, "named_vendors": [{"vendor": v, "delta_cents": d} for v, d in named.items()], "named_share_of_cloud_increase": NAMED_VENDOR_SHARE, "payroll": {"jul": EXP_BASE["payroll"], "aug": aug[("payroll", "ADP Payroll Services")]}, "vendor_spend": {"jul": EXP_BASE["vendor_spend"], "aug": VS_aug}}
    return {"2026-07": jul, "2026-08": aug}, truth


def _rows_for_period(rng: random.Random, period: str, start: date, days: int, rev: dict[tuple[str, str, str], int], exp: dict[tuple[str, str], int], rules: owners.Rules) -> list[dict]:
    rows: list[dict] = []

    def add(day: int, customer: str, segment: str, category: str, vendor: str, amount: int) -> None:
        row = {"date": date(start.year, start.month, min(day, days)).isoformat(), "customer": customer, "segment": segment, "category": category, "account": account_for(category, segment), "vendor": vendor, "amount_cents": amount}
        row["owner_agent"] = owners.assign(row, rules)
        rows.append(row)

    for (seg, cat, cust), total in rev.items():
        if total <= 0:
            continue
        k = {"subscription": rng.choice([1, 1, 1, 2]), "services": rng.randint(1, 3), "one_time": 1}[cat]
        for part in _split(rng, total, k):
            day = rng.randint(1, 5) if cat == "subscription" else rng.randint(1, days)
            add(day, cust, seg, cat, "", part)
    for (cat, vendor), total in exp.items():
        if cat == "payroll":
            for day, half in ((15, total // 2), (days, total - total // 2)):
                for dept, part in _allocate(rng, half, ["Engineering", "Sales", "Operations", "Finance", "Support", "Marketing", "Product", "People"]).items():
                    add(day, "", "", cat, f"{vendor} · {dept}", part)
            continue
        k = rng.randint(4, 8) if cat == "cloud_hosting" else rng.randint(5, 10)
        for part in _split(rng, total, k):
            add(rng.randint(1, days), "", "", cat, vendor, part)
    rows.sort(key=lambda r: (r["date"], r["category"], r["customer"], r["vendor"], r["amount_cents"]))
    for i, r in enumerate(rows, start=1):
        r["txn_id"] = f"T{period[2:4]}{period[5:7]}-{i:04d}"
    return [{c: r[c] for c in TXN_COLUMNS} for r in rows]


def summarize(period: str, rows: list[dict]) -> list[dict]:
    acc: dict[tuple[str, str, str, str], list[int]] = {}
    for r in rows:
        key = (r["account"], r["category"], r["segment"], r["owner_agent"])
        acc.setdefault(key, [0, 0])
        acc[key][0] += 1
        acc[key][1] += int(r["amount_cents"])
    return [{"period": period, "account": k[0], "category": k[1], "segment": k[2], "owner_agent": k[3], "txn_count": v[0], "amount_cents": v[1]} for k, v in sorted(acc.items())]


def generate(seed: int = MANDATE_SEED) -> Dataset:
    rng = random.Random(seed)
    rules = owners.load_rules()
    customers = {
        "enterprise": _names(rng, 12, ENT_SUFFIX, [n for n, _ in NAMED_CUSTOMERS]),
        "mid_market": _names(rng, 30, MM_SUFFIX),
        "smb": _names(rng, 120, SMB_SUFFIX),
    }
    rev, rev_truth = _plan_revenue(rng, customers)
    exp, exp_truth = _plan_expenses(rng)
    ds = Dataset()
    for period, start, days, _ in PERIODS:
        rows = _rows_for_period(rng, period, start, days, rev[period], exp[period], rules)
        ds.transactions[period] = rows
        ds.summaries[period] = summarize(period, rows)
    ds.ground_truth = {
        "seed": seed,
        "periods": [p for p, *_ in PERIODS],
        "drivers": [
            {"id": "total_revenue", "pct": round(TOTAL_REVENUE_FACTOR * 100 - 100, 2), **rev_truth["total_revenue"]},
            {"id": "enterprise_revenue", "segment": "enterprise", "owner": "controller_a", "pct": round(ENTERPRISE_FACTOR * 100 - 100, 2), **rev_truth["enterprise"], "named_customers": rev_truth["named_customers"], "named_share_of_total_increase": NAMED_SHARE},
            {"id": "smb_subscription", "segment": "smb", "category": "subscription", "owner": "controller_b", "pct": round(SMB_SUB_FACTOR * 100 - 100, 2), **rev_truth["smb_subscription"]},
            {"id": "cloud_hosting", "category": "cloud_hosting", "owner": "procurement", "pct": round(CLOUD_FACTOR * 100 - 100, 2), **exp_truth["cloud_hosting"], "named_vendors": exp_truth["named_vendors"], "named_share_of_increase": NAMED_VENDOR_SHARE},
            {"id": "payroll", "category": "payroll", "owner": "payroll", "tolerance_pct": 1.0, **exp_truth["payroll"]},
            {"id": "noise", "tolerance_pct": NOISE_LIMIT * 100, "lines": rev_truth["noise_lines"] | {"vendor_spend": exp_truth["vendor_spend"]}},
        ],
    }
    return ds


def to_csv(rows: list[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in columns})
    return buf.getvalue()


def files(ds: Dataset) -> dict[str, str]:
    out = {}
    for period, _, _, tag in PERIODS:
        out[f"transactions_{tag}.csv"] = to_csv(ds.transactions[period], TXN_COLUMNS)
        out[f"summary_{tag}.csv"] = to_csv(ds.summaries[period], SUMMARY_COLUMNS)
    out["ground_truth.json"] = json.dumps(ds.ground_truth, indent=2, sort_keys=True) + "\n"
    return out


def write(out_dir: Path = OUT_DIR, seed: int = MANDATE_SEED) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, text in files(generate(seed)).items():
        p = out_dir / name
        p.write_text(text, encoding="utf-8")
        paths.append(p)
    return paths


if __name__ == "__main__":
    for p in write():
        print(p)
