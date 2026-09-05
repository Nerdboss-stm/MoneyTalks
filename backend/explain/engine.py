"""Deterministic variance engine. Zero LLM. Everything an agent may say about a period comes from here."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from explain.datagen import summarize

REVENUE_CATEGORIES = ("subscription", "services", "one_time")
NON_DIMENSIONS = {"txn_id", "date", "amount_cents"}
DIM_NOUN = {"customer": "customers", "vendor": "vendors", "segment": "segments", "category": "categories", "owner_agent": "owners", "account": "accounts"}
TOTALS = ("TOTAL:revenue", "TOTAL:expense", "TOTAL:net")
CONCENTRATION_THRESHOLD = 0.5


def usd(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def signed_usd(cents: int) -> str:
    return ("+" if cents >= 0 else "-") + f"${abs(cents) / 100:,.2f}"


def pct(delta: int, base: int) -> float | None:
    return None if base == 0 else round(delta * 100 / base, 2)


def is_revenue(row: dict) -> bool:
    if row.get("category") in REVENUE_CATEGORIES:
        return True
    acct = str(row.get("account", ""))
    return acct[:1] == "4" and row.get("category") not in ("cloud_hosting", "payroll", "vendor_spend")


@dataclass
class Variance:
    key: str
    category: str
    segment: str
    owner_agent: str
    prior: int
    current: int
    delta_cents: int
    delta_pct: float | None
    rank: int | None
    direction: str


@dataclass
class Contribution:
    dimension: str
    value: str
    prior: int
    current: int
    contribution_cents: int
    contribution_pct_of_delta: float | None
    pct: float | None
    direction: str
    txn_ids: list[str] = field(default_factory=list)


@dataclass
class Concentration:
    target: str
    dimension: str
    k: int
    values: list[str]
    share: float
    direction: str
    delta_cents: int
    txn_ids: list[str] = field(default_factory=list)

    def statement(self) -> str:
        noun = DIM_NOUN.get(self.dimension, f"{self.dimension} values")
        noun = noun if self.k != 1 else noun.rstrip("s") if self.dimension != "category" else "category"
        return f"{self.k} {noun} account for {self.share:.0f}% of the {self.direction}: {', '.join(self.values)}."


@dataclass
class Evidence:
    id: str
    statement: str
    figures: dict[str, Any]
    txn_ids: list[str]
    target: str
    kind: str
    owners: list[str]


def _direction(delta: int) -> str:
    return "up" if delta > 0 else "down" if delta < 0 else "flat"


def _uniq(rows: list[dict], key: str) -> str:
    vals = {str(r.get(key, "")) for r in rows}
    return next(iter(vals)) if len(vals) == 1 else "mixed"


def _is_summary(rows: list[dict]) -> bool:
    return bool(rows) and "txn_count" in rows[0]


class Engine:
    def __init__(self, prior_txns: list[dict], current_txns: list[dict], prior_label: str, current_label: str, prior_summary: list[dict] | None = None, current_summary: list[dict] | None = None) -> None:
        self.prior_txns = prior_txns
        self.current_txns = current_txns
        self.prior_label = prior_label
        self.current_label = current_label
        self.prior_summary = prior_summary or summarize(prior_label, prior_txns)
        self.current_summary = current_summary or summarize(current_label, current_txns)
        self.dimensions = self._dimensions()

    @classmethod
    def from_periods(cls, transactions: dict[str, list[dict]], summaries: dict[str, list[dict]] | None = None) -> "Engine":
        periods = sorted(transactions)
        if len(periods) < 2:
            raise ValueError("need two periods")
        p, c = periods[-2], periods[-1]
        s = summaries or {}
        return cls(transactions[p], transactions[c], p, c, s.get(p), s.get(c))

    def _dimensions(self) -> list[str]:
        keys: list[str] = []
        for r in self.prior_txns + self.current_txns:
            for k in r:
                if k not in NON_DIMENSIONS and k not in keys:
                    keys.append(k)
        return [k for k in keys if any(str(r.get(k, "")) for r in self.prior_txns + self.current_txns)]

    # ---- compare

    def compare(self, prior: list[dict] | None = None, current: list[dict] | None = None) -> list[Variance]:
        ps = prior if prior is not None else self.prior_summary
        cs = current if current is not None else self.current_summary
        if not _is_summary(ps):
            ps = summarize(self.prior_label, ps)
        if not _is_summary(cs):
            cs = summarize(self.current_label, cs)
        accounts = sorted({r["account"] for r in ps} | {r["account"] for r in cs})
        rows: list[Variance] = []
        for a in accounts:
            pa = [r for r in ps if r["account"] == a]
            ca = [r for r in cs if r["account"] == a]
            p = sum(r["amount_cents"] for r in pa)
            c = sum(r["amount_cents"] for r in ca)
            rows.append(Variance(a, _uniq(pa + ca, "category"), _uniq(pa + ca, "segment"), _uniq(pa + ca, "owner_agent"), p, c, c - p, pct(c - p, p), None, _direction(c - p)))
        rows.sort(key=lambda v: (-abs(v.delta_cents), -abs(v.delta_pct or 0), v.key))
        for i, v in enumerate(rows, start=1):
            v.rank = i
        rev_p = sum(r["amount_cents"] for r in ps if is_revenue(r))
        rev_c = sum(r["amount_cents"] for r in cs if is_revenue(r))
        exp_p = sum(r["amount_cents"] for r in ps if not is_revenue(r))
        exp_c = sum(r["amount_cents"] for r in cs if not is_revenue(r))
        totals = [
            Variance("TOTAL:revenue", "revenue", "all", "all", rev_p, rev_c, rev_c - rev_p, pct(rev_c - rev_p, rev_p), None, _direction(rev_c - rev_p)),
            Variance("TOTAL:expense", "expense", "all", "all", exp_p, exp_c, exp_c - exp_p, pct(exp_c - exp_p, exp_p), None, _direction(exp_c - exp_p)),
            Variance("TOTAL:net", "net", "all", "all", rev_p - exp_p, rev_c - exp_c, (rev_c - exp_c) - (rev_p - exp_p), pct((rev_c - exp_c) - (rev_p - exp_p), rev_p - exp_p), None, _direction((rev_c - exp_c) - (rev_p - exp_p))),
        ]
        return totals + rows

    # ---- selection

    def _select(self, rows: list[dict], target: str | dict) -> list[dict]:
        if isinstance(target, dict):
            return [r for r in rows if all(str(r.get(k, "")) == str(v) for k, v in target.items())]
        if target == "TOTAL:revenue":
            return [r for r in rows if is_revenue(r)]
        if target == "TOTAL:expense":
            return [r for r in rows if not is_revenue(r)]
        if target == "TOTAL:net":
            return list(rows)
        return [r for r in rows if r.get("account") == target]

    def _signed(self, r: dict, target: str | dict) -> int:
        if target == "TOTAL:net" and not is_revenue(r):
            return -r["amount_cents"]
        return r["amount_cents"]

    # ---- drill

    def drill(self, target: str | dict = "TOTAL:revenue", prior_txns: list[dict] | None = None, current_txns: list[dict] | None = None) -> dict[str, list[Contribution]]:
        prior = self._select(prior_txns if prior_txns is not None else self.prior_txns, target)
        current = self._select(current_txns if current_txns is not None else self.current_txns, target)
        delta = sum(self._signed(r, target) for r in current) - sum(self._signed(r, target) for r in prior)
        out: dict[str, list[Contribution]] = {}
        dims = [d for d in self.dimensions if d != "account" or isinstance(target, dict) or target.startswith("TOTAL:")]
        for dim in dims:
            values = sorted({str(r.get(dim, "")) for r in prior + current})
            if values == [""]:
                continue
            contribs: list[Contribution] = []
            for v in values:
                pr = [r for r in prior if str(r.get(dim, "")) == v]
                cr = [r for r in current if str(r.get(dim, "")) == v]
                p = sum(self._signed(r, target) for r in pr)
                c = sum(self._signed(r, target) for r in cr)
                d = c - p
                contribs.append(Contribution(dim, v or "(none)", p, c, d, None if delta == 0 else round(d * 100 / delta, 2), pct(d, p), _direction(d), [r["txn_id"] for r in pr + cr]))
            contribs.sort(key=lambda x: (-abs(x.contribution_cents), x.value))
            out[dim] = contribs
        return out

    # ---- drivers, offsets, concentration

    def drivers(self, target: str | dict = "TOTAL:revenue", n: int = 5) -> dict[str, Any]:
        drilled = self.drill(target)
        prior = self._select(self.prior_txns, target)
        current = self._select(self.current_txns, target)
        delta = sum(self._signed(r, target) for r in current) - sum(self._signed(r, target) for r in prior)
        direction = "increase" if delta > 0 else "decrease" if delta < 0 else "no change"
        sign = 1 if delta >= 0 else -1
        order = {d: i for i, d in enumerate(self.dimensions)}
        same = [c for cs in drilled.values() for c in cs if c.contribution_cents * sign > 0]
        opposing = [c for cs in drilled.values() for c in cs if c.contribution_cents * sign < 0]
        same.sort(key=lambda c: (-abs(c.contribution_cents), order.get(c.dimension, 99), c.value))
        opposing.sort(key=lambda c: (-abs(c.contribution_cents), order.get(c.dimension, 99), c.value))
        concentration: list[Concentration] = []
        for dim, cs in drilled.items():
            aligned = [c for c in cs if c.contribution_cents * sign > 0]
            if not aligned or delta == 0 or len(cs) < 2:
                continue
            cum = 0
            for k, c in enumerate(aligned, start=1):
                cum += c.contribution_cents
                share = cum * 100 / delta
                if share >= CONCENTRATION_THRESHOLD * 100 or k == len(aligned):
                    concentration.append(Concentration(str(target), dim, k, [x.value for x in aligned[:k]], round(share, 2), direction, cum, [t for x in aligned[:k] for t in x.txn_ids]))
                    break
        return {"target": str(target), "delta_cents": delta, "direction": direction, "drivers": same[:n], "offsets": opposing[:n], "concentration": concentration}

    # ---- evidence

    def _targets(self) -> list[str]:
        return list(TOTALS[:2]) + [v.key for v in self.compare() if not v.key.startswith("TOTAL:")]

    def _owner_of(self, target: str, txn_ids: list[str]) -> list[str]:
        ids = set(txn_ids)
        owners = {str(r.get("owner_agent", "")) for r in self.prior_txns + self.current_txns if r["txn_id"] in ids}
        return sorted(o for o in owners if o)

    def evidence(self, n: int = 5) -> list[Evidence]:
        rows: list[Evidence] = []
        variances = {v.key: v for v in self.compare()}
        label = {"TOTAL:revenue": "Total revenue", "TOTAL:expense": "Total expense", "TOTAL:net": "Net"}

        def add(kind: str, target: str, statement: str, figures: dict, txn_ids: list[str]) -> None:
            rows.append(Evidence(f"E{len(rows) + 1}", statement, figures, sorted(set(txn_ids)), target, kind, self._owner_of(target, txn_ids)))

        for target in self._targets():
            v = variances.get(target)
            if v is None or (v.prior == 0 and v.current == 0):
                continue
            name = label.get(target, f"Account {target}")
            ids = [r["txn_id"] for r in self._select(self.prior_txns + self.current_txns, target)]
            move = "rose" if v.delta_cents > 0 else "fell" if v.delta_cents < 0 else "was flat"
            pct_txt = f" {abs(v.delta_pct):.1f}%" if v.delta_pct is not None and v.delta_cents != 0 else ""
            add("variance", target, f"{name} {move}{pct_txt} from {usd(v.prior)} in {self.prior_label} to {usd(v.current)} in {self.current_label} ({signed_usd(v.delta_cents)}).", {"prior": v.prior, "current": v.current, "delta_cents": v.delta_cents, "delta_pct": v.delta_pct, "rank": v.rank, "owner_agent": v.owner_agent}, ids)
            if v.delta_cents == 0:
                continue
            d = self.drivers(target, n)
            for c in d["drivers"]:
                add("driver", target, f"{c.dimension} {c.value} contributed {signed_usd(c.contribution_cents)} ({c.contribution_pct_of_delta:.1f}% of the {d['direction']}), moving from {usd(c.prior)} to {usd(c.current)}.", {"dimension": c.dimension, "value": c.value, "prior": c.prior, "current": c.current, "contribution_cents": c.contribution_cents, "contribution_pct_of_delta": c.contribution_pct_of_delta, "pct": c.pct}, c.txn_ids)
            for c in d["offsets"]:
                add("offset", target, f"{c.dimension} {c.value} moved {signed_usd(c.contribution_cents)} against the {d['direction']} ({usd(c.prior)} to {usd(c.current)}).", {"dimension": c.dimension, "value": c.value, "prior": c.prior, "current": c.current, "contribution_cents": c.contribution_cents, "contribution_pct_of_delta": c.contribution_pct_of_delta, "pct": c.pct}, c.txn_ids)
            for k in d["concentration"]:
                add("concentration", target, k.statement(), {"dimension": k.dimension, "k": k.k, "values": k.values, "share": k.share, "delta_cents": k.delta_cents}, k.txn_ids)
        return rows

    def to_evidence_json(self, n: int = 5) -> dict:
        variances = self.compare()
        drivers = {t: self.drivers(t, n) for t in self._targets()}
        return {
            "periods": {"prior": self.prior_label, "current": self.current_label},
            "variances": [asdict(v) for v in variances],
            "drivers": {t: [asdict(c) for c in d["drivers"]] for t, d in drivers.items()},
            "offsets": {t: [asdict(c) for c in d["offsets"]] for t, d in drivers.items()},
            "concentration": {t: [asdict(k) | {"statement": k.statement()} for k in d["concentration"]] for t, d in drivers.items()},
            "evidence": [asdict(e) for e in self.evidence(n)],
        }

    def slice_for_owner(self, agent_id: str, n: int = 5) -> dict:
        variances = [v for v in self.compare() if v.owner_agent == agent_id]
        accounts = {v.key for v in variances}
        prior = sum(v.prior for v in variances)
        current = sum(v.current for v in variances)
        ev = [e for e in self.evidence(n) if e.target in accounts and e.owners == [agent_id]]
        drivers = {a: self.drivers(a, n) for a in sorted(accounts)}
        return {
            "agent_id": agent_id,
            "periods": {"prior": self.prior_label, "current": self.current_label},
            "accounts": sorted(accounts),
            "totals": {"prior": prior, "current": current, "delta_cents": current - prior, "delta_pct": pct(current - prior, prior)},
            "variances": [asdict(v) for v in variances],
            "drivers": {a: [asdict(c) for c in d["drivers"]] for a, d in drivers.items()},
            "offsets": {a: [asdict(c) for c in d["offsets"]] for a, d in drivers.items()},
            "concentration": {a: [asdict(k) | {"statement": k.statement()} for k in d["concentration"]] for a, d in drivers.items()},
            "evidence": [asdict(e) for e in ev],
        }


def evidence_json_text(engine: Engine) -> str:
    return json.dumps(engine.to_evidence_json(), indent=2, sort_keys=True) + "\n"
