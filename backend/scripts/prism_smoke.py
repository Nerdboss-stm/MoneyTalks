import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from prismtrace import PRISMtraceLangGraphHandler, wrap_langgraph
from typing_extensions import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

HOST = os.environ.get("PRISMTRACE_HOST", "").rstrip("/")
PROJECT_ID = os.environ.get("PRISMTRACE_PROJECT_ID", "")
API_KEY = os.environ.get("PRISMTRACE_API_KEY", "")
if not (HOST and PROJECT_ID and API_KEY):
    print("missing PRISMTRACE_HOST / PRISMTRACE_PROJECT_ID / PRISMTRACE_API_KEY", file=sys.stderr)
    sys.exit(2)

SESSION_ID = "smoke-01"
AGENT_ID = "smoke"
MODEL = "claude-haiku-4-5"
METADATA = {"run_version": "smoke", "channel": "voice"}
HEADERS = {"X-PRISMtrace-Key": API_KEY, "Content-Type": "application/json"}

http = httpx.Client(base_url=HOST, headers=HEADERS, timeout=30)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# (a) plain trace ------------------------------------------------------------
sent_at = time.monotonic()
sent_wall = now()
resp = http.post(
    "/api/traces",
    json={
        "project_id": PROJECT_ID,
        "trace_id": "smoke-01-a",
        "model": MODEL,
        "input_messages": [{"role": "user", "content": "Smoke, what is your cash position?"}],
        "output_message": "Cash position is 1,250,000.00 USD per my record.",
        "latency_ms": 0,
        "session_id": SESSION_ID,
        "agent_id": AGENT_ID,
        "agent_name": "smoke",
        "metadata": METADATA,
    },
)
print(f"(a) POST /api/traces -> {resp.status_code} at {sent_wall}")
for h in ("X-PRISMtrace-Plan-Warning", "X-PRISMtrace-Deprecation"):
    if h in resp.headers:
        print(f"    header {h}: {resp.headers[h]}")
resp.raise_for_status()
trace_a = resp.json()
print(f"    id={trace_a.get('id')} session_id={trace_a.get('session_id')} cost_usd={trace_a.get('cost_usd')}")


# (b) two-node LangGraph with one tool ----------------------------------------
tool_calls: list[dict] = []


@tool
def read_cash() -> int:
    """Return the current cash balance in cents."""
    t0 = time.perf_counter()
    value = 125_000_000
    tool_calls.append({"tool_name": "read_cash", "input": "{}", "output": str(value), "duration_ms": int((time.perf_counter() - t0) * 1000)})
    return value


class State(TypedDict, total=False):
    cash_cents: int
    report: str


def read_node(state: State, config: RunnableConfig) -> State:
    return {"cash_cents": read_cash.invoke({}, config)}


def report_node(state: State) -> State:
    return {"report": f"cash={state['cash_cents'] / 100:,.2f} USD"}


builder = StateGraph(State)
builder.add_node("read", read_node)
builder.add_node("report", report_node)
builder.add_edge(START, "read")
builder.add_edge("read", "report")
builder.add_edge("report", END)
compiled = builder.compile()

handler = PRISMtraceLangGraphHandler(
    api_key=API_KEY,
    project_id=PROJECT_ID,
    host=HOST,
    agent_name=AGENT_ID,
    session_id=SESSION_ID,
)
run_trace_id = handler.trace_id
graph = wrap_langgraph(compiled, handler)
out = graph.invoke({})
flushed = handler.flush()
print(f"(b) graph.invoke -> {out}")
print(
    f"    handler.session_id={handler.session_id} handler.trace_id(before invoke)={run_trace_id} "
    f"flush()={flushed} (False = buffer already auto-flushed when the outermost chain ended)"
)

# (b2) trajectory for the run: spans alone render in Agent Runs as one LLM step with TOOLS 0;
# the SDK's ClaudeAgentTracer emits spans AND a trajectory, so do the same (docs/prism-notes.md §4).
from mandate import prism_util  # noqa: E402

steps = [prism_util.tool_step(c["tool_name"], c["input"], c["output"], "", c["duration_ms"]) for c in tool_calls] + [prism_util.final_step(out["report"], "", "report")]
traj = prism_util.submit_steps(SESSION_ID, run_trace_id, steps, agent_id=AGENT_ID, model=MODEL)
print(f"(b2) POST /api/trajectories -> {traj}")

# (c) ids ----------------------------------------------------------------------
print("(c) ids")
print(f"    trace (a) id        : {trace_a.get('id')}  (client trace_id smoke-01-a)")
print(f"    trace (a) session_id: {trace_a.get('session_id')}")
print(f"    run (b) trace_id    : {run_trace_id}")
print(f"    run (b) session_id  : {SESSION_ID}")
print(f"    trajectory (b2) id  : {(traj or {}).get('id')}")

# (d) scoring status -------------------------------------------------------------
print("(d) polling GET /api/setup-doctor every 15s for up to 6 min (project-level; no per-trace score read endpoint is documented)")
deadline = time.monotonic() + 6 * 60
last_steps = None
while True:
    d = http.get("/api/setup-doctor", params={"project_id": PROJECT_ID})
    body = d.json() if d.status_code == 200 else {}
    steps = {s.get("title"): (s.get("status"), s.get("detail")) for s in body.get("steps", [])}
    elapsed = int(time.monotonic() - sent_at)
    if steps != last_steps:
        print(f"    t+{elapsed}s live_connected={body.get('live_connected')} blocked_step={body.get('blocked_step')} overall={body.get('overall')}")
        for title, (status, detail) in steps.items():
            print(f"        [{status}] {title}: {detail}")
        last_steps = steps
    else:
        print(f"    t+{elapsed}s unchanged")
    analysis = [v for k, v in steps.items() if "nalysis" in str(k)]
    if body.get("live_connected") and analysis and analysis[0][0] == "done":
        break
    if time.monotonic() >= deadline:
        break
    time.sleep(15)

print()
print("manual check: open https://prism.blockconvey.com -> Traces -> filter session_id=smoke-01 ->")
print(f"  trace id {trace_a.get('id')} should show Customer satisfaction / Response quality / Intent detected.")
print("  Agent Runs -> the [langgraph] run should list a child span named read_cash with span_type tool.")
print(f"trace (a) was sent at {sent_wall}. Press Enter when the scores are visible on trace (a).")
try:
    input()
    print(f"scores confirmed at {now()}, {int(time.monotonic() - sent_at)}s after send")
except EOFError:
    print("no stdin; elapsed not recorded")
