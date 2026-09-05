# MONEY OPERATIONS

Finance agents that may only say numbers a deterministic engine proved. Built for MONEY TALKS (Block Convey, New York, 5 September 2026).

## How this maps to the judging criteria

| Criterion | What the product does | Where to see it |
|---|---|---|
| Use of Hackathon Tools & Sponsor Technology | PRISM sessions explain-v1-04 and explain-v2-04 with tool trajectories, live verdict line, failure_class metadata, Root Cause, Remediation applied as a commit (none applied yet; `scripts/apply_remediation.sh` writes it and `/explain/prove` reads it back); GIDE session log; ElevenLabs Scribe and Flash TTS, twelve voices | `shots/prism/`, `docs/gide-session.jsonl`, `config/voices.json` |
| Financial Workflow Automation | Automates flux analysis, the variance commentary step of the month-end close: compares periods, ranks variances, drills to transaction drivers, produces cited commentary; MANDATE payments mode enforces a spoken standing order on cash outflows | `make meeting V=v2 N=4`; key `M` |
| AI Intelligence & Accuracy | Agents answer only from engine-computed evidence slices; verify.py rejects any figure not in the evidence before it is spoken; v1 freeform produced 42 unverifiable figures, v2 produced zero | `/explain/prove`, the red card in explain-v1-04 |
| Business Value & Decision Support | Days of spreadsheet flux analysis to minutes; the CFO cross-examines owners live; concentration insights ("3 customers, 64% of the increase"); context memory improves run over run | the transcript panel, `docs/context-memory.md` |
| Product Execution & User Experience | End to end live: hold-to-talk, twelve voices under 3 s, transcript with citations and verdicts, evidence drawer to transactions, Run 1 / Run 2 switch | the demo video |

## 1. Problem

Every month someone in finance explains why the numbers moved, reading two periods of summaries and transaction CSVs by hand. AI attempts write fluent paragraphs with figures that are not in the data. A confident wrong number in a finance meeting is the most expensive sentence there is.

## 2. Solution

Twelve voiced agents run the review as a meeting; each owns a slice of the books. A deterministic pandas engine compares the periods, ranks the variances, drills each to its driver transactions, and writes numbered evidence rows E1..En. An owner may only speak numbers that exist in its slice, each with its row citation, and a gate checks every answer before it is spoken. PRISM holds the independent record of each exchange, scores it, and clusters failures into root causes and remediation.

## 3. Key features

- Deterministic variance engine: compare, rank, drill, evidence rows E1..En with their transaction ids.
- Owner agents: twelve roles, each answering from its own evidence slice (`config/owners.yaml`).
- verify.py gate: figures, citations and driver coverage checked before any speech; a failing grounded answer is replaced.
- Product shell: ranked agenda, live transcript, evidence drawer down to transactions, hold-to-talk.
- PRISM: a session per run, a trajectory per answer, a live verdict line, failure classes, Root Cause, Remediation applied as a git commit.
- Context memory: one learned line per grounded run in `docs/context-memory.md`, read by the next run.
- Ingest: arbitrary transaction CSVs into the engine's shape.
- MANDATE payments mode: the standing-orders floor as the deepest evidence tier, down to a payment stamp and the sentence that allowed it.

## 4. Tech stack

Python 3.12, LangGraph, Claude Haiku 4.5, pandas, FastAPI, PRISM Builder (prismtrace-sdk 0.4.2), ElevenLabs Scribe and Flash TTS, PixiJS + GSAP, Vite/TypeScript, GIDE.

## 5. How it works

One CFO question, voice to voice:

1. Space held records; on release ElevenLabs Scribe transcribes (300–800 ms).
2. The Desk classifies the utterance: named owner, change question, payments query, order, or noise (400–700 ms).
3. The engine returns that owner's evidence slice (about 10 ms).
4. Claude Haiku 4.5 phrases a grounded answer from the slice and the memory, citing rows (700–1200 ms).
5. verify.py checks every figure, citation and the driver coverage; a failing grounded answer is replaced before speech.
6. The exchange goes to PRISM as a trace in the run's session, verifier result and failure class in its metadata; the tool steps follow as a trajectory.
7. ElevenLabs Flash TTS streams the owner's voice; the first chunk arrives in 75–200 ms.
8. Playback; the room prints the words, the transcript mirrors them, and PRISM's verdict is polled for 15 s onto the record line.

v1 (freeform) gives the model raw transaction rows to estimate from; it produces confident figures the data does not contain, marked by the gate after the fact. v2 (grounded) gives it only the evidence JSON and the memory, verifies before speech, and passes.

## 6. PRISM, honestly

verify.py gates before speech; PRISM is the independent record and auditor and blocked no sentence. Its automatic scoring is general-purpose: in explain-v1-04 verify.py failed five of six freeform answers, and PRISM flagged one of the six. So verify.py is the evaluator of record for figure-level checks; PRISM holds the record, the verdict, Root Cause, and Remediation.

The live verdict line reads a trace's evaluation from `GET /api/traces/{id}`, an endpoint observed in the dashboard's own requests, not in the documentation; `docs/prism-notes.md` separates observed from unverified. Dashboard page URLs are undocumented, so every "Open PRISM" link goes to the host.

Credits used come from `PRISM_CREDITS_USED` in `backend/.env`; it is unset at the time of writing, so `/explain/prove` reports 0.

Remediation is applied as a commit: `scripts/apply_remediation.sh` writes `apply PRISM remediation <id> (<time>): <first line of recordings/explain-v1-04/prism/remediation.md>`, which `/explain/prove` reads back. None has been applied yet: the file does not exist and `git log` has no such commit, so there is no text or hash to quote. The Remediation page holds every cluster for review (`shots/prism/05-remediation.png`).

## 7. Proof

`make explain-prove N=4`:

| | explain-v1-04 | explain-v2-04 |
|---|---:|---:|
| Turns | 6 | 6 |
| Unverifiable figures | 42 | 0 |
| Turns with an unverifiable figure | 5 | 0 |
| Driver coverage, median % | 0.0 | 100.0 |
| Pass rate % | 0.0 | 100.0 |
| Answers replaced by the gate | 0 | 0 |
| Memory lines learned | 0 | 1 |
| Median latency, addressed to answer, ms | 3590 | 3785 |
| Traces recorded in PRISM | 6 | 6 |
| Traces scored by PRISM within 15 s | 4 | 3 |
| Traces PRISM flagged | 1 | 0 |

`/explain/prove` also carries the MANDATE proof: mandate-v1-01 has 1 violation (Halden Logistics, $51,000, 65 s after the order bound) and 4 misreports; mandate-v2-01 has 0 violations, 1 escalation, the same payment held at the boundary at 10:03:14.

PRISM project `moneytalks-event`, links as returned by `GET /explain/links` (every URL is the host, labelled Open PRISM):

| Key | Session | Trace | Note |
|---|---|---|---|
| explain_v1 | explain-v1-01 | | freeform reference run |
| explain_v2 | explain-v2-01 | | grounded reference run |
| flagged_trace | explain-v1-01 | eb72827d-0801-45ec-994c-d62cde682aef | Procurement; figures verify.py failed |
| mandate_v1 | mandate-v1-01 | | payments, stale plan executes |
| mandate_v2 | mandate-v2-01 | | payments, boundary holds |

The scored event runs are explain-v1-04 and explain-v2-04; the trace PRISM itself flagged in explain-v1-04 is 8b1ced79 (Controller A, "What changed in August?").

Screenshots under `shots/prism/`:

| File | Shows |
|---|---|
| 01-v1-session.png | Sessions list with explain-v1-04 in view |
| 02-v1-flagged-trace.png | the flagged trace: quality 35/100, response quality 25/100 |
| 03-agent-run-tools.png | Agent Runs, a controller_a trajectory with its tool steps |
| 04-root-cause.png | Root Cause clusters |
| 05-remediation.png | Remediation table, clusters held for review |
| 06-v2-session.png | Sessions list with explain-v2-04 in view |

## 8. How to run

```bash
make setup                 # uv sync, npm install
make dev-backend           # FastAPI on :8000
make dev-frontend          # Vite on :5173, the app boots into the meeting
make explain-data          # seeded two-month ledger into shared/explain
make meeting V=v2 N=4      # headless meeting, recorded to recordings/explain-v2-04.jsonl
make explain-prove N=4     # the proof table above
make ingest FILES="a.csv b.csv" OUT=shared/explain/live
make run V=v1 N=1 SPEED=0  # a MANDATE payments run
make replay S=mandate-v1-01
make prove
make test
```

`backend/.env` keys: `PRISMTRACE_HOST`, `PRISMTRACE_PROJECT_ID`, `PRISMTRACE_API_KEY`, `ANTHROPIC_API_KEY`, `elevenlabs_api_key`, `PRISM_CREDITS_USED`. Optional: `MANDATE_DECIDER=rules|llm`, `PRISM_HANDLERS=on|off`, `EXPLAIN_LLM=on|off`.

Keys in the app: `M` switches between the meeting and the payments floor; `Space` held is push-to-talk; `P` opens Prove; `K` opens the record; `1` and `2` select run 1 (freeform) and run 2 (grounded); `L` loads the meeting; `V` focuses the typed question; `Esc` closes.

## 9. Demo video

Demo video: DEMO_VIDEO_URL

## 10. Disclosure

Agent framework, voice pipeline, and visualization shell were built before the event and are disclosed here. The variance engine's scored runs, all PRISM sessions in project moneytalks-event, Root Cause, Remediation, and proof artifacts were created during the event.

## 11. Tools

- PRISM: sessions, trajectories, per-trace verdicts, Root Cause, Remediation; every fact used is in `docs/prism-notes.md`.
- GIDE: `docs/gide-session.jsonl`, `docs/gide.png`.
- ElevenLabs: Scribe for speech to text, Flash v2.5 for each owner's voice (`config/voices.json`).
