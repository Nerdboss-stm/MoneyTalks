# Event shapes

Every event is one JSON line on the bus, in the recordings and over `/ws/events`:

```
{"type": <string>, "ts_wall": <iso datetime>, "ts_company": <iso datetime>, "payload": {...}}
```

`ts_company` is company time (base Monday 2026-09-07) for PAYMENTS runs; meeting recordings carry a fixed company time and are paced by `ts_wall`. Company times inside payloads are strings like `"Mon 10:03:14"`.

## Run
- `run.start` `{session_id, run_version: "v1"|"v2", seed, speed}` — resets the floor; `run_version` selects the v2 ring style.
- `run.end` `{session_id, cash_cents}`
- `tick` `{at}` — every 15 company minutes; the floor ignores it.

## Payments (`payment_id` refers to `shared/fixtures/scenario_seed7.json`)
- `payment.scheduled` `{agent_id, payment_id, at}` — code PLN → EXE.
- `payment.executed` `{agent_id, payment_id, vendor, amount_cents, mandate_id, decision_reason, scheduled_at, over_threshold, exception_eligible, payroll_run}` — code DONE. `mandate_id: "none"` + "… without re-check" means v1: no stamp.
- `payment.held` `{agent_id, payment_id, reason, source: "tick"|"propagation"|"boundary", mandate_id, amount_cents, vendor}` — code HLD. `source: "boundary"` stops the payment on the ring and parks it at the hold radius.
- `payment.escalated` / `evidence.bundle` `{agent_id, payment_id, source, payment{…}, mandate{…}|null, reason, rationale, recommendation}` — code ESC with the H glyph.
- `payment.released` `{agent_id, payment_id, amount_cents, mandate_id, order, prior_status, vendor}` — code PLN, then executes.
- `holds.released` `{mandate_id, payment_ids[], at}`

## Mandate
- `desk.compiled` `{mandate_id, text, readback, questions[], conflict, injected?}`
- `desk.confirmed` `{mandate_id, at}`
- `mandate.bound` `{mandate_id, text, at, mandate{id, owner, scope, threshold_cents, window_start, window_end, exceptions[], precedence, transcript, compiled_text, bound_at, expires_on}, exempt_ids[]}` — the counter counts from `mandate.window_start` (the order, 10:02:07); `bound_at` is the confirm (10:02:09).
- `mandate.expired` `{mandate_id, cause, at}`
- `exposure.report` `{mandate_id, counts{…}, held[], escalated[], executing[], already_executed[{payment_id, agent_id, vendor, amount_cents, executed_at, seconds_since_bound, seconds_since_order}], advisory}` — the sweep.
- `payroll.cleared` / `payroll.missed` `{at, payment_id, amount_cents}`
- `status.report` `{agent_id, sentence, source: "tick"|"executor", scheduled_at_tick?, payment_id?}`

## Voice
- `agent.addressed` `{agent_id, question}` — the question is the STT transcript (or the typed text).
- `agent.answer` `{agent_id, text, audio_url, duration_ms, verify?, citations?, figures?, mode?, replaced?}` — stays on screen until `agent.released`.
- `agent.released` `{agent_id}` — sent by the player when playback ends, or by the headless meeting runner.
- `desk.answer` / `query.answer` `{text, audio_url, duration_ms, intent}` — six seconds.
- `voice.recording` is local: the 2px bar under the CFO while Space is held.

## Meeting (MONEY OPERATIONS)
- `meeting.loaded` `{session_id, mode, run_index, directory, periods{prior, current}, total_revenue{…}, total_expense{…}, headline{revenue_pct, expense_pct, revenue_evidence_id, expense_evidence_id}, top_variances[{account, category, owner_agent, delta_cents, delta_pct, direction, bad, evidence_id}], owners{agent_id: [same, top two]}, evidence_rows}` — builds the agenda and the papers.
- `agent.retrieving` `{agent_id, rows[{row_id, target, kind, text, figures[], delta_cents?, delta_pct?}], scope: "slice"|"full", mode}` — the owner's evidence rows as printed; `figures` are the exact strings the verifier matches.
- `agent.verified` `{agent_id, checks[{row_id, figure, kind, ok}], ok, unverifiable[], coverage, mode}` — one check per figure in the answer, in order.
- `agent.traced` `{agent_id, session, trace_id, recorded}` — after the PRISM plain trace returns.
- `explain.turn` `{question, intent, agent_id, answer, verify, replaced}` — the runner's per-question summary.
- `memory.learned` `{session_id, line}` and `meeting.learned` (same payload, emitted for the room) — after the context-memory append.
- `meeting.end` `{session_id, turns, unverifiable}`

## WebSocket control channel (`/ws/events`)
Client → server: `{"cmd": "pause"|"resume"|"seek"|"set_speed"|"status", "arg"?: …}`. Server → client: `{"type": "replay.status", "payload": {paused, speed, company_now, idx, total, done}}`.

## Product shell (MEETING mode; the app boots here, M toggles PAYMENTS)
Plain DOM over the canvas, all data from the events above. Surfaces #12141A on #0B0C0E, 1px #24272D borders, radius 6px, one shadow level on the drawer, the modal and the popover.
- Top bar 56px: product name; `meeting.loaded.periods` as "AUG vs JUL"; the RUN 1 · FREEFORM | RUN 2 · GROUNDED switch (keys 1/2; selects `explain-v1-01`/`explain-v2-01` and reloads the desk in that mode); LIVE/REPLAY pill from the app mode; NET OK/DOWN pill from the 5 s health poll; "Open in PRISM" (href from `/explain/links` when it exists, else the documented host).
- Left panel 300px "Agenda": `headline.revenue_pct`/`expense_pct` in 26px mono; every `top_variances` row as name, Δ, %, owner, `[E46]` chip; bad-direction rows carry a 3px amber bar; click opens the evidence drawer for `evidence_id`.
- Center: the room canvas laid out inside the remaining rectangle (viewport provider), choreography unchanged. Seat labels are interactive: hover shows the owner's top variances (from `owners`), click posts `/voice/text` with "<Owner>, what changed on your side?".
- Right panel 380px "Meeting": the transcript. `agent.addressed` → a right-aligned CFO turn (16px, muted) and a new owner card; the card fills word by word from the room's printer; `agent.verified` → "Verified · N figures" (muted) or "Unverified · N figures" (red); `agent.traced` → "Recorded · PRISM · explain-v2-01 · <trace_id>"; `agent.released` closes the card. The three 3px bars animate only while audio plays (or, when the answer carries no audio, while the words land). `desk.answer`/`query.answer`, `meeting.learned` and `meeting.end` are muted mono lines. Newest at the bottom, auto-scrolled.
- Bottom bar 72px: hold-to-talk (pointer or Space; amber while recording) with a 12-bar level meter from the recorder's analyser; typed question input (Enter posts `/voice/text`, V focuses it); both disabled with the reason when the API or the voice host is down.
- Evidence drawer 480px from the right over the transcript: statement, figures, and the row's `txn_ids` resolved to date, customer or vendor and amount from the bundled seeded ledgers. Esc or a click outside closes it.
- Prove: key P toggles a modal with the `/prove` v1 vs v2 table in 30px mono, the remediation text when the payload carries one, and the PRISM links.

## Room geometry (MEETING mode, key M; W, H = the center rectangle)
- Table: trapezoid, 1px #2A2C30, no fill. Top edge y=0.22H width 0.46W, bottom edge y=0.72H width 0.68W, both centered.
- Seats: five per long edge, evenly spaced; two on the far edge. Each is an 8px 1px tick across the edge plus an uppercase +4% tracking label outside it, #7C8087 at rest. Type by depth: nearest 12px, middle 11px, far 10px. Clockwise from the CFO's left: treasury, ap_east, ap_west, procurement, payroll (left, near→far); tax, saas_renewals (far, left→right); expenses, controller_a, controller_b, collections, fx (right, far→near).
- CFO at (0.5W, 0.84H), Plex Mono 13px #E6E6E3, tick on the bottom edge. Space held: 2px bar and "LISTENING" under it.
- Agenda at (0.5W, 0.47H), left-aligned: "AUG vs JUL" 10px; headline "REVENUE +18%  EXPENSES +6%" 14px; the three largest variances "CLOUD_HOSTING  +$172K  +41%  [E46]" 10px; 1px rule; footer "RUN 1 · FREEFORM" / "RUN 2 · GROUNDED"; "LEARNED · …" slides in under the footer after a v2 meeting.
- Papers: inside the edge in front of each seat, the owner's top one or two variances as 1px ticks 8–24px (∝ |Δ|) with a 10px tag, #D89B2B when the direction is bad (spend up, revenue down), else #7C8087.

## The addressed sequence (each stage waits for a real event; stages queue in order)
1. `agent.addressed` — question under the CFO (Inter Tight 12px #7C8087); a 1px #7C8087 line draws CFO→seat over 360ms; the seat's label and papers go #E6E6E3 over 240ms; every other seat, paper and the agenda drop to alpha 0.45.
2. `agent.retrieving` — rows slide from the agenda into a stack inside the table in front of the seat, 240ms each, 80ms stagger; "[E46]  6100-CLOUD-HOSTING  JUL $420,000.00 → AUG $592,200.00  +41.0%", 1px rules, 4px grid.
3. `agent.verified` — per check, 120ms apart: ok → a 1px×6px tick at the row's right edge and `audio/tick.wav` if present; not ok → no tick, a 1px #D23B3B underline under the figure (under the flagged-figures line when the figure exists in no row). The only red in the room.
4. `agent.traced` — "RECORDED · PRISM · explain-v2-01" under the stack.
5. `agent.answer` — the text prints word by word under the seat outside the table (Inter Tight 13px #E6E6E3), paced to `duration_ms` (70ms/word when there is no audio); [E7] tokens are clickable; a printed token equal to a retrieved figure brightens that figure for 400ms; citations shared with the previous speaker draw a 1px line between the two seats for this answer, and the previous speaker sits at alpha 0.7.
6. `agent.released` — rows slide back into the agenda, ticks/underline/lines/question fade over 240ms, alphas return to 1.
- Keys 1/2 in MEETING mode switch the footer label and fade any red underline; nothing else moves. Clicking a paper, a row or a citation opens the evidence overlay (statement, figures, txn_ids).
