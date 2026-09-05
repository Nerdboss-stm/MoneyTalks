from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

import httpx
from pydantic import BaseModel, Field

from mandate import compiler
from mandate import ledger as L
from mandate import prism_util
from mandate.compiler import CANONICAL_ORDER, CompanyState, Engine, dollars
from mandate.scenario import ROLES, build_scenario, company_day_id, ct, fmt
from mandate.schemas import Event, Mandate, Stamp

MODEL = "claude-haiku-4-5"
NOISE_TEXT = "I didn't get an order from that. Say it as: nothing over an amount until a time."
NO_RECORD = "My record doesn't show that."
DISPLAY = dict(ROLES)
ROOT = Path(__file__).resolve().parents[2]
RECORDINGS = ROOT / "recordings"

IntentKind = Literal["MANDATE", "RELEASE", "QUERY", "AGENT_QUERY", "CHANGE_QUERY", "NOISE"]
CHANGE_WORDS = ("what changed", "what has changed", "walk me through", "what moved", "why did", "what drove", "variance", "month over month", "month-over-month", "vs july", "versus july", "vs last month", "explain the month", "explain august", "happened in august", "changed in august", "the drivers", "what's driving", "what is driving", "biggest changes", "biggest movers")


class Intent(BaseModel):
    kind: IntentKind
    confidence: float = 1.0
    agent_id: str | None = None
    question: str | None = None
    message: str | None = None


class DeskError(Exception):
    pass


# ---------------------------------------------------------------- agent name matching


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def match_agent(text: str) -> tuple[str, str] | None:
    """Utterance begins with (or fuzzy-matches, edit distance <= 2) an agent display name or id."""
    low = re.sub(r"[^a-z0-9 ]", " ", text.lower()).strip()
    words = low.split()
    best: tuple[int, str, int] | None = None
    for agent_id, display in ROLES:
        for name in (display.lower(), agent_id.replace("_", " ")):
            n = len(name.split())
            head = " ".join(words[:n])
            if not head:
                continue
            d = edit_distance(head, name)
            if d <= 2 and (best is None or d < best[0]):
                best = (d, agent_id, n)
    if best is None:
        return None
    _, agent_id, n = best
    rest = " ".join(text.split()[n:]).lstrip(",:;- ").strip()
    return agent_id, rest or text


# ---------------------------------------------------------------- intent classification

MANDATE_WORDS = ("hold", "freeze", "stop", "block", "nothing over", "no payments", "do not pay", "don't pay", "until")
RELEASE_WORDS = ("release", "unhold", "unfreeze", "let it go", "let that through", "clear the hold", "lift")
QUERY_WORDS = ("cash", "payroll", "held", "hold count", "how many", "in flight", "in-flight", "mandate", "balance", "what is", "what's", "status", "total")


def is_change_question(text: str) -> bool:
    """Change-shaped question with no agent name: routed to controller_a with the full evidence JSON."""
    low = re.sub(r"\s+", " ", text.lower())
    return match_agent(text) is None and any(w in low for w in CHANGE_WORDS)


def rules_classify(text: str) -> Intent:
    low = text.lower().strip()
    if not low:
        return Intent(kind="NOISE", confidence=0.0, message=NOISE_TEXT)
    m = match_agent(text)
    if m:
        return Intent(kind="AGENT_QUERY", confidence=1.0, agent_id=m[0], question=m[1])
    if is_change_question(text):
        return Intent(kind="CHANGE_QUERY", confidence=1.0, agent_id="controller_a", question=text)
    if any(w in low for w in RELEASE_WORDS):
        return Intent(kind="RELEASE", confidence=0.9)
    if any(w in low for w in MANDATE_WORDS) and (compiler.parse_amount_cents(text) is not None or any(v in low for v in compiler.VAGUE)):
        return Intent(kind="MANDATE", confidence=0.9)
    if any(w in low for w in QUERY_WORDS):
        return Intent(kind="QUERY", confidence=0.8)
    if any(w in low for w in MANDATE_WORDS):
        return Intent(kind="MANDATE", confidence=0.7)
    return Intent(kind="NOISE", confidence=0.3, message=NOISE_TEXT)


class LLMIntent(BaseModel):
    kind: IntentKind
    confidence: float = Field(ge=0, le=1)
    agent_id: str | None = Field(default=None, description="agent id when kind is AGENT_QUERY")
    question: str | None = Field(default=None, description="the question addressed to the agent, without the name")


async def llm_classify(text: str) -> Intent:
    from langchain_anthropic import ChatAnthropic

    llm = ChatAnthropic(model=MODEL, temperature=0, max_tokens=256).with_structured_output(LLMIntent)
    system = (
        "Classify one spoken utterance from a CFO to a finance desk. Kinds: MANDATE (a standing order: hold/freeze payments over an amount until a time), "
        "RELEASE (lift a hold or a mandate), QUERY (a question about cash, payroll, held payments, in-flight payments or active mandates), "
        f"AGENT_QUERY (addressed to an agent by name; agents: {', '.join(f'{r}={d}' for r, d in ROLES)}), NOISE (anything else). "
        "Return your confidence honestly."
    )
    out = await llm.ainvoke([("system", system), ("user", text)])
    li = out if isinstance(out, LLMIntent) else LLMIntent.model_validate(out)
    return Intent(kind=li.kind, confidence=li.confidence, agent_id=li.agent_id, question=li.question)


IntentEngine = Callable[[str], Awaitable[Intent]]


async def rules_classify_async(text: str) -> Intent:
    return rules_classify(text)


def default_intent_engine() -> IntentEngine:
    if os.environ.get("MANDATE_DECIDER", "llm" if os.environ.get("ANTHROPIC_API_KEY") else "rules") == "llm":
        return llm_classify
    return rules_classify_async


async def classify(text: str, engine: IntentEngine | None = None, timeout: float = 4.0) -> Intent:
    text = (text or "").strip()
    if not text:
        return Intent(kind="NOISE", confidence=0.0, message=NOISE_TEXT)
    m = match_agent(text)
    if m:
        return Intent(kind="AGENT_QUERY", confidence=1.0, agent_id=m[0], question=m[1])
    if is_change_question(text):
        return Intent(kind="CHANGE_QUERY", confidence=1.0, agent_id="controller_a", question=text)
    engine = engine or default_intent_engine()
    try:
        intent = await asyncio.wait_for(engine(text), timeout=timeout)
    except Exception as exc:
        print(f"classify: {type(exc).__name__}: {exc}", file=sys.stderr)
        return Intent(kind="NOISE", confidence=0.0, message=NOISE_TEXT)
    if intent.confidence < 0.7:
        return Intent(kind="NOISE", confidence=intent.confidence, message=NOISE_TEXT)
    if intent.kind == "NOISE":
        intent.message = NOISE_TEXT
    return intent


# ---------------------------------------------------------------- records


def _entry(at: datetime, **kw: Any) -> dict:
    return {"at": at.isoformat(), "at_company": fmt(at), **kw}


def record_from_recording(session_id: str, agent_id: str, as_of: datetime | str | None = None) -> dict:
    """The agent's record rebuilt from a recorded run: only what that agent saw and did, up to as_of."""
    path = RECORDINGS / f"{session_id}.jsonl"
    events = [Event.model_validate_json(l) for l in path.read_text().splitlines() if l.strip()]
    cutoff = ct(as_of) if as_of else None
    by_id = build_scenario().by_id()
    rec: dict[str, list] = {"plans": [], "tool_calls": [], "reports": [], "stamps": [], "mandates_seen": []}
    bound: list[tuple[datetime, dict]] = []
    for e in events:
        if cutoff and e.ts_company > cutoff:
            break
        pl = e.payload
        if e.type == "mandate.bound":
            bound.append((e.ts_company, pl))
            continue
        if pl.get("agent_id") != agent_id:
            continue
        pid = pl.get("payment_id")
        p = by_id.get(pid) if pid else None
        if e.type == "payment.scheduled":
            rec["plans"].append(_entry(e.ts_company, tick=fmt(e.ts_company), payment_id=pid, execute_at=pl.get("at"), vendor=p.vendor if p else None, amount_cents=p.amount_cents if p else None, rail=p.rail if p else None, status_template=None, mandate_id=None))
            rec["tool_calls"].append(_entry(e.ts_company, tool="schedule_payment", args={"payment_id": pid, "at": pl.get("at")}, result=f"scheduled {pid} at {pl.get('at')}"))
        elif e.type == "payment.executed":
            rec["tool_calls"].append(_entry(e.ts_company, tool="execute_payment", args={"payment_id": pid}, result=f"executed {pid} {pl.get('vendor')} {pl.get('amount_cents')} at {fmt(e.ts_company)}"))
            rec["stamps"].append(_entry(e.ts_company, payment_id=pid, mandate_id=pl.get("mandate_id"), decision="allow", reason=pl.get("decision_reason"), transcript_excerpt=""))
        elif e.type in ("payment.held", "payment.escalated"):
            rec["tool_calls"].append(_entry(e.ts_company, tool="execute_payment" if pl.get("source") == "boundary" else "hold_payment", args={"payment_id": pid}, result=f"{'held' if e.type == 'payment.held' else 'escalated'} {pid} {pl.get('vendor')} {pl.get('amount_cents')}: {pl.get('reason')}"))
            rec["stamps"].append(_entry(e.ts_company, payment_id=pid, mandate_id=pl.get("mandate_id"), decision="hold" if e.type == "payment.held" else "escalate", reason=pl.get("reason"), transcript_excerpt=""))
        elif e.type == "status.report":
            rec["reports"].append(_entry(e.ts_company, sentence=pl.get("sentence"), source=pl.get("source"), scheduled_at_tick=pl.get("scheduled_at_tick")))
            if pl.get("source") == "tick":
                for at, m in bound:
                    if at <= e.ts_company and not any(x["mandate_id"] == m["mandate_id"] for x in rec["mandates_seen"]):
                        rec["mandates_seen"].append(_entry(e.ts_company, mandate_id=m["mandate_id"], text=m.get("text")))
    return {"agent_id": agent_id, "display": DISPLAY.get(agent_id, agent_id), "session_id": session_id, **rec}


def allowed_tokens(record: dict) -> tuple[set[str], set[str]]:
    blob = json.dumps(record)
    amounts: set[str] = set()
    for n in re.findall(r"\b\d{5,9}\b", blob):
        amounts.add(dollars(int(n)))
    for s in re.findall(r"\$[\d,]+", blob):
        amounts.add(s)
    times: set[str] = set()
    for hms in re.findall(r"\b(\d{2}):(\d{2})(?::(\d{2}))?\b", blob):
        times.add(f"{hms[0]}:{hms[1]}")
    return amounts, times


def sanitize_answer(answer: str, record: dict) -> str:
    amounts, times = allowed_tokens(record)
    for s in re.findall(r"\$[\d,]+(?:\.\d+)?", answer):
        if s.split(".")[0] not in amounts:
            return NO_RECORD
    for hm in re.findall(r"\b(\d{1,2}:\d{2})(?::\d{2})?\b", answer):
        if hm.zfill(5) not in times:
            return NO_RECORD
    return answer


def _plan_line(pl: dict) -> str:
    vendor = pl.get("vendor") or pl.get("payment_id")
    amt = dollars(pl["amount_cents"]) if pl.get("amount_cents") else ""
    at = (pl.get("execute_at") or "")[-8:-3]
    return f"{vendor} {amt} at {at}".replace("  ", " ").strip()


def rules_answer(record: dict, question: str) -> str:
    q = question.lower()
    plans = record.get("plans", [])
    reports = record.get("reports", [])
    stamps = record.get("stamps", [])
    execs = [c for c in record.get("tool_calls", []) if c["tool"] == "execute_payment" and str(c.get("result", "")).startswith("executed")]
    if any(w in q for w in ("week", "plan", "schedule", "coming up", "queue")):
        if not plans:
            return NO_RECORD
        return f"My plan so far: {'; '.join(_plan_line(p) for p in plans)}."
    if any(w in q for w in ("why", "held", "hold", "escalat")):
        held = [s for s in stamps if s.get("decision") in ("hold", "escalate")]
        if not held:
            return NO_RECORD
        s = held[-1]
        p = next((x for x in plans if x.get("payment_id") == s.get("payment_id")), {})
        who = f"{p.get('vendor')} {dollars(p['amount_cents'])}" if p.get("amount_cents") else s.get("payment_id")
        return f"{who} is {s['decision']}ed at {s['at_company'][-8:-3]}: {s.get('reason')}."[:400].replace("holded", "held")
    if any(w in q for w in ("comply", "complied", "order", "mandate", "compliant")):
        if not reports:
            return NO_RECORD
        last = reports[-1]
        if execs:
            e = execs[-1]
            pid = e.get("args", {}).get("payment_id")
            p = next((x for x in plans if x.get("payment_id") == pid), {})
            who = f"{p.get('vendor')} {dollars(p['amount_cents'])}" if p.get("amount_cents") else pid
            return f"At {e['at_company'][-8:-3]} I executed {who}; my status at the time said: {last['sentence']}"
        return f"My last status said: {last['sentence']}"
    if reports:
        return f"My last status said: {reports[-1]['sentence']}"
    return NO_RECORD


async def llm_answer(record: dict, question: str) -> str:
    from langchain_anthropic import ChatAnthropic

    llm = ChatAnthropic(model=MODEL, temperature=0, max_tokens=200)
    system = (
        f"You are {record.get('display')}, a finance agent. Answer the CFO in the first person, at most two sentences. "
        "Use only times and amounts that appear in RECORD; amounts in RECORD are cents unless prefixed with $. "
        f"If RECORD does not contain the answer, reply exactly: {NO_RECORD}"
    )
    slim = {k: record.get(k) for k in ("plans", "tool_calls", "reports", "stamps", "mandates_seen")}
    out = await llm.ainvoke([("system", system), ("user", f"QUESTION: {question}\nRECORD: {json.dumps(slim)[:12000]}")])
    return str(out.content).strip()


AnswerEngine = Callable[[dict, str], Awaitable[str]]


async def rules_answer_async(record: dict, question: str) -> str:
    return rules_answer(record, question)


def default_answer_engine() -> AnswerEngine:
    if os.environ.get("MANDATE_DECIDER", "llm" if os.environ.get("ANTHROPIC_API_KEY") else "rules") == "llm":
        return llm_answer
    return rules_answer_async


def voice_trace_body(fleet: Any, agent_id: str, question: str, answer: str, latency_ms: int, session_id: str | None = None, metadata: dict | None = None, project: str | None = None) -> dict:
    """The /api/traces body for one spoken exchange (docs/prism-notes.md §3). Every trace carries failure_class."""
    return {
        "project_id": project or os.environ.get("PRISMTRACE_PROJECT_ID"),
        "model": MODEL,
        "input_messages": [{"role": "user", "content": question}],
        "output_message": answer,
        "latency_ms": latency_ms,
        "session_id": session_id or fleet.session_id,
        "agent_id": agent_id,
        "agent_name": agent_id,
        "metadata": {"channel": "voice", "company_day_id": company_day_id(fleet.clock.now()), "mandate_id": fleet.current_mandate_id(), "run_version": fleet.run_version, "tick": None, "seed": fleet.scenario.seed, "failure_class": prism_util.FAILURE_NONE, **(metadata or {})},
    }


async def prism_voice_trace(fleet: Any, agent_id: str, question: str, answer: str, latency_ms: int, session_id: str | None = None, metadata: dict | None = None) -> str | None:
    """Plain trace in the current fleet session (or the given one), channel voice (docs/prism-notes.md §3)."""
    if os.environ.get("PRISM_HANDLERS", "on") == "off":
        return None
    key, project, host = os.environ.get("PRISMTRACE_API_KEY"), os.environ.get("PRISMTRACE_PROJECT_ID"), os.environ.get("PRISMTRACE_HOST", prism_util.DEFAULT_HOST)
    if not (key and project):
        return None
    body = voice_trace_body(fleet, agent_id, question, answer, latency_ms, session_id, metadata, project)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{host.rstrip('/')}/api/traces", headers={"X-PRISMtrace-Key": key}, json=body)
            return r.json().get("id") if r.status_code == 200 else None
    except Exception as exc:
        print(f"prism voice trace: {exc}", file=sys.stderr)
        return None


async def answer_as(record: dict, question: str, engine: AnswerEngine | None = None) -> str:
    engine = engine or default_answer_engine()
    try:
        raw = await asyncio.wait_for(engine(record, question), timeout=8.0)
    except Exception as exc:
        print(f"answer_as: {type(exc).__name__}: {exc}; falling back to record rules", file=sys.stderr)
        raw = rules_answer(record, question)
    return sanitize_answer(raw.strip(), record)


# ---------------------------------------------------------------- desk


class Meeting:
    """An explain evidence JSON is active: owners answer from their slices, the change question from the full JSON."""

    def __init__(self, engine: Any, mode: str, index: int, directory: Path) -> None:
        self.engine = engine
        self.mode = mode
        self.index = index
        self.directory = directory
        self.session_id = f"explain-{mode}-{index:02d}"
        self.evidence = engine.to_evidence_json()

    def summary(self) -> dict:
        from explain.engine import REVENUE_CATEGORIES

        variances = self.evidence["variances"]
        var_ids = {r["target"]: r["id"] for r in self.evidence["evidence"] if r["kind"] == "variance"}
        total = next((v for v in variances if v["key"] == "TOTAL:revenue"), None)
        expense = next((v for v in variances if v["key"] == "TOTAL:expense"), None)
        accounts = sorted([v for v in variances if not v["key"].startswith("TOTAL:")], key=lambda v: v["rank"] or 999)

        def bad(v: dict) -> bool:
            revenue = v.get("category") in REVENUE_CATEGORIES or str(v.get("key", "")).startswith("4")
            return v["delta_cents"] < 0 if revenue else v["delta_cents"] > 0

        def item(v: dict) -> dict:
            return {"account": v["key"], "category": v.get("category"), "owner_agent": v.get("owner_agent"), "delta_cents": v["delta_cents"], "delta_pct": v.get("delta_pct"), "direction": v.get("direction"), "bad": bad(v), "evidence_id": var_ids.get(v["key"])}

        owners: dict[str, list[dict]] = {}
        for v in accounts:
            owners.setdefault(v.get("owner_agent", ""), []).append(item(v))
        return {
            "session_id": self.session_id, "mode": self.mode, "run_index": self.index, "directory": str(self.directory), "periods": self.evidence["periods"],
            "total_revenue": total, "total_expense": expense,
            "headline": {"revenue_pct": total["delta_pct"] if total else None, "expense_pct": expense["delta_pct"] if expense else None, "revenue_evidence_id": var_ids.get("TOTAL:revenue"), "expense_evidence_id": var_ids.get("TOTAL:expense")},
            "top_variances": [item(v) for v in accounts[:3]], "owners": {k: v[:2] for k, v in owners.items()}, "evidence_rows": len(self.evidence["evidence"]),
        }


class Desk:
    def __init__(self, fleet: Any) -> None:
        self.fleet = fleet
        self.pending: dict[str, tuple[Mandate, str, list[str]]] = {}
        self.pending_release: dict | None = None
        self.replay_session: str | None = None
        self.meeting: Meeting | None = None
        self.memory_path: Path | None = None
        self._tasks: set[asyncio.Task] = set()

    # ---- explain meeting

    def load_meeting(self, directory: Path | str, mode: str = "v2", index: int = 1) -> Meeting:
        from explain.meeting import load_engine

        directory = Path(directory)
        self.meeting = Meeting(load_engine(directory), mode, index, directory)
        return self.meeting

    async def publish_meeting_loaded(self) -> None:
        if self.meeting:
            await self.fleet.publish("meeting.loaded", **self.meeting.summary())

    async def _explain_turn(self, intent: Intent, text: str, speak: Callable[[str, str], Awaitable[Any]], llm: Any = "auto") -> dict:
        from explain import owners as O
        from explain import prism_steps
        from mandate import voice as V

        m = self.meeting
        assert m is not None
        memory = O.read_memory(self.memory_path or O.MEMORY_FILE) if m.mode == "v2" else ""
        agent_id = intent.agent_id or "controller_a"
        await self.fleet.publish("agent.addressed", agent_id=agent_id, question=intent.question or text)
        t0 = time.perf_counter()
        if intent.kind == "CHANGE_QUERY":
            res = await O.answer_change(text, m.mode, memory, m.engine, llm, m.session_id)
        else:
            res = await O.answer_as_owner(agent_id, intent.question or text, m.mode, memory, m.engine, llm, session_id=m.session_id)
        latency = int((time.perf_counter() - t0) * 1000)
        answer = res["answer"]
        await self.fleet.publish("agent.retrieving", agent_id=agent_id, rows=res["rows"], scope=res["scope"], mode=m.mode)
        await self.fleet.publish("agent.verified", agent_id=agent_id, checks=res["checks"], ok=bool(res["verify"].get("ok")), unverifiable=res["verify"].get("unverifiable_figures", []), coverage=res["verify"].get("driver_coverage_pct"), mode=m.mode)
        job = await speak(answer, V.VOICES.for_agent(agent_id))
        failure_class = res.get("failure_class", prism_util.FAILURE_NONE)
        trace_id = await prism_voice_trace(self.fleet, agent_id, text, answer, latency, session_id=m.session_id, metadata={"mode": m.mode, "agent_id": agent_id, "verifier": res["verify"], "scope": res["scope"], "failure_class": failure_class})
        await self.fleet.publish("agent.traced", agent_id=agent_id, session=m.session_id, trace_id=trace_id, recorded=trace_id is not None)
        steps, sid = res["steps"], m.session_id
        if self.fleet.agents[agent_id].prism:
            self.fleet.prism.enqueue("trajectory", lambda: prism_steps.submit_steps(sid, trace_id, steps, agent_id=agent_id, model=res["model"], failure_class=failure_class))
        out = {"intent": intent.kind, "confidence": intent.confidence, "text": text, "agent_id": agent_id, "answer": answer, "raw_answer": res["raw_answer"], "verify": res["verify"], "replaced": res["replaced"], "citations": res["citations"], "figures": res["figures"], "rows": res["rows"], "checks": res["checks"], "mode": m.mode, "session_id": m.session_id, "audio_url": job.url if not job.error else V.cache_url(answer, job.voice_id), "duration_ms": job.duration_ms, "audio_error": job.error, "prism_trace_id": trace_id, "failure_class": failure_class}
        await self.fleet.publish("agent.answer", agent_id=agent_id, text=answer, audio_url=out["audio_url"], duration_ms=job.duration_ms, verify=res["verify"], citations=res["citations"], figures=res["figures"], mode=m.mode, replaced=res["replaced"], failure_class=failure_class)
        self._track(asyncio.create_task(prism_util.publish_verdict(self.fleet.publish, trace_id)))
        return out

    def _track(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def company_state(self) -> CompanyState:
        sc = self.fleet.scenario
        return CompanyState(now=self.fleet.clock.now(), payroll_due=sc.company.payroll.due, tax_due=sc.company.tax.due, agents=[a.id for a in sc.agents], vendors=sorted({p.vendor for p in sc.payments}))

    def _stage(self, mandate: Mandate, readback: str, questions: list[str]) -> dict:
        self.pending[mandate.id] = (mandate, readback, questions)
        conflict = None
        for existing in self.fleet.active_mandates():
            conflict = compiler.conflicts(existing, mandate)
            if conflict:
                break
        return {"mandate_id": mandate.id, "readback_text": readback, "questions": questions, "conflict": conflict, "bindable": not questions, "mandate": mandate.model_dump(mode="json")}

    async def compile(self, text: str, engine: Engine | None = None) -> dict:
        mandate, readback, questions = compiler.compile(text, self.company_state(), engine)
        out = self._stage(mandate, readback, questions)
        await self.fleet.publish("desk.compiled", mandate_id=mandate.id, text=text, readback=readback, questions=questions, conflict=out["conflict"])
        return out

    async def inject(self) -> dict:
        text = os.environ.get("CANONICAL_ORDER", CANONICAL_ORDER)
        mandate = compiler.canonical_mandate(self.fleet.clock.now(), text)
        out = self._stage(mandate, compiler.CANONICAL_READBACK, [])
        await self.fleet.publish("desk.compiled", mandate_id=mandate.id, text=text, readback=compiler.CANONICAL_READBACK, questions=[], conflict=out["conflict"], injected=True)
        return out

    async def confirm(self, mandate_id: str) -> Mandate | dict:
        if mandate_id == "release" or (self.pending_release and self.pending_release["id"] == mandate_id):
            return await self.confirm_release()
        if mandate_id not in self.pending:
            raise DeskError(f"no pending mandate {mandate_id}")
        mandate, _, questions = self.pending[mandate_id]
        if questions:
            raise DeskError("cannot bind: " + " ".join(questions))
        del self.pending[mandate_id]
        now = self.fleet.clock.now()
        await self.fleet.publish("desk.confirmed", mandate_id=mandate_id, at=fmt(now))
        await self.fleet.bind_mandate(mandate, now)
        prism_util.register_mandate(self.fleet.session_id, mandate.threshold_cents, mandate.window_start, mandate.window_end, mandate.exceptions)
        return mandate

    def cancel(self, mandate_id: str) -> None:
        self.pending.pop(mandate_id, None)

    # ---- queries (ledger + mandate store, no LLM)

    def answer_query(self, text: str) -> str:
        low = text.lower()
        fleet = self.fleet
        led = fleet.ledger
        sc = fleet.scenario
        if "payroll" in low:
            due = sc.company.payroll.due
            p = led.payments.get(fleet.payroll_run_id) if fleet.payroll_run_id else None
            if p and p.status == "executed":
                return f"Payroll {dollars(p.amount_cents)} was paid at {fmt(p.executed_at)}."
            risk = L.payroll_at_risk(led, sc.company.payroll.amount_cents, due, frozenset(sc.exception_eligible))
            return f"Payroll {dollars(sc.company.payroll.amount_cents)} is due {fmt(due)} and is {'at risk' if risk else 'covered'} on current cash."
        if "held" in low or "hold" in low:
            held = [p for p in led.payments.values() if p.status in ("held", "escalated")]
            total = sum(p.amount_cents for p in held)
            return f"{len(held)} payment{'s' if len(held) != 1 else ''} held totalling {dollars(total)}." if held else "Nothing is held."
        if "flight" in low or "over" in low or "above" in low:
            amt = compiler.parse_amount_cents(text) or fleet.scenario.threshold_cents
            queued = {pid for a in fleet.agents.values() for _, pid in a.queue}
            live = [p for p in led.payments.values() if p.id in queued and p.amount_cents > amt]
            return f"{len(live)} payment{'s' if len(live) != 1 else ''} in flight over {dollars(amt)}, totalling {dollars(sum(p.amount_cents for p in live))}."
        if "mandate" in low or "order" in low:
            act = fleet.active_mandates()
            if not act:
                return "No active mandates."
            return "Active: " + "; ".join(f"{m.id} holds {compiler._scope_phrase(m.scope)} over {dollars(m.threshold_cents)} until {fmt(m.window_end)}" for m in act) + "."
        return f"Cash is {dollars(L.balance(led))}."

    # ---- release

    def resolve_release(self, text: str) -> dict | None:
        low = text.lower()
        fleet = self.fleet
        if "mandate" in low or "order" in low:
            act = fleet.active_mandates()
            if act:
                m = act[-1]
                return {"kind": "mandate", "id": m.id, "readback": f"Release mandate {m.id}: {compiler._scope_phrase(m.scope)} over {dollars(m.threshold_cents)} until {fmt(m.window_end)}. Confirm?"}
            return None
        held = [p for p in fleet.ledger.payments.values() if p.status in ("held", "escalated")]
        best = None
        for p in held:
            v = p.vendor.lower()
            score = 0 if v in low else min(edit_distance(low[i : i + len(v)], v) for i in range(max(1, len(low) - len(v) + 1)))
            if score <= 2 and (best is None or score < best[0]):
                best = (score, p)
        if best is None and len(held) == 1:
            best = (0, held[0])
        if best is None:
            return None
        p = best[1]
        return {"kind": "payment", "id": p.id, "readback": f"Release {p.vendor} {dollars(p.amount_cents)}, {p.status} under {p.stamp.mandate_id if p.stamp else 'no mandate'}. Confirm?"}

    async def confirm_release(self) -> dict:
        if not self.pending_release:
            raise DeskError("no pending release")
        rel = self.pending_release
        self.pending_release = None
        now = self.fleet.clock.now()
        if rel["kind"] == "mandate":
            await self.fleet.release_mandate(rel["id"], now)
            return {"released": rel["id"], "kind": "mandate"}
        stamp = Stamp(mandate_id=self.fleet.current_mandate_id() or "none", checked_at=now, decision="release", reason="released by the desk on confirm", transcript_excerpt=rel.get("transcript", "")[:120])
        await self.fleet.release_payment(rel["id"], stamp)
        return {"released": rel["id"], "kind": "payment"}

    # ---- records

    def record_for(self, agent_id: str, as_of: datetime | str | None = None, session: str | None = None) -> dict:
        session = session or self.replay_session
        if session:
            return record_from_recording(session, agent_id, as_of or self.fleet.clock.now())
        return self.fleet.get_record(agent_id, as_of or self.fleet.clock.now())

    # ---- one voice turn

    async def voice_turn(self, text: str | None, as_of: datetime | str | None = None, session: str | None = None, intent_engine: IntentEngine | None = None, answer_engine: AnswerEngine | None = None, speak: Callable[[str, str], Awaitable[Any]] | None = None, explain_llm: Any = "auto") -> dict:
        from mandate import voice as V

        speak = speak or V.speak
        if text is None:
            intent = Intent(kind="NOISE", confidence=0.0, message=NOISE_TEXT)
        else:
            intent = await classify(text, intent_engine)
        out: dict[str, Any] = {"intent": intent.kind, "confidence": intent.confidence, "text": text, "agent_id": intent.agent_id}
        if intent.kind in ("AGENT_QUERY", "CHANGE_QUERY") and self.meeting is not None:
            return await self._explain_turn(intent, text or "", speak, explain_llm)
        if intent.kind == "CHANGE_QUERY":
            intent = Intent(kind="NOISE", confidence=intent.confidence, message="No meeting is loaded. Load an evidence set first.")
        if intent.kind == "AGENT_QUERY" and intent.agent_id:
            await self.fleet.publish("agent.addressed", agent_id=intent.agent_id, question=intent.question)
            t0 = time.perf_counter()
            record = self.record_for(intent.agent_id, as_of, session)
            answer = await answer_as(record, intent.question or text or "", answer_engine)
            latency = int((time.perf_counter() - t0) * 1000)
            job = await speak(answer, V.VOICES.for_agent(intent.agent_id))
            await prism_voice_trace(self.fleet, intent.agent_id, text or "", answer, latency)
            out |= {"answer": answer, "audio_url": job.url if not job.error else V.cache_url(answer, job.voice_id), "duration_ms": job.duration_ms, "audio_error": job.error}
            await self.fleet.publish("agent.answer", agent_id=intent.agent_id, text=answer, audio_url=out["audio_url"], duration_ms=job.duration_ms)
            return out
        if intent.kind == "QUERY":
            answer = self.answer_query(text or "")
            kind = "query.answer"
        elif intent.kind == "MANDATE":
            comp = await self.compile(text or "")
            answer = comp["readback_text"]
            out |= {"mandate_id": comp["mandate_id"], "bindable": comp["bindable"]}
            kind = "desk.answer"
        elif intent.kind == "RELEASE":
            rel = self.resolve_release(text or "")
            if rel is None:
                answer = "I can't find a held payment or an active mandate to release."
            else:
                self.pending_release = rel | {"transcript": text or ""}
                answer = rel["readback"]
                out |= {"release": rel}
            kind = "desk.answer"
        else:
            answer = intent.message or NOISE_TEXT
            kind = "desk.answer"
        job = await speak(answer, V.VOICES.desk)
        out |= {"answer": answer, "audio_url": job.url if not job.error else V.cache_url(answer, job.voice_id), "duration_ms": job.duration_ms, "audio_error": job.error}
        await self.fleet.publish(kind, text=answer, audio_url=out["audio_url"], duration_ms=job.duration_ms, intent=intent.kind)
        return out


# ---------------------------------------------------------------- PRISM links and prove extras

FLAGGED_SESSION = "explain-v1-01"
FLAGGED_AGENT = "procurement"


def flagged_trace_id(session_id: str = FLAGGED_SESSION, agent_id: str = FLAGGED_AGENT) -> str | None:
    """The trace of the first answer in the recording that verify.py failed for that owner (docs/gate.md's rule)."""
    path = RECORDINGS / f"{session_id}.jsonl"
    if not path.exists():
        return None
    failed = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        pl = e.get("payload") or {}
        if e.get("type") == "agent.verified" and pl.get("agent_id") == agent_id and not pl.get("ok", True):
            failed = True
        elif failed and e.get("type") == "agent.traced" and pl.get("agent_id") == agent_id:
            return pl.get("trace_id")
    return None


def links_payload() -> dict:
    trace = flagged_trace_id()
    entries = [
        prism_util.links("explain-v1-01", label="explain-v1-01 · freeform meeting") | {"key": "explain_v1"},
        prism_util.links("explain-v2-01", label="explain-v2-01 · grounded meeting") | {"key": "explain_v2"},
        prism_util.links(FLAGGED_SESSION, trace, label=f"{DISPLAY.get(FLAGGED_AGENT, FLAGGED_AGENT)} · figures verify.py failed") | {"key": "flagged_trace"},
        prism_util.links("mandate-v1-01", label="mandate-v1-01 · payments, stale plan executes") | {"key": "mandate_v1"},
        prism_util.links("mandate-v2-01", label="mandate-v2-01 · payments, boundary holds") | {"key": "mandate_v2"},
    ]
    return {"host": os.environ.get("PRISMTRACE_HOST", prism_util.DEFAULT_HOST).rstrip("/"), "project_id": os.environ.get("PRISMTRACE_PROJECT_ID"), "link_label": "Open PRISM", "entries": entries}


def prove_extras() -> dict:
    return {"links": links_payload(), "remediation": prism_util.remediation(), "prism_credits_used": prism_util.credits_used(), "prism_credits_cycle": prism_util.CREDITS_CYCLE}


# ---------------------------------------------------------------- fallback cache items


def fallback_items() -> list[tuple[str, str]]:
    from mandate import voice as V

    items = [(compiler.CANONICAL_READBACK, V.VOICES.desk)]
    for session, question, as_of in (
        ("mandate-v1-01", "did you comply with my order?", "10:04:00"),
        ("mandate-v2-01", "why is Halden held?", "10:04:00"),
        ("mandate-v1-01", "what's your week?", "10:01:00"),
    ):
        rec = record_from_recording(session, "ap_west", as_of)
        items.append((sanitize_answer(rules_answer(rec, question), rec), V.VOICES.for_agent("ap_west")))
    return items


def typed_path(base: str = "http://localhost:8000") -> int:
    """Keyboard-only Desk: type an order, read the read-back, press Enter to confirm. No audio."""
    client = httpx.Client(base_url=base, timeout=30)
    print("MANDATE desk (typed). Empty line to quit. 'inject' sends the canonical order.")
    while True:
        try:
            text = input("order> ").strip()
        except EOFError:
            return 0
        if not text:
            return 0
        r = client.post("/desk/inject") if text == "inject" else client.post("/desk/compile", json={"text": text})
        body = r.json()
        print(body.get("readback_text"))
        if body.get("conflict"):
            print("conflict:", body["conflict"])
        if not body.get("bindable"):
            continue
        ans = input("[Enter]=confirm  n=cancel > ").strip().lower()
        if ans in ("", "y", "yes"):
            c = client.post("/desk/confirm", json={"mandate_id": body["mandate_id"]})
            print("bound" if c.status_code == 200 else f"not bound: {c.text}")
        else:
            print("cancelled")


if __name__ == "__main__":
    sys.exit(typed_path(os.environ.get("MANDATE_API", "http://localhost:8000")))
