from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

from explain import prism_steps, verify
from mandate.scenario import ROLES

ROOT = Path(__file__).resolve().parents[2]
RULES_FILE = ROOT / "config" / "owners.yaml"
MEMORY_FILE = ROOT / "docs" / "context-memory.md"
AGENT_IDS = {r for r, _ in ROLES}
DISPLAY = dict(ROLES)
MODEL = "claude-haiku-4-5"
NO_RECORDS = "My records don't show that."
V1_ROWS = 150


@dataclass(frozen=True)
class Rule:
    match: dict[str, str]
    owner: str


@dataclass(frozen=True)
class Rules:
    default: str
    rules: tuple[Rule, ...]


def load_rules(path: Path = RULES_FILE) -> Rules:
    data = yaml.safe_load(path.read_text())
    default = str(data.get("default", "controller_a"))
    rules = []
    for r in data.get("rules") or []:
        owner = str(r["owner"])
        if owner not in AGENT_IDS:
            raise ValueError(f"owners.yaml: unknown agent id {owner!r}")
        rules.append(Rule(match={str(k): str(v) for k, v in (r.get("match") or {}).items()}, owner=owner))
    if default not in AGENT_IDS:
        raise ValueError(f"owners.yaml: unknown default agent id {default!r}")
    return Rules(default=default, rules=tuple(rules))


def assign(row: dict, rules: Rules) -> str:
    for rule in rules.rules:
        if all(fnmatch.fnmatchcase(str(row.get(col, "")).lower(), pat.lower()) for col, pat in rule.match.items()):
            return rule.owner
    return rules.default


# ---------------------------------------------------------------- memory


def read_memory(path: Path = MEMORY_FILE) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def learned_line(engine: Any) -> str:
    """One deterministic sentence from the evidence: where revenue concentrates and who owns the biggest expense move."""
    rev = engine.drivers("TOTAL:revenue")
    conc = next((k for k in rev["concentration"] if k.dimension == "customer"), None)
    parts = []
    if conc:
        parts.append(f"revenue {rev['direction']} concentrates in {', '.join(conc.values)} ({conc.share:.0f}% of the change)")
    expenses = [v for v in engine.compare() if not v.key.startswith("TOTAL:") and v.category in ("cloud_hosting", "payroll", "vendor_spend")]
    if expenses:
        top = max(expenses, key=lambda v: abs(v.delta_cents))
        parts.append(f"{top.category.replace('_', ' ')} spend is {DISPLAY.get(top.owner_agent, top.owner_agent)}'s ({top.delta_pct:+.1f}%)")
    return "; ".join(parts) + "." if parts else "no material movement."


def learn(engine: Any, session_id: str, path: Path = MEMORY_FILE, today: date | None = None) -> str:
    line = f"- {(today or date.today()).isoformat()} {session_id}: {learned_line(engine)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_memory(path)
    if not existing:
        existing = "# Context memory\n\nOne learned line per grounded (v2) meeting run. v2 agents read this before answering.\n\n"
    path.write_text(existing.rstrip("\n") + "\n" + line + "\n", encoding="utf-8")
    return line


# ---------------------------------------------------------------- LLM plumbing

LLM = Callable[[str, str, float], Awaitable[str]]


async def haiku(system: str, user: str, temperature: float) -> str:
    from langchain_anthropic import ChatAnthropic

    llm = ChatAnthropic(model=MODEL, temperature=temperature, max_tokens=400)
    out = await llm.ainvoke([("system", system), ("user", user)])
    return str(out.content).strip()


def default_llm() -> LLM | None:
    if os.environ.get("EXPLAIN_LLM", "on") == "off" or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    return haiku


def _strip_ids(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_ids(v) for k, v in obj.items() if k != "txn_ids"}
    if isinstance(obj, list):
        return [_strip_ids(v) for v in obj]
    return obj


def evidence_prompt(evidence: dict) -> str:
    slim = {
        "periods": evidence.get("periods"),
        "totals": evidence.get("totals"),
        "variances": [{k: v[k] for k in ("key", "prior", "current", "delta_cents", "delta_pct", "owner_agent") if k in v} for v in evidence.get("variances", [])],
        "evidence": [{"id": r["id"], "kind": r["kind"], "target": r["target"], "statement": r["statement"]} for r in evidence.get("evidence", [])],
    }
    return json.dumps(_strip_ids(slim), separators=(",", ":"))


def top_driver(evidence: dict) -> dict | None:
    rows = [r for r in evidence.get("evidence", []) if r.get("kind") == "driver"]
    if not rows:
        rows = [r for r in evidence.get("evidence", []) if r.get("kind") in ("concentration", "variance")]
    if not rows:
        return None
    return max(rows, key=lambda r: abs(int(r.get("figures", {}).get("contribution_cents", r.get("figures", {}).get("delta_cents", 0)) or 0)))


def grounded_fallback(evidence: dict) -> str:
    r = top_driver(evidence)
    return f"{r['statement']} [{r['id']}]" if r else NO_RECORDS


V2_SYSTEM = (
    "You are {display}, a finance agent, answering the CFO in a variance review meeting. Rules: at most {sentences} sentences, first person. "
    "Use ONLY figures that appear in EVIDENCE below; never compute or estimate a new number. Every claim ends with the citation of the evidence row it comes from, like [E7]. "
    "If EVIDENCE does not contain the answer, reply exactly: {no_records}"
)
V1_SYSTEM = (
    "You are {display}, a finance agent in a variance review meeting comparing {prior} to {current}. The CFO wants a crisp read. "
    "Answer in two sentences with specific percentages and dollar amounts based on the transaction rows you are given."
)


def _rows_for_owner(engine: Any, agent_id: str, limit: int) -> list[dict]:
    rows = [r for r in engine.prior_txns + engine.current_txns if r.get("owner_agent") == agent_id]
    return rows[:limit]


def _rows_prompt(rows: list[dict]) -> str:
    cols = ["date", "customer", "segment", "category", "account", "vendor", "amount_cents"]
    return "\n".join(",".join(str(r.get(c, "")) for c in cols) for r in rows)


async def answer_as_owner(agent_id: str, question: str, mode: str, memory: str, engine: Any, llm: LLM | None = None, rows_limit: int = V1_ROWS, session_id: str = "") -> dict:
    """v2 = grounded on slice_for_owner + memory, verified before anything is spoken. v1 = freeform on raw rows, verified after."""
    llm = llm if llm is not None else default_llm()
    display = DISPLAY.get(agent_id, agent_id)
    slice_ = engine.slice_for_owner(agent_id)
    steps: list[dict] = []
    if mode == "v1":
        rows = _rows_for_owner(engine, agent_id, rows_limit)
        steps.append(prism_steps.tool_step("raw_rows", {"agent_id": agent_id, "limit": rows_limit}, f"{len(rows)} rows, no totals", ""))
        if llm is None:
            raw = grounded_fallback(slice_)
            source = "rules"
        else:
            raw = await llm(V1_SYSTEM.format(display=display, prior=engine.prior_label, current=engine.current_label), f"QUESTION: {question}\nROWS (date,customer,segment,category,account,vendor,amount_cents):\n{_rows_prompt(rows)}", 0.7)
            source = MODEL
        steps.append({"step_type": "reasoning", "label": f"{source} temp 0.7", "output_summary": raw[:200], "duration_ms": 0, "token_count": 0, "status": "success"})
        result = verify.check(raw, slice_)
        steps.append(prism_steps.tool_step("verify", {"mode": mode}, json.dumps(result)[:200], ""))
        answer = raw
        replaced = False
    else:
        steps.append(prism_steps.tool_step("slice_lookup", {"agent_id": agent_id}, f"{len(slice_.get('evidence', []))} evidence rows across {len(slice_.get('accounts', []))} account(s)", ""))
        if llm is None:
            raw = grounded_fallback(slice_)
            source = "rules"
        else:
            user = f"CONTEXT MEMORY:\n{memory.strip() or '(none)'}\n\nQUESTION: {question}\n\nEVIDENCE: {evidence_prompt(slice_)}"
            raw = await llm(V2_SYSTEM.format(display=display, sentences=2, no_records=NO_RECORDS), user, 0.0)
            source = MODEL
        steps.append({"step_type": "reasoning", "label": f"{source} temp 0", "output_summary": raw[:200], "duration_ms": 0, "token_count": 0, "status": "success"})
        result = verify.check(raw, slice_)
        steps.append(prism_steps.tool_step("verify", {"mode": mode}, json.dumps(result)[:200], "", status="success" if result["ok"] else "error"))
        replaced = not result["ok"]
        answer = raw if result["ok"] else grounded_fallback(slice_)
        if replaced:
            result = {"replaced_with": "top driver statement", "first_attempt": result} | verify.check(answer, slice_)
    steps.append(prism_steps.final_step(answer, "", "answer"))
    return {"agent_id": agent_id, "display": display, "mode": mode, "question": question, "answer": answer, "raw_answer": raw, "replaced": replaced, "verify": result, "citations": result.get("citations", []), "scope": "slice", "model": source, "steps": steps, "session_id": session_id}


async def answer_change(question: str, mode: str, memory: str, engine: Any, llm: LLM | None = None, session_id: str = "") -> dict:
    """The change question: controller_a answers from the FULL evidence JSON, three sentences allowed."""
    llm = llm if llm is not None else default_llm()
    full = engine.to_evidence_json()
    steps = [prism_steps.tool_step("evidence_lookup", {"scope": "full"}, f"{len(full['evidence'])} evidence rows, {len(full['variances'])} variances", "")]
    if llm is None or mode == "v1":
        if mode == "v1" and llm is not None:
            rows = engine.prior_txns[:V1_ROWS // 2] + engine.current_txns[:V1_ROWS // 2]
            raw = await llm(V1_SYSTEM.format(display="Controller A", prior=engine.prior_label, current=engine.current_label), f"QUESTION: {question}\nROWS:\n{_rows_prompt(rows)}", 0.7)
            source = MODEL
        else:
            raw = grounded_fallback(full)
            source = "rules"
    else:
        user = f"CONTEXT MEMORY:\n{memory.strip() or '(none)'}\n\nQUESTION: {question}\n\nEVIDENCE: {evidence_prompt(full)}"
        raw = await llm(V2_SYSTEM.format(display="Controller A", sentences=3, no_records=NO_RECORDS), user, 0.0)
        source = MODEL
    steps.append({"step_type": "reasoning", "label": f"{source}", "output_summary": raw[:200], "duration_ms": 0, "token_count": 0, "status": "success"})
    result = verify.check(raw, full)
    replaced = mode != "v1" and not result["ok"]
    answer = grounded_fallback(full) if replaced else raw
    if replaced:
        result = {"replaced_with": "top driver statement", "first_attempt": result} | verify.check(answer, full)
    steps.append(prism_steps.tool_step("verify", {"mode": mode}, json.dumps(result)[:200], "", status="success" if result["ok"] else "error"))
    steps.append(prism_steps.final_step(answer, "", "answer"))
    return {"agent_id": "controller_a", "display": "Controller A", "mode": mode, "question": question, "answer": answer, "raw_answer": raw, "replaced": replaced, "verify": result, "citations": result.get("citations", []), "scope": "full", "model": source, "steps": steps, "session_id": session_id}
