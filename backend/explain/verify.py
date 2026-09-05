"""The gate. No number is spoken or printed unless it exists in the evidence JSON."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

BRACKET = re.compile(r"\[([^\]]*)\]")
CITE_ID = re.compile(r"\bE\d+\b")
DATE = re.compile(r"\b(?:19|20)\d{2}-\d{2}(?:-\d{2})?\b")
FIGURE = re.compile(r"(?<![\w\[])(\$?)(\d[\d,]*(?:\.\d+)?)\s*(%|percent|[kKmM]\b|million|thousand|billion)?(?![\w\]])", re.I)
SUFFIX = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "billion": 1e9}


def citations(text: str) -> list[str]:
    """Every E-id inside square brackets, in order: [E7], [E7, E9] and [E7][E9] all count."""
    out: list[str] = []
    for group in BRACKET.findall(text or ""):
        out.extend(CITE_ID.findall(group))
    return out


def _strip_citations(text: str) -> str:
    return BRACKET.sub(lambda m: " " if CITE_ID.search(m.group(1)) else m.group(0), text or "")
REFUSAL = re.compile(r"records? (?:doesn't|don't|do not|does not) show", re.I)
PCT_TOL = 0.5
COVERAGE_MIN = 50.0


@dataclass(frozen=True)
class Figure:
    raw: str
    kind: str  # dollar | percent | number
    value: float


def extract_figures(text: str) -> list[Figure]:
    cleaned = DATE.sub(" ", _strip_citations(text))
    out: list[Figure] = []
    for m in FIGURE.finditer(cleaned):
        dollar, num, suffix = m.group(1), m.group(2).rstrip(",."), (m.group(3) or "").lower()
        try:
            value = float(num.replace(",", ""))
        except ValueError:
            continue
        value *= SUFFIX.get(suffix, 1)
        if suffix in ("%", "percent"):
            kind = "percent"
        elif dollar:
            kind = "dollar"
        else:
            if "." not in num and "," not in num and 1900 <= value <= 2100:
                continue  # a year, not a figure
            kind = "number"
        out.append(Figure((m.group(1) + num + (" " + m.group(3) if m.group(3) else "")).strip(), kind, value))
    return out


def _walk(obj: Any, key: str = "") -> list[tuple[str, float]]:
    found: list[tuple[str, float]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "txn_ids":
                continue
            if k in ("statement",) and isinstance(v, str):
                for f in extract_figures(v):
                    found.append((f.kind, f.value))
                continue
            found.extend(_walk(v, k))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_walk(v, key))
    elif isinstance(obj, bool):
        return found
    elif isinstance(obj, (int, float)):
        k = key.lower()
        if k.endswith("_cents") or k == "cents":
            found.append(("dollar", obj / 100))
        elif k.endswith("_pct") or k in ("pct", "share", "delta_pct", "contribution_pct_of_delta"):
            found.append(("percent", float(obj)))
        else:
            found.append(("number", float(obj)))
    return found


def allowed_figures(evidence: dict) -> dict[str, set[float]]:
    allowed: dict[str, set[float]] = {"dollar": set(), "percent": set(), "number": set()}
    for kind, value in _walk(evidence):
        allowed[kind].add(round(value, 4))
    return allowed


def _sig_digits(f: Figure) -> int:
    digits = re.sub(r"[^\d]", "", f.raw.split("%")[0].replace("percent", "")).strip("0")
    return len(digits) if digits else 1


def _matches(f: Figure, allowed: dict[str, set[float]]) -> bool:
    if f.kind == "percent":
        return any(abs(f.value - a) <= PCT_TOL for a in allowed["percent"])
    pool = allowed["dollar"] if f.kind == "dollar" else allowed["dollar"] | allowed["number"] | allowed["percent"]
    whole = "." not in f.raw.split(" ")[0]
    tol = 0.5 if whole else 0.005  # a figure quoted without cents may be rounded to the dollar
    if any(abs(f.value - a) <= tol for a in pool):
        return True
    if _sig_digits(f) <= 3:
        return any(a and abs(f.value - a) / abs(a) <= 0.01 for a in pool)
    return False


def _rows(evidence: dict) -> dict[str, dict]:
    return {r["id"]: r for r in evidence.get("evidence", [])}


def _target_delta(evidence: dict, target: str) -> int:
    for v in evidence.get("variances", []):
        if v.get("key") == target:
            return int(v.get("delta_cents", 0))
    for r in evidence.get("evidence", []):
        if r.get("target") == target and r.get("kind") == "variance":
            return int(r.get("figures", {}).get("delta_cents", 0))
    return 0


def _target_pct(evidence: dict, target: str) -> float | None:
    for v in evidence.get("variances", []):
        if v.get("key") == target:
            return v.get("delta_pct")
    return None


def driver_coverage(cited: list[dict], evidence: dict) -> float:
    by_target_dim: dict[tuple[str, str], int] = {}
    for r in cited:
        if r.get("kind") == "variance":
            p = _target_pct(evidence, r.get("target", ""))
            if p is not None and abs(p) < 1.0:
                return 100.0  # a flat account is fully explained by its own variance row
            continue
        if r.get("kind") not in ("driver", "concentration"):
            continue
        fig = r.get("figures", {})
        cents = int(fig.get("contribution_cents", fig.get("delta_cents", 0)) or 0)
        key = (r.get("target", ""), fig.get("dimension", ""))
        by_target_dim[key] = by_target_dim.get(key, 0) + cents
    best = 0.0
    for (target, _), cents in by_target_dim.items():
        delta = _target_delta(evidence, target)
        if delta:
            best = max(best, min(100.0, abs(cents) * 100 / abs(delta)))
    return round(best, 2)


def check(text: str, evidence: dict) -> dict:
    allowed = allowed_figures(evidence)
    figures = extract_figures(text)
    unverifiable = [f.raw for f in figures if not _matches(f, allowed)]
    rows = _rows(evidence)
    cited_ids = citations(text)
    unresolved = sorted({c for c in cited_ids if c not in rows})
    cited = [rows[c] for c in cited_ids if c in rows]
    coverage = driver_coverage(cited, evidence)
    refusal = bool(REFUSAL.search(text or ""))
    ok = not unverifiable and not unresolved and (refusal or coverage >= COVERAGE_MIN)
    return {
        "ok": ok,
        "unverifiable_figures": unverifiable,
        "figures_checked": len(figures),
        "citations": sorted(set(cited_ids), key=lambda c: int(c[1:])),
        "unresolved_citations": unresolved,
        "driver_coverage_pct": coverage,
        "refusal": refusal,
    }
