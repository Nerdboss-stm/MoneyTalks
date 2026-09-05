"""Foreign CSV -> the normalized frames datagen produces (transactions + summaries, cents, ISO dates, owners)."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from explain import owners
from explain.datagen import SUMMARY_COLUMNS, TXN_COLUMNS, summarize, to_csv

DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%d-%b-%Y", "%b %d, %Y", "%Y-%m-%dT%H:%M:%S")
SYNONYMS: dict[str, tuple[str, ...]] = {
    "txn_id": ("txn_id", "transaction_id", "id", "ref", "reference", "txn", "entry_id"),
    "date": ("date", "posted_on", "posted", "txn_date", "transaction_date", "entry_date", "day"),
    "customer": ("customer", "client", "account_name", "customer_name", "counterparty"),
    "segment": ("segment", "tier", "customer_segment"),
    "category": ("category", "type", "gl_category", "line", "kind"),
    "account": ("account", "gl", "gl_account", "ledger", "ledger_account", "account_code"),
    "vendor": ("vendor", "supplier", "payee", "merchant"),
    "amount_cents": ("amount_cents", "amount", "amt", "amt_usd", "value", "total", "net", "usd", "cents"),
    "owner_agent": ("owner_agent", "owner", "agent"),
    "txn_count": ("txn_count", "count", "n", "transactions"),
    "period": ("period", "month"),
}
NUMERIC = re.compile(r"^[-+]?\$?\s?[\d,]*\.?\d+$")


@dataclass
class Normalized:
    transactions: dict[str, list[dict]] = field(default_factory=dict)
    summaries: dict[str, list[dict]] = field(default_factory=dict)
    sources: list[dict] = field(default_factory=list)


def _canon(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")


def _parse_date(v: str) -> date | None:
    v = (v or "").strip()
    if not v:
        return None
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(v[:19] if "T" in v else v, f).date()
        except ValueError:
            continue
    return None


def _num(v: str) -> float | None:
    s = (v or "").strip().replace("$", "").replace(",", "").replace(" ", "")
    if not s or not NUMERIC.match(s):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        headers = [h for h in (r.fieldnames or [])]
        rows = [dict(row) for row in r]
    return headers, rows


def _col_by_synonym(headers: list[str], key: str) -> str | None:
    canon = {_canon(h): h for h in headers}
    for s in SYNONYMS[key]:
        if s in canon:
            return canon[s]
    return None


def _date_column(headers: list[str], rows: list[dict]) -> str | None:
    hinted = _col_by_synonym(headers, "date")
    candidates = ([hinted] if hinted else []) + [h for h in headers if h != hinted]
    for h in candidates:
        vals = [r.get(h, "") for r in rows]
        ok = sum(_parse_date(v) is not None for v in vals)
        if vals and ok >= 0.9 * len(vals):
            return h
    return None


def _numeric_columns(headers: list[str], rows: list[dict], exclude: set[str]) -> dict[str, list[float]]:
    out = {}
    for h in headers:
        if h in exclude:
            continue
        vals = [_num(r.get(h, "")) for r in rows]
        present = [v for v in vals if v is not None]
        if rows and len(present) >= 0.95 * len(rows):
            out[h] = present
    return out


def _amount_column(headers: list[str], rows: list[dict], exclude: set[str]) -> tuple[str | None, bool]:
    """Largest-magnitude numeric column. Returns (name, is_dollars)."""
    nums = _numeric_columns(headers, rows, exclude)
    hinted = _col_by_synonym(headers, "amount_cents")
    if hinted in nums:
        name = hinted
    elif nums:
        name = max(nums, key=lambda h: sum(abs(v) for v in nums[h]))
    else:
        return None, False
    vals = nums[name]
    fractional = any(abs(v - round(v)) > 1e-9 for v in vals)
    is_dollars = fractional or ("cents" not in _canon(name) and ("usd" in _canon(name) or "dollar" in _canon(name)))
    return name, is_dollars


def _id_column(headers: list[str], rows: list[dict], exclude: set[str]) -> str | None:
    hinted = _col_by_synonym(headers, "txn_id")
    if hinted and hinted not in exclude:
        return hinted
    for h in headers:
        if h in exclude:
            continue
        vals = [r.get(h, "") for r in rows]
        if not rows or len(set(vals)) != len(vals):
            continue
        if all(_num(v) is not None for v in vals):
            continue  # a purely numeric unique column is an amount, not an id
        if all(any(ch.isdigit() for ch in v) for v in vals):
            return h
    return None


def _text_columns(headers: list[str], rows: list[dict], exclude: set[str]) -> dict[str, int]:
    out = {}
    for h in headers:
        if h in exclude:
            continue
        vals = [r.get(h, "") for r in rows]
        if all(_num(v) is None or v.strip() == "" for v in vals):
            out[h] = len(set(vals))
    return out


def classify(headers: list[str], rows: list[dict]) -> str:
    if _col_by_synonym(headers, "txn_count"):
        return "summary"
    if _date_column(headers, rows) is None:
        return "summary"
    return "transactions"


def _cents(v: str, is_dollars: bool) -> int:
    n = _num(v)
    if n is None:
        return 0
    return int(round(n * 100)) if is_dollars else int(round(n))


def _period_from(path: Path, rows: list[dict], date_col: str | None) -> str:
    if date_col:
        ds = [_parse_date(r.get(date_col, "")) for r in rows]
        ds = [d for d in ds if d]
        if ds:
            return max(set(f"{d.year:04d}-{d.month:02d}" for d in ds), key=lambda p: sum(1 for d in ds if f"{d.year:04d}-{d.month:02d}" == p))
    m = re.search(r"(20\d{2})[-_]?(0[1-9]|1[0-2])", path.stem)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    for tag, mm in (("jan", "01"), ("feb", "02"), ("mar", "03"), ("apr", "04"), ("may", "05"), ("jun", "06"), ("jul", "07"), ("aug", "08"), ("sep", "09"), ("oct", "10"), ("nov", "11"), ("dec", "12")):
        if tag in path.stem.lower():
            return f"2026-{mm}"
    return "unknown"


def normalize_transactions(path: Path, headers: list[str], rows: list[dict], rules: owners.Rules) -> tuple[dict[str, list[dict]], dict]:
    date_col = _date_column(headers, rows)
    id_col = _id_column(headers, rows, {date_col} if date_col else set())
    amount_col, is_dollars = _amount_column(headers, rows, {c for c in (date_col, id_col) if c})
    used = {c for c in (date_col, id_col, amount_col) if c}
    mapped: dict[str, str] = {}
    for key in ("customer", "segment", "category", "account", "vendor", "owner_agent"):
        col = _col_by_synonym(headers, key)
        if col and col not in used:
            mapped[key] = col
            used.add(col)
    text = _text_columns(headers, rows, used)
    dims = [h for h, card in text.items() if card <= max(12, len(rows) * 0.05)]
    by_period: dict[str, list[dict]] = {}
    for i, r in enumerate(rows, start=1):
        d = _parse_date(r.get(date_col, "")) if date_col else None
        period = f"{d.year:04d}-{d.month:02d}" if d else _period_from(path, rows, None)
        row = {
            "txn_id": (r.get(id_col, "") if id_col else "") or f"X{period[2:4]}{period[5:7]}-{i:04d}",
            "date": d.isoformat() if d else "",
            "customer": r.get(mapped.get("customer", ""), "").strip() if "customer" in mapped else "",
            "segment": r.get(mapped.get("segment", ""), "").strip() if "segment" in mapped else "",
            "category": r.get(mapped.get("category", ""), "").strip() if "category" in mapped else "",
            "account": r.get(mapped.get("account", ""), "").strip() if "account" in mapped else "",
            "vendor": r.get(mapped.get("vendor", ""), "").strip() if "vendor" in mapped else "",
            "amount_cents": _cents(r.get(amount_col, ""), is_dollars) if amount_col else 0,
        }
        for h in dims:
            row[_canon(h)] = r.get(h, "").strip()
        owner = r.get(mapped["owner_agent"], "").strip() if "owner_agent" in mapped else ""
        row["owner_agent"] = owner if owner in owners.AGENT_IDS else owners.assign(row, rules)
        by_period.setdefault(period, []).append(row)
    for period, prs in by_period.items():
        prs.sort(key=lambda x: (x["date"], x["txn_id"]))
    meta = {"file": str(path), "kind": "transactions", "date": date_col, "id": id_col, "amount": amount_col, "amount_unit": "dollars" if is_dollars else "cents", "mapped": mapped, "dimensions": dims, "rows": len(rows)}
    return by_period, meta


def normalize_summary(path: Path, headers: list[str], rows: list[dict], rules: owners.Rules) -> tuple[dict[str, list[dict]], dict]:
    amount_col, is_dollars = _amount_column(headers, rows, {c for c in (_col_by_synonym(headers, "txn_count"),) if c})
    count_col = _col_by_synonym(headers, "txn_count")
    period_col = _col_by_synonym(headers, "period")
    mapped = {k: _col_by_synonym(headers, k) for k in ("account", "category", "segment", "owner_agent")}
    by_period: dict[str, list[dict]] = {}
    for r in rows:
        period = r.get(period_col, "").strip() if period_col else _period_from(path, rows, None)
        row = {"period": period, "account": r.get(mapped["account"] or "", "").strip(), "category": r.get(mapped["category"] or "", "").strip(), "segment": r.get(mapped["segment"] or "", "").strip(), "txn_count": int(_num(r.get(count_col, "")) or 0) if count_col else 0, "amount_cents": _cents(r.get(amount_col, ""), is_dollars) if amount_col else 0}
        owner = r.get(mapped["owner_agent"] or "", "").strip()
        row["owner_agent"] = owner if owner in owners.AGENT_IDS else owners.assign(row, rules)
        by_period.setdefault(period, []).append({c: row[c] for c in SUMMARY_COLUMNS})
    for prs in by_period.values():
        prs.sort(key=lambda x: (x["account"], x["category"], x["segment"], x["owner_agent"]))
    meta = {"file": str(path), "kind": "summary", "amount": amount_col, "amount_unit": "dollars" if is_dollars else "cents", "rows": len(rows)}
    return by_period, meta


def load(paths: list[str | Path], rules: owners.Rules | None = None) -> Normalized:
    rules = rules or owners.load_rules()
    out = Normalized()
    for p in paths:
        path = Path(p)
        headers, rows = read_csv(path)
        kind = classify(headers, rows)
        if kind == "summary":
            frames, meta = normalize_summary(path, headers, rows, rules)
            for period, prs in frames.items():
                out.summaries.setdefault(period, []).extend(prs)
        else:
            frames, meta = normalize_transactions(path, headers, rows, rules)
            for period, prs in frames.items():
                out.transactions.setdefault(period, []).extend(prs)
        out.sources.append(meta)
    for period, prs in out.transactions.items():
        prs.sort(key=lambda x: (x["date"], x["txn_id"]))
        if period not in out.summaries:
            out.summaries[period] = summarize(period, prs)
    for prs in out.summaries.values():
        prs.sort(key=lambda x: (x["account"], x["category"], x["segment"], x["owner_agent"]))
    return out


def revenue_by_period(norm: Normalized, revenue_categories: tuple[str, ...] = ("subscription", "services", "one_time")) -> dict[str, int]:
    return {p: sum(r["amount_cents"] for r in rows if r.get("category") in revenue_categories) for p, rows in norm.transactions.items()}


def write(norm: Normalized, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for period, rows in sorted(norm.transactions.items()):
        extra = sorted({k for r in rows for k in r} - set(TXN_COLUMNS))
        p = out_dir / f"transactions_{period}.csv"
        p.write_text(to_csv(rows, TXN_COLUMNS + extra), encoding="utf-8")
        paths.append(p)
    for period, rows in sorted(norm.summaries.items()):
        p = out_dir / f"summary_{period}.csv"
        p.write_text(to_csv(rows, SUMMARY_COLUMNS), encoding="utf-8")
        paths.append(p)
    return paths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m explain.ingest")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    norm = load(a.files)
    for m in norm.sources:
        print(f"{m['file']}: {m['kind']} rows={m['rows']} amount={m.get('amount')} ({m.get('amount_unit')}) date={m.get('date')} id={m.get('id')} dims={m.get('dimensions', [])}", file=sys.stderr)
    for p in write(norm, Path(a.out)):
        print(p)
    for period, cents in sorted(revenue_by_period(norm).items()):
        print(f"{period} revenue ${cents / 100:,.2f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
