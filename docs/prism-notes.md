# PRISM notes

Source of truth for every PRISM fact in this repo. Quoted text is verbatim from https://blockconvey.com/docs (https://blockconvey.com/prismtrace/docs 302-redirects to it), the linked product pages, and the installed `prismtrace-sdk==0.4.2` source. Anything not found is marked UNVERIFIED. Lines tagged `[SDK src]` come from reading the package source, not the docs.

Fetched 2026-09-04: `/docs`, `/prism`, `/prism/ai-observability`, `/prism/ai-remediation`, `/prism/agent-intelligence`, `/prism/evaluators`, `/prism/synthetic-scenarios`, `/pricing`, `/integrations`, `/resources/guides` (all twelve guides "Coming soon"), `/resources/glossary` ("Definitions are being published").

## 1 Install

- Distribution / import: "The distribution is `prismtrace-sdk`; the import package is `prismtrace`. Install one, import the other."
- `pip install "prismtrace-sdk>=0.4.0"`  — installed here: `prismtrace-sdk==0.4.2` (in `backend/pyproject.toml`).
- Exported symbols (docs "Symbols that exist" table): `PRISMtrace` ("Manual client. Has `trace_llm`, `submit_trajectory` and a `trace` decorator."), `PRISMtraceCallbackHandler` (LangChain), `PRISMtraceLangGraphHandler`, `wrap_langgraph` (LangGraph), `PRISMtraceADKAdapter`, `install_litellm`, `install_openai_agents`, `ClaudeAgentTracer` (Anthropic Messages API), `PRISMtraceVoiceTracer` (ElevenLabs).
- `[SDK src]` `prismtrace/__init__.py` additionally exports `PRISMtraceTracingProcessor`, `install_elevenlabs_voice`. `wrap_langgraph` is an alias of `langgraph_helper.wrap_graph`.
- Docs "Symbols that do not exist": `PRISMtraceLangchainCallback`, any npm package, `monitor()`, `wrap_bedrock()`, a `blockconvey` module, `prism-sdk`/`prismsdk`, "An OpenTelemetry ingest endpoint. `/api/otlp/v1/traces` is not served."
- `python -m prismtrace.verify` — "does the same thing" as `GET /api/setup-doctor`; prints `CREDENTIAL OK|FAIL` and `LIVE CONNECTED|WAITING FOR LIVE`.

## 2 Auth

- Env vars (docs `.env` block):
  ```
  PRISMTRACE_HOST=https://prism.blockconvey.com
  PRISMTRACE_PROJECT_ID=00000000-0000-0000-0000-000000000000
  PRISMTRACE_API_KEY=pt-sk-...
  ```
- "Take the project ID from **Settings → Project**, and create a key on the **API keys** page." "A key is shown once."
- Header: "Machine callers authenticate with the `X-PRISMtrace-Key` header. This is not a bearer token: `Authorization: Bearer` carries dashboard sessions and will be rejected for an API key."
- Base URL: "API base URL `https://prism.blockconvey.com`". `PRISMTRACE_HOST` is "Fixed. This is the only public host."
- `[SDK src]` `_config.DEFAULT_HOST = "https://api.prism.blockconvey.com"`; resolution order: explicit `host=`/`endpoint=` arg → `PRISMTRACE_HOST` → `PRISMTRACE_ENDPOINT` → default. This differs from the docs host, so always set `PRISMTRACE_HOST` explicitly.
- Handler constructor args (docs example): `api_key`, `project_id`, `host`, `agent_name`, `session_id`. `[SDK src]` full signature: `PRISMtraceCallbackHandler(api_key=None, project_id=None, endpoint=None, host=None, session_id=None, agent_name=None, source="langchain")`; `api_key`/`project_id` fall back to `PRISMTRACE_API_KEY`/`PRISMTRACE_PROJECT_ID`. There is **no `agent_id` and no `metadata` constructor argument**.
- Manual client: `PRISMtrace(api_key, host, project_id, timeout=10)` `[SDK src]`.
- Key scopes: `ingest` "Sending traces and spans. This is what your application should carry."; `read` "Reading traces, analyses, and balances back out."; `operate` "Triggering actions that spend credits." "A read-only key returns 403 on ingest by design."
- Deprecated: "An `api_key` body field is still accepted ... and it is deprecated."

## 3 Traces

- Endpoint: `POST https://prism.blockconvey.com/api/traces`. "Everything starts from one HTTP call."
- Required fields: "`project_id`, `model`, `input_messages`, `output_message`, `latency_ms`". `latency_ms`: "Send 0 if you are not measuring it." `input_messages`: "Objects of `{role, content}`."
- Optional: `session_id`, `agent_id`, `agent_name`, `user_identifier`, `trace_id` ("Re-sending the same one returns the existing trace instead of duplicating it."), `token_count_input`, `token_count_output` ("Defaults to 0. Used for cost."), `metadata` ("Free-form. Filterable in the dashboard.").
- Returns: "`200` with the stored trace: its `id`, the resolved `cost_usd`, and the `session_id` it was filed under."
- Minimal call (docs curl):
  ```bash
  curl -sS -X POST "$PRISMTRACE_HOST/api/traces" \
    -H "Content-Type: application/json" \
    -H "X-PRISMtrace-Key: $PRISMTRACE_API_KEY" \
    -d '{"project_id":"'"$PRISMTRACE_PROJECT_ID"'","model":"claude-sonnet-4-6",
         "input_messages":[{"role":"user","content":"What loan rates do you offer?"}],
         "output_message":"Our personal loan rates currently start at 7.9% APR.",
         "latency_ms":420,"session_id":"conversation-1","agent_id":"loan-assistant"}'
  ```
- `agent_id`: "Stable agent identifier. Matched by Model Inventory and agent-scoped alert rules." "Sending a stable `agent_id` is what stops one agent splitting into several entries."
- `session_id`: "Groups traces into one conversation or run. Without it, traces never assemble into a session." "Send a session_id from day one ... it cannot be backfilled onto traces you have already sent."
- `metadata`: "Free-form. Filterable in the dashboard." "`session_id`, `user_identifier`, `agent_id`, and `agent_name` are also read from inside `metadata` for older integrations. The top-level form is preferred and wins if both are present."
- Session granularity recommendation: "Session — Traces that share a `session_id`, assembled into one conversation. Send the same value for every turn of a conversation." Docs LangChain example comment: `session_id="conversation-1",   # one value per conversation`. LangGraph example uses `session_id="graph-run-1"`. Setup-doctor stage "Trace normalized — Traces carry a shared session_id and have been assembled into runs."
- Plain conversational trace into an existing session: `POST /api/traces` with the same `session_id`, `input_messages=[{"role":"user","content":<input text>}]`, `output_message=<output text>`, `model`, `latency_ms`, plus `agent_id` and `metadata`. This is the call `backend/scripts/prism_smoke.py` step (a) uses.
- SDK alternative `[SDK src]`: `PRISMtrace.trace_llm(model, input_messages, output, latency_ms, token_count_input=0, token_count_output=0, trace_id=None, agent_id=None, agent_name=None, metadata=None) -> None`. It has **no `session_id` parameter** and posts from a background thread, returning nothing; the session would have to travel in `metadata={"session_id": ...}` (docs: read from metadata for older integrations). Use HTTP when the returned `id` is needed.
- Rate limit: "Ingest allows 200 requests per minute." Delivery: "If you call the HTTP endpoint yourself in production, do it from a thread, an asyncio task, or a queue so trace delivery never adds latency to your agent."

## 4 Agent Runs and spans

- Definitions: "Agent run — The trajectory inside a single run: each model call, tool call, retrieval, and handoff. Produced automatically from spans when you use the SDK." "Span — One step inside a run. The framework handlers emit these; the HTTP endpoint does not."
- What makes a run appear under Agent Runs: spans, not `/api/traces`. "**Agent Runs** shows the trajectory inside a single run: which model was called, which tools fired, where a handoff happened, and where it failed." "Success for LangChain is a real application invocation that flushes spans. A curl to `/api/traces` proves the credential only; it does not attach callbacks."
- LangGraph handler name and attachment (docs, verbatim):
  ```python
  import os
  from prismtrace import PRISMtraceLangGraphHandler, wrap_langgraph

  handler = PRISMtraceLangGraphHandler(
      api_key=os.environ["PRISMTRACE_API_KEY"],
      project_id=os.environ["PRISMTRACE_PROJECT_ID"],
      host=os.environ["PRISMTRACE_HOST"],
      agent_name="my-graph",
      session_id="graph-run-1",
  )

  graph = wrap_langgraph(compiled_graph, handler)
  graph.invoke({"messages": [("user", "hello")]})
  handler.flush()
  ```
  "`wrap_langgraph` injects the callbacks on invoke and stream, so you do not repeat the config at every call site." "Success is one real `invoke` or `stream` that flushes spans."
- `[SDK src]` `wrap_graph` patches `invoke`/`ainvoke`/`stream`/`astream` on the compiled graph in place, appending the handler to `config["callbacks"]`. Passing `config={"callbacks": [handler]}` directly is the documented-in-source alternative. `PRISMtraceLangGraphHandler` is a subclass of `PRISMtraceCallbackHandler` with `source="langgraph"` and every span name prefixed `"[langgraph] "`.
- How tool calls become spans `[SDK src]`: the handler implements `on_tool_start`/`on_tool_end`/`on_tool_error` → span `span_type="tool"`, `name` = tool name, `parent_span_id` = the enclosing chain/node run id. Chains/nodes → `span_type="chain"`; chat models → `span_type="llm"` with `model`; retrievers → `"retrieval"`; `on_agent_action` → `"agent"`. A tool call inside a node is only seen if the tool is invoked with the run's config (`tool.invoke(args, config)`), which is how LangChain propagates callbacks.
- Flush `[SDK src]`: the handler auto-flushes when the outermost chain ends (`_chain_depth <= 0`) and at interpreter exit; `flush()` returns `True` on 2xx with ≥1 span, `False` on an empty buffer. Each flush posts one `trace_id` (uuid minted by the handler, exposed as `handler.trace_id`, regenerated after every flush — read it **before** `invoke`).
- Manual span API: `POST /api/spans/ingest` — "Used by the SDK handlers, not usually by you directly. Body is `{trace_id, project_id, spans[], session_id?, metadata?}`. Each span carries `name`, `span_type`, `start_time`, and optionally `span_id`, `parent_span_id`, `input_text`, `output_text`, `end_time`, `duration_ms`, `status`, `error_message`, `token_count_input`, `token_count_output`, `cost_usd`, `model`." `[SDK src]` the handler sends `metadata={"source": <source>, "agent_name": <agent_name>}` and per-span `metadata` dicts.
- Trajectory API `[SDK src]`: `PRISMtrace.submit_trajectory(steps, *, agent_name="default-agent", agent_id=None, conversation_id=None, request_id=None, model=None, final_status="success", async_send=False)` → `POST /api/trajectories`; steps carry `step_type` (`"reasoning" | "tool_call" | "final_answer"`), `label`, `output_summary`, `tool_name`, `input_summary`, `duration_ms`, `token_count`, `status`. Docs only name `submit_trajectory`; its body shape is not on the docs page.
- OBSERVED 2026-09-04 (smoke, dashboard): spans from `PRISMtraceLangGraphHandler` alone rendered in **Agent Runs** as one run "smoke · langgraph · 4 steps" whose graph was `Request received → smoke — claude-haiku-4-5 (the plain /api/traces trace) → smoke — langgraph (the whole span batch as one LLM step) → Response delivered`, with **TOOLS 0** — the `[langgraph] read_cash` tool span was not broken out. The run id shown (`eec2c25c…`) was neither the handler `trace_id` nor the trace id; PRISM assembled it from the session. `GET /api/setup-doctor` reported "1 trajectory assembled".
- Fix `[SDK src claude_tracer.py]`: the SDK's own `ClaudeAgentTracer` "Emits both spans (for the trace detail view) AND a trajectory (for PRISM evaluation + trajectory analytics)" — every tool call becomes a trajectory step `{"step_type": "tool_call", "label": <tool>, "tool_name": <tool>, "input_summary", "output_summary", "duration_ms", "token_count", "status": "success"|"error"}` posted to `POST /api/trajectories` with `conversation_id` = session id and `request_id` = the spans' `trace_id`. So for LangGraph: keep the handler for spans **and** call `PRISMtrace(api_key, host, project_id).submit_trajectory(steps, agent_name=role, agent_id=role, conversation_id=session_id, request_id=handler_trace_id, model=...)` after each run. `submit_trajectory` returns `{"id", "step_count", "created_at"}` (observed: `step_count: 2`). `backend/scripts/prism_smoke.py` step (b2) does this.
- `[SDK src 0.4.2, observed via inspect.getsource]` `PRISMtraceCallbackHandler.flush()`: closes every entry in `self._open_spans` (sets `end_time` via `self._now_iso()`), returns `False` on an empty buffer, otherwise POSTs `{"trace_id": self.trace_id, "project_id": self.project_id, "session_id": self.session_id, "metadata": {"source": self.source, "agent_name": self.agent_name}, "spans": self._spans}` to `{self.endpoint}/api/spans/ingest` with its own client, prints `PRISMtrace flush error: …` on exception or non-2xx, **then clears `self._spans` and mints a new `self.trace_id` regardless of success** and returns the boolean. `_flush()` is a backward-compatible alias that just calls `flush()`; `on_chain_end` calls `_flush()` when the chain depth returns to 0. Consequence: a failed SDK flush is unrecoverable by calling it again. `PRISMtrace.submit_trajectory` likewise catches HTTP errors, prints `PRISMtrace warning: /api/trajectories failed: …` and returns `None` (observed 2026-09-05).
- Fleet handling (`mandate/prism_util.py`, `agents.py`): one handler **per decision step** (never reused), `trace_id` captured before invoke; `detach_flush()` replaces that instance's `flush`/`_flush` with a function that builds the identical payload, resets the buffer/trace_id the same way, and enqueues a `POST /api/spans/ingest` (header `X-PRISMtrace-Key`, 10 s timeout) on `PrismQueue` — 4 workers, two retries with 0.5 s / 2 s backoff, each failure kind logged once per run, drained for up to 300 s at run end. `submit_steps` raises when the SDK returns `None` so trajectories retry the same way. A retry after a client-side timeout can duplicate a trajectory the server had already accepted (observed: ~112 assembled for ~96 submitted in `mandate-v2-03`).
- Fleet trajectories: one `submit_trajectory` per decision step (and one per executor fire) via `mandate.prism_util.submit_steps(session_id, trace_id, steps, agent_id, model)` — steps are every tool invocation of that step in order (`step_type: "tool_call"`, `label: "<tool> <company time>"`, `tool_name`, `input_summary`, `output_summary`, `duration_ms`, `token_count: 0`, `status`) and the report as the last `final_answer` step. `conversation_id` = the run's session id, `request_id` = that step's handler `trace_id`.
- CONFIRMED 2026-09-04 (dashboard after the fix): the trajectory appears in **Agent Runs** as its own run, id = the trajectory id (`c84c5027…`), labelled `smoke · claude-haiku-4-5 · Success`, **TOOLS 1**, graph `TOOLS read_cash → OUTPUT report`. The session-assembled `langgraph` run (`eec2c25c…`) kept TOOLS 0 and grew to 6 steps as more traces/span batches arrived in the session. Consequence for the fleet: one trajectory per agent run (tick decision or executor fire), `conversation_id` = PRISM session id, `agent_id` = role, `request_id` = the handler `trace_id` captured before invoke; spans stay on for the trace-detail view.
- UNVERIFIED: how to set `agent_id` on a LangGraph run via the handler alone (the trajectory path above carries `agent_id` explicitly). The handler exposes only `agent_name`; the spans-ingest body documents `metadata?` but not which keys it honours. UNVERIFIED whether ingest-level `metadata` keys (`company_day_id`, `mandate_id`, `run_version`, `tick`, `seed`) are filterable for runs the way `/api/traces` `metadata` is.
- UNVERIFIED: a read endpoint for runs or spans (`GET /api/traces/{id}`, `GET /api/sessions`, `GET /api/runs` are not on the docs page).

## 5 Automatic scoring

- "Every trace is analysed on arrival. This costs no credits and happens whether or not you ask for it."
- Scores: **Customer satisfaction** 0–100 "How satisfied the user would likely feel with this response. Alert rules read this as compliance_score."; **Response quality** 0–100 "Accuracy, helpfulness, and clarity of the reply."; **Intent detected** label; **Flagged for review** boolean.
- Flag trigger: "Set when either score is below 40, the reply looks like a hallucination, the user seems very frustrated, or the conversation contradicts itself. Carries a one-line reason."
- Industry checks: "rate promises and regulated-activity flags for financial conversations, PHI exposure and unsafe medical advice for healthcare, and privilege risk for legal."
- Scoring latency (documented statements, no number given): Troubleshooting "Scores are missing on recent traces — Analysis lags ingest. — Wait. This is not a setup failure and costs no credits." Failure modes: "Scores missing on new traces — Scoring lags ingest — Wait. Not a setup failure and not billed." Setup doctor: "Analysis ready — Automatic scoring has caught up. Lagging here is normal and is not a setup failure." `blocked_step: analysis_ready` — "Traces arrived; scoring is still catching up."
- OBSERVED 2026-09-04: after `POST /api/traces`, `GET /api/setup-doctor` reported "Analysis ready … live trace analysed" at the first poll, **6 s** after send (both smoke runs). No per-trace score endpoint is documented; the doctor is the only API read of scoring state.
- Read path for scores: dashboard ("Click any trace for the full conversation, the automatic analysis ..."). Project-level: `GET /api/setup-doctor?project_id=…` returns `live_connected`, per-stage `steps[]` (`title`, `status` in `done|pending`, `detail`) and `blocked_step` `[SDK src verify.py]`. UNVERIFIED: any per-trace score read endpoint.
- Re-run: "Re-run analysis on a trace, including each trace in a back-fill — 1 per trace".

## 6 Root Cause

- "**Root Cause** clusters failing traces into recurring problems and traces each back toward the prompt or code responsible, rather than leaving you to read failures one at a time."
- Input: failing traces in a project (scored/flagged traces). Product page (`/prism/agent-intelligence`): "Group runs that share the same cause"; classifier categories shown: "Knowledge gap / missing content → KB update", "Workflow / tool failure → Code PR", "Prompt logic → Prompt version", "Retrieval quality → Retrieval config", "Guardrail / policy config → Guardrail rule".
- Credit cost: "Root-cause engine run over a project — 5". "Root-cause fix attempt: patch, replay, and pull request — 5".
- Trigger: "Credits are spent only on actions a human deliberately triggers in the dashboard." "Before an action runs, PRISM checks affordability and quotes the price rather than starting something it cannot finish." Pricing page: "Before every AI-powered action, PRISM shows Action / Credit cost / Current balance / Expected balance after completion." UNVERIFIED: an API endpoint to trigger it (the `operate` scope exists; no route is documented).
- Output shape: UNVERIFIED as an API object. Product page shows per item: pattern name, session count, span/evidence line, root-cause category, fix layer, proposed change ("Detected / Diagnosed / Approved / Validated").
- Availability: "root cause, remediation ... all work on Free."

## 7 Remediation

- "**Remediation** turns those clusters into concrete recommendations." "Recommendations may cover prompts, retrieval, Knowledge Base content, guardrails, workflow state, tool use, and the validation plan."
- Credit cost: "Remediation recommendation — 2". "Prompt improvement suggestion — 2".
- Does PRISM apply changes? Exact language (`/prism/ai-remediation`): "Review suggested prompt, retrieval, Knowledge Base, guardrail, or workflow changes. Your team decides what to apply". "PRISM finds the failure and proposes the fix. A human decides what ships." "Code — PRISM drafts the change and the evidence. You open the pull request. Nothing merges without you." "Does AI Remediation edit my code? Not by default." Product boundary: "It does not generate autonomous pull requests, make unattended repository changes, merge automatically, deploy automatically, or perform autonomous production remediation." (`/prism/ai-observability`): "PRISM never merges on its own."
- Validation: "A fix is only Validated when the target metric improves in production over a minimum seven-day window, measured against the same cohort definition used to detect the failure."
- Agent Intelligence: "summarising a window of traffic in prose and naming the failure themes inside it." Cost "1, once per project per day".
- Evaluators (Builder only): "Run an evaluator task, or an experiment — 5". "Deterministic — same input, same verdict, no model in the loop" / "Judged — a model reads the exchange, sampled and spot-checked". "Send uncertain or important examples to the review queue". "Free shows core scores and findings. Builder adds your own evaluators."

## 8 Alerts

- "A rule is a metric, a condition, a threshold, and a severity, optionally scoped to one agent. Give it a notification email and PRISM writes there when the rule fires." "Alerts are available on Free and Builder alike."
- Metrics: `latency` "Response time of the trace, in milliseconds" (typical "Above 5000"); `compliance_score` "The customer-satisfaction score from automatic analysis" ("Below 60"); `error_rate` "Percentage of traces in the trailing hour with no output" ("Above 5"); `traces_per_hour` ("Below 1, to catch an agent that has gone silent"); `guardrail_blocks` ("Above 10"); `evaluation_failed` "Traces automatic analysis could not evaluate" ("Above 0").
- "A rule scoped to an `agent_id` no trace has ever carried can never fire. PRISM refuses it at creation and lists the agent IDs it does know about."
- "Fired alerts can be resolved or dismissed, and repeated firings are grouped into patterns."
- UNVERIFIED: an API to create alert rules (dashboard only in docs).

## 9 Limits

- Ingest rate: "Rate limited. Ingest allows 200 requests per minute." (`429`).
- Plan table (docs): Traces per month Free 25,000 / Builder 250,000 ("Counted, warned about at 80% and 100%. Traces are still stored."); Distinct models 5 / 20 ("Counted and warned about. Traces are still stored."); Data retention 14 days / 90 days ("Older traces stop appearing in reads. Nothing is refused on write."); Team members 1 / 5; Knowledge Base docs 50 / 500; Projects Unlimited; Credits per cycle 100 / 500.
- Over a ceiling: response "carries an `X-PRISMtrace-Plan-Warning` header naming the limit."
- Free vs Builder: "Free is the complete core product, not a trial. Traces, sessions, agent runs, automatic scoring, alerts, root cause, remediation, Knowledge Base, Model Inventory, connectors, and export all work on Free. Builder raises the ceilings and adds three capabilities: **Guardrails**, the **Evaluators Hub** with its annotation queues, and the **expanded Model Inventory**." Docs: "Free and Builder are the only plans." Pricing page additionally lists Builder $20/mo 500 credits, Growth $50/mo 1,500 credits, Managed $300/mo 10,000 credits, Enterprise — the docs and pricing pages disagree on plan count.
- Credits: "Your plan grants an allowance once per **rolling 30-day cycle**, anchored to the date your organization was created". "New organizations also get a one-time welcome grant of 100 credits, and top-up packs of 100 can be bought at any time." Pricing page: "Minimum planned top-up: 100 credits for $10". "Unused allowance carries over up to one full allowance".
- Not charged: "Trace ingest, viewing traces, the automatic per-trace analysis and its scores, trajectory assembly, regulatory classification, Knowledge Base processing, guardrail checks, dataset and evidence exports". "At a zero balance, new AI actions pause. Ingest, reads, automatic scoring, guardrails and alerts all continue."
- Usage endpoints: `GET /api/credits/summary`, `GET /api/credits/ledger`, `GET /api/credits/catalog`, `GET /api/entitlements`, `GET /api/entitlements/usage`.
- Diagnostics: `POST /api/setup-doctor/handshake` body `{project_id, send_test_trace}`; `GET /api/setup-doctor?project_id=`. "Handshake-only is never `LIVE CONNECTED`."
- Voice: "Each ElevenLabs call becomes one trajectory". "Enable exactly one path" (webhook `post_call_transcription` or `PRISMtraceVoiceTracer`). "audio is never sent to or stored by PRISM."
