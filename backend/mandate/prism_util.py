"""PRISM helpers shared by the fleet, the smoke and the M3 explain agents.

Facts used here come from docs/prism-notes.md: PRISMtrace(api_key, host, project_id, timeout) and
PRISMtrace.submit_trajectory(steps, agent_name, agent_id, conversation_id, request_id, model) (§4),
PRISMtraceLangGraphHandler(api_key, project_id, host, agent_name, session_id) with handler.trace_id and
flush() (§2, §4). Nothing here blocks the fleet loop: every network call goes through PrismQueue.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

MODEL = "claude-haiku-4-5"
HTTP_TIMEOUT = 10
RETRIES = 2
BACKOFF = (0.5, 2.0)
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOST = "https://prism.blockconvey.com"

# Root Cause failure classes carried on every trace, span batch and trajectory (shared/events.md).
FAILURE_NONE = "none"
FAILURE_HALLUCINATED = "hallucinated_figure"
FAILURE_STALE = "stale_status_contradiction"


def config() -> dict[str, str] | None:
    key, project = os.environ.get("PRISMTRACE_API_KEY"), os.environ.get("PRISMTRACE_PROJECT_ID")
    if os.environ.get("PRISM_HANDLERS", "on") == "off" or not (key and project):
        return None
    return {"api_key": key, "project_id": project, "host": os.environ.get("PRISMTRACE_HOST", "https://prism.blockconvey.com")}


_client: Any = None


def client() -> Any:
    global _client
    if _client is None:
        from prismtrace import PRISMtrace

        cfg = config()
        if cfg is None:
            raise RuntimeError("PRISM is off or PRISMTRACE_* is not set")
        _client = PRISMtrace(api_key=cfg["api_key"], host=cfg["host"], project_id=cfg["project_id"], timeout=HTTP_TIMEOUT)
    return _client


def make_handler(agent_id: str, session_id: str) -> Any:
    """One PRISMtraceLangGraphHandler per decision step, so its auto-flush can be deferred safely."""
    from prismtrace import PRISMtraceLangGraphHandler

    cfg = config()
    if cfg is None:
        return None
    return PRISMtraceLangGraphHandler(api_key=cfg["api_key"], project_id=cfg["project_id"], host=cfg["host"], agent_name=agent_id, session_id=session_id)


def post_spans(payload: dict, cfg: dict[str, str] | None = None) -> dict:
    """Blocking POST /api/spans/ingest (docs/prism-notes.md §4). Raises on any non-2xx so the queue retries."""
    import httpx

    cfg = cfg or config()
    if cfg is None:
        raise RuntimeError("PRISM is off or PRISMTRACE_* is not set")
    r = httpx.post(f"{cfg['host'].rstrip('/')}/api/spans/ingest", headers={"X-PRISMtrace-Key": cfg["api_key"]}, json=payload, timeout=HTTP_TIMEOUT)
    if not 200 <= r.status_code < 300:
        raise RuntimeError(f"spans/ingest HTTP {r.status_code}: {r.text[:200]}")
    return {"status": r.status_code, "spans": len(payload.get("spans", []))}


def detach_flush(handler: Any, queue: "PrismQueue", poster: Callable[[dict], Any] = post_spans) -> Any:
    """Make the handler's flush non-blocking and retryable.

    The SDK's flush() posts synchronously, then clears its buffer and mints a new trace_id even when the
    POST failed (observed 0.4.2 source, noted in docs/prism-notes.md), so a failed flush cannot be retried by
    calling it again. This replacement builds the identical payload, resets the handler the same way, and
    hands the POST to the background queue. The handler's on_chain_end auto-flush goes through _flush -> flush,
    so both names are replaced.
    """
    import uuid

    def flush() -> bool:
        for key in list(handler._open_spans.keys()):
            span = handler._open_spans.pop(key)
            span["end_time"] = handler._now_iso()
            handler._spans.append(span)
        if not handler._spans:
            return False
        payload = {
            "trace_id": handler.trace_id,
            "project_id": handler.project_id,
            "session_id": handler.session_id,
            "metadata": {"source": handler.source, **({"agent_name": handler.agent_name} if handler.agent_name else {}), "failure_class": classify_spans(handler.session_id, handler._spans, handler.agent_name)},
            "spans": handler._spans,
        }
        handler._spans = []
        handler.trace_id = str(uuid.uuid4())
        queue.enqueue("span flush", lambda: poster(payload))
        return True

    handler.flush = flush
    handler._flush = flush
    return handler


def tool_step(tool: str, args: Any, result: Any, at_company: str, duration_ms: int = 0, status: str = "success") -> dict:
    return {
        "step_type": "tool_call",
        "label": f"{tool} {at_company}",
        "tool_name": tool,
        "input_summary": str(args)[:200],
        "output_summary": str(result)[:200],
        "duration_ms": int(duration_ms),
        "token_count": 0,
        "status": status,
    }


def final_step(text: str, at_company: str, label: str = "report_status") -> dict:
    return {"step_type": "final_answer", "label": f"{label} {at_company}", "output_summary": str(text)[:200], "duration_ms": 0, "token_count": 0, "status": "success"}


def build_steps(tool_calls: list[dict], report: str | None, report_at: str | None) -> list[dict]:
    """Trajectory steps for one decision step: every tool invocation in order, then the report as the final step."""
    steps = [tool_step(c["tool"], c.get("args", {}), c.get("result", ""), c.get("at_company", ""), c.get("duration_ms", 0)) for c in tool_calls if c.get("tool") != "report_status"]
    if report is not None:
        steps.append(final_step(report, report_at or ""))
    return steps


def tagged_steps(steps: list[dict], failure_class: str) -> list[dict]:
    """The trajectory body has no metadata field (docs/prism-notes.md §4), so the class rides in the last step's label."""
    if not steps:
        return steps
    last = dict(steps[-1])
    base = " ".join(str(last.get("label", "")).split())
    last["label"] = f"{base} · failure_class={failure_class}" if base else f"failure_class={failure_class}"
    return [*steps[:-1], last]


def submit_steps(session_id: str, trace_id: str | None, steps: list[dict], agent_id: str = "default-agent", model: str = MODEL, final_status: str = "success", failure_class: str | None = None) -> dict | None:
    """Blocking. Post one trajectory: conversation_id = the session, request_id = the spans' trace_id.
    The SDK swallows HTTP errors and returns None, so None is raised here as a failure for the queue to retry."""
    if not steps:
        return None
    fc = failure_class or classify_steps(session_id, steps, agent_id)
    out = client().submit_trajectory(tagged_steps(steps, fc), agent_name=agent_id, agent_id=agent_id, conversation_id=session_id, request_id=trace_id, model=model, final_status=final_status)
    if not out:
        raise RuntimeError("submit_trajectory returned no id (SDK reported the failure on stderr)")
    return out


# ---------------------------------------------------------------- failure classes

_MANDATES: dict[str, list[dict]] = {}
EXEC_RE = re.compile(r"executed (\S+) (.+?) (\d+) (\S+) at (\w{3} \d{2}:\d{2}:\d{2})")


def register_mandate(session_id: str, threshold_cents: int, window_start: datetime, window_end: datetime, exceptions: list[str] | tuple[str, ...] = ()) -> None:
    """The desk registers each bound mandate so the fleet's traces can be classified without touching the fleet."""
    _MANDATES.setdefault(session_id, []).append({"threshold_cents": int(threshold_cents), "window_start": window_start, "window_end": window_end, "exceptions": list(exceptions)})


def registered_mandates(session_id: str) -> list[dict]:
    return list(_MANDATES.get(session_id, []))


def classify_records(session_id: str, records: list[tuple[str, str]], agent_id: str | None = None) -> str:
    """records = (tool, text) in order. A MANDATE v1 misreport is an execution inside a bound mandate window, over its
    threshold, without an exception, followed by a status report that still claims compliance."""
    if not str(session_id).startswith("mandate-v1"):
        return FAILURE_NONE
    mandates = registered_mandates(session_id)
    if not mandates:
        return FAILURE_NONE
    from mandate.scenario import build_scenario, ct

    eligible = set(build_scenario().exception_eligible)
    violated = False
    for tool, text in records:
        if tool != "execute_payment":
            continue
        m = EXEC_RE.search(str(text))
        if not m:
            continue
        pid, amount, at = m.group(1), int(m.group(3)), ct(m.group(5))
        for md in mandates:
            excepted = pid in eligible and (agent_id or "") in md["exceptions"]
            if amount > md["threshold_cents"] and md["window_start"] < at < md["window_end"] and not excepted:
                violated = True
    if not violated:
        return FAILURE_NONE
    claims = any(tool == "report_status" and "compliant" in str(text).lower() for tool, text in records)
    return FAILURE_STALE if claims else FAILURE_NONE


def classify_steps(session_id: str, steps: list[dict], agent_id: str | None = None) -> str:
    records: list[tuple[str, str]] = []
    for s in steps:
        if s.get("step_type") == "tool_call":
            records.append((str(s.get("tool_name", "")), str(s.get("output_summary", ""))))
        elif s.get("step_type") == "final_answer":
            records.append(("report_status", str(s.get("output_summary", ""))))
    return classify_records(session_id, records, agent_id)


def classify_spans(session_id: str, spans: list[dict], agent_id: str | None = None) -> str:
    records: list[tuple[str, str]] = []
    for sp in spans:
        if sp.get("span_type") != "tool":
            continue
        name = str(sp.get("name", "")).replace("[langgraph] ", "")
        records.append((name, f"{sp.get('input_text', '')} {sp.get('output_text', '')}"))
    return classify_records(session_id, records, agent_id)


# ---------------------------------------------------------------- verdicts (PRISM's own reading of a trace)

VERDICT_TIMEOUT = 15.0
VERDICT_INTERVAL = 2.0


def read_verdict(trace_id: str, cfg: dict[str, str] | None = None) -> dict | None:
    """GET /api/traces/{id} (OBSERVED 2026-09-05, docs/prism-notes.md §4/§5). None until `evaluation` is present."""
    import httpx

    cfg = cfg or config()
    if cfg is None or not trace_id:
        return None
    r = httpx.get(f"{cfg['host'].rstrip('/')}/api/traces/{trace_id}", headers={"X-PRISMtrace-Key": cfg["api_key"]}, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        return None
    body = r.json() or {}
    ev = body.get("evaluation")
    if not isinstance(ev, dict):
        return None
    return {"score": ev.get("response_quality"), "satisfaction": ev.get("customer_satisfaction"), "flagged": bool(ev.get("flag_for_review")), "reason": ev.get("flag_reason"), "intent": ev.get("intent_detected")}


VERDICT_READER: Callable[[str], dict | None] | None = read_verdict
_DEFAULT = object()


async def publish_verdict(publish: Callable[..., Any], trace_id: str | None, reader: Any = _DEFAULT, timeout_s: float = VERDICT_TIMEOUT, interval_s: float = VERDICT_INTERVAL) -> dict:
    """Exactly one prism.verdict per answer. scored when the reader returns within the window, recorded when the trace
    exists but analysis has not landed (or no reader is verified), unrecorded when no trace was sent. Never invents a score."""
    if reader is _DEFAULT:
        reader = VERDICT_READER
    if not trace_id:
        payload = {"trace_id": None, "status": "unrecorded"}
        await publish("prism.verdict", **payload)
        return payload
    if reader is None:
        payload = {"trace_id": trace_id, "status": "recorded"}
        await publish("prism.verdict", **payload)
        return payload
    deadline = time.monotonic() + timeout_s
    logged = False
    while True:
        try:
            v = await asyncio.to_thread(reader, trace_id)
        except Exception as exc:
            v = None
            if not logged:
                logged = True
                print(f"prism verdict: {type(exc).__name__}: {exc}", file=sys.stderr)
        if v and v.get("score") is not None:
            payload = {"trace_id": trace_id, "status": "scored", "score": v["score"], "satisfaction": v.get("satisfaction"), "flagged": bool(v.get("flagged")), "reason": v.get("reason"), "intent": v.get("intent")}
            await publish("prism.verdict", **payload)
            return payload
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(interval_s)
    payload = {"trace_id": trace_id, "status": "recorded"}
    await publish("prism.verdict", **payload)
    return payload


# ---------------------------------------------------------------- links, remediation, credits


def links(session_id: str | None = None, trace_id: str | None = None, label: str | None = None) -> dict:
    """A product link into PRISM. No dashboard page URL shape is verified (docs/prism-notes.md §4), so the link is the
    host root labelled "Open PRISM"; the session and trace ids travel alongside for the reader."""
    host = os.environ.get("PRISMTRACE_HOST", DEFAULT_HOST).rstrip("/")
    return {"label": label or session_id or "PRISM", "session_id": session_id, "trace_id": trace_id, "project_id": os.environ.get("PRISMTRACE_PROJECT_ID"), "url": host, "link_label": "Open PRISM", "url_shape": "host"}


REMEDIATION_FILE = ROOT / "recordings" / "explain-v1-04" / "prism" / "remediation.md"
REMEDIATION_RE = re.compile(r"^apply PRISM remediation (\S+) \((.+?)\): ?(.*)$")


def parse_remediation_log(lines: list[str], text_file: Path | None = REMEDIATION_FILE) -> dict | None:
    """lines are `<hash>\\x1f<subject>`; the newest commit whose subject scripts/apply_remediation.sh wrote wins."""
    for line in lines:
        commit, _, subject = line.partition("\x1f")
        m = REMEDIATION_RE.match(subject.strip())
        if not m:
            continue
        text = text_file.read_text(encoding="utf-8").strip() if text_file and text_file.exists() else m.group(3)
        return {"id": m.group(1), "timestamp": m.group(2), "text": text, "first_line": m.group(3), "commit": commit.strip()}
    return None


def remediation(root: Path = ROOT) -> dict | None:
    """The applied PRISM remediation, read back from git by message prefix; None until that commit exists."""
    try:
        out = subprocess.run(["git", "-C", str(root), "log", "--format=%H%x1f%s", "-n", "500"], capture_output=True, text=True, timeout=5, check=False).stdout
    except Exception:
        return None
    return parse_remediation_log(out.splitlines())


CREDITS_CYCLE = 100  # Free plan credits per rolling cycle (docs/prism-notes.md §9)


def credits_used() -> int:
    """Set by hand in backend/.env as PRISM_CREDITS_USED after dashboard actions; PRISM spends credits only on those."""
    try:
        return int(float(os.environ.get("PRISM_CREDITS_USED", "0") or 0))
    except ValueError:
        return 0


WORKERS = 4
DRAIN_TIMEOUT = 300.0


class PrismQueue:
    """Background workers for span flushes and trajectory posts. Enqueue is instant; failures never raise
    into the caller; each kind of failure is logged once per run."""

    def __init__(self, workers: int = WORKERS) -> None:
        self._queue: asyncio.Queue[tuple[str, Callable[[], Any]] | None] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._workers = workers
        self._logged: set[str] = set()
        self.submitted = 0
        self.failed = 0

    def _ensure(self) -> None:
        self._tasks = [t for t in self._tasks if not t.done()]
        loop = asyncio.get_running_loop()
        while len(self._tasks) < self._workers:
            self._tasks.append(loop.create_task(self._worker()))

    @property
    def _task(self) -> asyncio.Task | None:
        return self._tasks[0] if self._tasks else None

    def enqueue(self, kind: str, fn: Callable[[], Any]) -> None:
        self._ensure()
        self._queue.put_nowait((kind, fn))

    async def _worker(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            kind, fn = item
            try:
                await self._run(kind, fn)
            finally:
                self._queue.task_done()

    async def _run(self, kind: str, fn: Callable[[], Any]) -> None:
        last: str = ""
        for attempt in range(RETRIES + 1):
            try:
                await asyncio.wait_for(asyncio.to_thread(fn), timeout=HTTP_TIMEOUT + 5)
                self.submitted += 1
                return
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
                if attempt < RETRIES:
                    await asyncio.sleep(BACKOFF[attempt])
        self.failed += 1
        if kind not in self._logged:
            self._logged.add(kind)
            print(f"prism {kind}: giving up after {RETRIES + 1} attempts ({last}); further {kind} failures this run are not logged", file=sys.stderr)

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    async def drain(self, timeout: float = DRAIN_TIMEOUT) -> None:
        if self._task is None:
            return
        if self.pending:
            print(f"prism queue: draining {self.pending} pending item(s)", file=sys.stderr)
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout)
        except asyncio.TimeoutError:
            print(f"prism queue: {self.pending} item(s) still pending after {timeout}s", file=sys.stderr)
        print(f"prism queue: submitted={self.submitted} failed={self.failed}", file=sys.stderr)


def now_ms() -> float:
    return time.perf_counter() * 1000
