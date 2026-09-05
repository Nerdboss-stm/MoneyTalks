"""Meeting proof read from recordings: what the gate let through, per run.

    uv run python -m explain.prove explain-v1-04 explain-v2-04

Every number comes from the events the meeting published (agent.verified, agent.answer, agent.traced,
prism.verdict) and from docs/context-memory.md. Nothing is re-scored here.
"""

from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECORDINGS = ROOT / "recordings"
MEMORY_FILE = ROOT / "docs" / "context-memory.md"


def load(session_id: str) -> list[dict]:
    path = RECORDINGS / f"{session_id}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _ts(e: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(e.get("ts_wall")))
    except (TypeError, ValueError):
        return None


def turns(events: list[dict]) -> list[dict]:
    """One dict per addressed owner: the verifier result, the answer, the trace and the verdict that followed."""
    out: list[dict] = []
    cur: dict | None = None
    for e in events:
        t, pl = e.get("type"), e.get("payload") or {}
        if t == "agent.addressed":
            cur = {"agent_id": pl.get("agent_id"), "question": pl.get("question"), "addressed_at": _ts(e)}
            out.append(cur)
        elif cur is None:
            continue
        elif t == "agent.verified":
            cur["ok"] = bool(pl.get("ok"))
            cur["unverifiable"] = list(pl.get("unverifiable") or [])
            cur["coverage"] = pl.get("coverage")
        elif t == "agent.answer":
            cur["answered_at"] = _ts(e)
            cur["replaced"] = bool(pl.get("replaced"))
            v = pl.get("verify") or {}
            cur.setdefault("ok", bool(v.get("ok")))
            cur.setdefault("unverifiable", list(v.get("unverifiable_figures") or []))
            cur.setdefault("coverage", v.get("driver_coverage_pct"))
        elif t == "agent.traced":
            cur["recorded"] = bool(pl.get("recorded"))
            cur["trace_id"] = pl.get("trace_id")
        elif t == "prism.verdict":
            cur["verdict"] = pl.get("status")
            cur["score"] = pl.get("score")
            cur["flagged"] = pl.get("flagged")
    return out


def memory_lines(session_id: str, path: Path = MEMORY_FILE) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("- ") and session_id in line)


def stats(session_id: str) -> dict:
    ts = turns(load(session_id))
    latencies = [int((t["answered_at"] - t["addressed_at"]).total_seconds() * 1000) for t in ts if t.get("answered_at") and t.get("addressed_at")]
    coverages = [float(t["coverage"]) for t in ts if t.get("coverage") is not None]
    passed = sum(1 for t in ts if t.get("ok"))
    return {
        "session_id": session_id,
        "turns": len(ts),
        "unverifiable_figures": sum(len(t.get("unverifiable", [])) for t in ts),
        "turns_with_unverifiable": sum(1 for t in ts if t.get("unverifiable")),
        "driver_coverage_pct_median": round(statistics.median(coverages), 1) if coverages else None,
        "pass_rate_pct": round(100 * passed / len(ts), 1) if ts else None,
        "replaced": sum(1 for t in ts if t.get("replaced")),
        "memory_lines": memory_lines(session_id),
        "median_latency_ms": int(statistics.median(latencies)) if latencies else None,
        "prism_recorded": sum(1 for t in ts if t.get("recorded")),
        "prism_scored": sum(1 for t in ts if t.get("verdict") == "scored"),
        "prism_flagged": sum(1 for t in ts if t.get("flagged")),
    }


ROWS = [
    ("Turns", "turns"),
    ("Unverifiable figures", "unverifiable_figures"),
    ("Turns with an unverifiable figure", "turns_with_unverifiable"),
    ("Driver coverage, median %", "driver_coverage_pct_median"),
    ("Pass rate %", "pass_rate_pct"),
    ("Answers replaced by the gate", "replaced"),
    ("Memory lines learned", "memory_lines"),
    ("Median latency, addressed to answer, ms", "median_latency_ms"),
    ("Traces recorded in PRISM", "prism_recorded"),
    ("Traces scored by PRISM within 15 s", "prism_scored"),
    ("Traces PRISM flagged", "prism_flagged"),
]


def table(a: dict, b: dict) -> str:
    fmt = lambda v: "—" if v is None else str(v)  # noqa: E731
    lines = [f"| | {a['session_id']} | {b['session_id']} |", "|---|---:|---:|"]
    lines += [f"| {label} | {fmt(a[key])} | {fmt(b[key])} |" for label, key in ROWS]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    v1, v2 = (argv + ["explain-v1-04", "explain-v2-04"])[:2]
    a, b = stats(v1), stats(v2)
    print(table(a, b))
    print(json.dumps({"v1": a, "v2": b}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
