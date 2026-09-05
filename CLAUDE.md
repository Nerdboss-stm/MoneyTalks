# MANDATE

Standing orders for AI finance agent fleets, with proof. Build for MONEY TALKS (Block Convey, NYC, Sep 5 2026). PRISM and GIDE are mandatory. ElevenLabs is an official partner. Rubric: Build → Observe → Improve → Prove → Demo. Saturday: build 11:00, submissions lock 15:30, demos 16:00.

## Working rules for the assistant — these override your defaults
- NEVER use the Task tool. NEVER spawn subagents. No review agents, no verification agents, no parallel exploration. All work happens in this session, by you, directly.
- Do not enter plan mode. Do not write a plan. Start editing files immediately.
- Open ONLY the files the prompt names, plus files you create.
- Run the test suite ONCE, at the end of the prompt.
- No README files, docs, explanatory comments, or extra tests unless the prompt asks.
- Do not refactor earlier work unless told to.
- When you would offer options, pick one and implement it.
- When a PRISM fact is missing from docs/prism-notes.md, write UNVERIFIED and stop. Never invent an SDK call.

## What this is
Twelve LangGraph finance agents hold five-day payment plans on a shared ledger. A human speaks. A Desk classifies the utterance (MANDATE, RELEASE, QUERY, AGENT_QUERY, NOISE). A MANDATE is compiled into a structured constraint, read back, bound on "confirm". Propagation reaches into plans already in flight. v1 injects the mandate into agent context; one agent whose plan formed before the order executes a $51,000 payment 67 seconds later without re-checking, then reports compliance. Every agent can be addressed by name and answers aloud in its own voice from its own record. PRISM holds the order transcript, the spoken answers, and the record of what each agent did, in one session. Root Cause names stale in-flight plans. v2 enforces the constraint inside execute_payment, fail-closed, escalating ambiguous cases to a human. Replay. Every payment carries a stamp with the words that allowed it.

## PIVOT: MONEY OPERATIONS — the variance review meeting
The product is an agent system that explains financial changes across periods with evidence, run as a live variance review meeting. A deterministic pandas engine (backend/explain/) compares two periods of account summaries and transaction CSVs, ranks meaningful variances, drills each to driver transactions, and emits an evidence JSON with numbered rows E1..En. Each of the twelve agents owns a slice of the books (config/owners.yaml). The CFO asks the table out loud; owners answer live in their own voices using ONLY their evidence slice, every figure cited [E7]. v1 (freeform: the model reads raw rows and estimates) produces confident unverifiable figures; verify.py and a PRISM evaluator catch them. v2 (grounded: narrate only from the evidence JSON) passes. docs/context-memory.md grows across runs. MANDATE (orders, boundary, stamps, the radial payments floor) stays intact as PAYMENTS mode and as the deepest evidence tier: one variance can drill past transactions to a stamp to the spoken sentence that caused it. Garnish, never core.
New non-negotiable: no number is spoken or printed unless it exists in the evidence JSON. The LLM routes and phrases; pandas computes; verify.py gates. Reuse desk.classify, desk.voice_turn, sanitize_answer, prism_voice_trace, voice.speak; do not build parallel voice plumbing.

## Non-negotiables
- PRISM facts come only from docs/prism-notes.md.
- Determinism. Seeded scenario. The 67-second race is guaranteed by construction. Same seed, same output.
- PRISM sessions: one per scenario run — mandate-v1-01, mandate-v2-01. Metadata on every trace and span: company_day_id, mandate_id, run_version, tick, seed. Voice exchanges are traces in the same session with metadata channel "voice" and agent_id of the addressed agent.
- Every tool call is a PRISM span. A bare LLM trace is a bug.
- Agent spoken answers are generated from the agent's own logged record only. Never from a fresh judgment.
- User-facing copy says "record", never "trajectory" or "span". Says "PRISM recommends. I apply." Never "PRISM fixed it."
- Audio is half-duplex: record only while Space is held; never record while audio plays.
- Nothing mocked on stage. Replay plays recorded real runs.
- Every feature has a keyboard fallback that works with the network off.
- The Desk never binds on unknown scope, unknown vendor, unknown agent, or low confidence. It asks.

## Design system — violations are bugs
- Background #0B0C0E. Text #E6E6E3. Muted #7C8087. Rules #2A2C30 at 1px.
- Amber #D89B2B: held payments only. Red #D23B3B: payments executed after a mandate bound, exactly one in the scripted run. No green. No third accent.
- IBM Plex Mono for every number, tag, code. Inter Tight for the few words.
- FORBIDDEN: gradients, glow, blur, shadows, trails, grids, radar rings, cards, rounded corners, charts, icon libraries, emoji, avatars, decorative motion, centered hero text, anything that reads as a dashboard.
- Motion encodes a state change or does not exist. GSAP timelines, power2.out, 240–360ms, no bounce, no elastic.
- 60fps with 40 objects.
- Payment tags are dense operational telemetry: vendor, amount, rail, ETA, status code. Uneven real spacing. Hard 1px lines.
- Reference frames in design/frames/*.png are the target. Match them.
- Component libraries (21st.dev, shadcn, Tailwind UI, MUI) and Framer Motion are forbidden. The floor is PixiJS + GSAP; overlays are plain DOM.
- If the floor reads as bare, add information density, never decoration: PO numbers, settlement dates, sub-ledger codes on tags, 1px lane separators, a tick ruler on the execution line. The reference is a Bloomberg terminal, not a landing page.
- Precision is the finish: everything snaps to a 4px grid, mono caps get +4% tracking, line-height 1.4 on multi-line text, no text under 10px at 1080p.

## Stack
backend/: Python 3.12, uv, FastAPI, LangGraph, langchain-anthropic (claude-haiku-4-5), pydantic v2, websockets, structlog, pytest, reportlab, httpx.
frontend/: Vite, TypeScript, PixiJS v8, GSAP 3, vanilla DOM, @fontsource/ibm-plex-mono, @fontsource/inter-tight.