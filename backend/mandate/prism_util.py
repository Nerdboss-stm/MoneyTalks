"""PRISM helpers shared by the fleet, the smoke and the M3 explain agents.

Facts used here come from docs/prism-notes.md: PRISMtrace(api_key, host, project_id, timeout) and
PRISMtrace.submit_trajectory(steps, agent_name, agent_id, conversation_id, request_id, model) (§4),
PRISMtraceLangGraphHandler(api_key, project_id, host, agent_name, session_id) with handler.trace_id and
flush() (§2, §4). Nothing here blocks the fleet loop: every network call goes through PrismQueue.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any, Callable

MODEL = "claude-haiku-4-5"
HTTP_TIMEOUT = 10
RETRIES = 2
BACKOFF = (0.5, 2.0)


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
            "metadata": {"source": handler.source, **({"agent_name": handler.agent_name} if handler.agent_name else {})},
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


def submit_steps(session_id: str, trace_id: str | None, steps: list[dict], agent_id: str = "default-agent", model: str = MODEL, final_status: str = "success") -> dict | None:
    """Blocking. Post one trajectory: conversation_id = the session, request_id = the spans' trace_id.
    The SDK swallows HTTP errors and returns None, so None is raised here as a failure for the queue to retry."""
    if not steps:
        return None
    out = client().submit_trajectory(steps, agent_name=agent_id, agent_id=agent_id, conversation_id=session_id, request_id=trace_id, model=model, final_status=final_status)
    if not out:
        raise RuntimeError("submit_trajectory returned no id (SDK reported the failure on stderr)")
    return out


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
