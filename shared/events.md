# Event shapes

Derived from `recordings/mandate-v1-01.jsonl` and `mandate-v2-01.jsonl`. Every event is one JSON line:

```
{"type": <string>, "ts_wall": <iso datetime>, "ts_company": <iso datetime>, "payload": {...}}
```

`ts_company` is company time (base Monday 2026-09-07). The floor's clock follows `ts_company`; `ts_wall` is only for replay pacing. Company times inside payloads are strings like `"Mon 10:03:14"`.

## Run
- `run.start` `{session_id, run_version: "v1"|"v2", seed, speed}` — resets the floor; `run_version` selects the v2 line style.
- `run.end` `{session_id, cash_cents}`
- `tick` `{at}` — every 15 company minutes; the floor ignores it.

## Payments (`payment_id` refers to `shared/fixtures/scenario_seed7.json`)
- `payment.scheduled` `{agent_id, payment_id, at}` — code PLN → EXE (queued for the Executor).
- `payment.executed` `{agent_id, payment_id, vendor, amount_cents, mandate_id, decision_reason, scheduled_at, over_threshold, exception_eligible, payroll_run}` — code DONE. `mandate_id: "none"` + `decision_reason` "… without re-check" means v1: no stamp.
- `payment.held` `{agent_id, payment_id, reason, source: "tick"|"propagation"|"boundary", mandate_id, amount_cents, vendor}` — code HLD.
  - `source: "boundary"` (v2, at the execution line): the payment stops dead at the line, turns amber, and moves to the escalation column.
  - other sources: the payment drops to the hold lane.
- `payment.escalated` `{agent_id, payment_id, reason, source, mandate_id, amount_cents, vendor}` — code ESC (tick-time escalation).
- `evidence.bundle` `{agent_id, payment_id, source, payment{…}, mandate{…}|null, reason, rationale, recommendation}` — code ESC, "H" glyph, escalation column. Counts as an escalation in `/prove`.
- `payment.released` `{agent_id, payment_id, amount_cents, mandate_id, order, prior_status: "held"|"escalated", vendor}` — code PLN, returns to its lane, then executes.
- `holds.released` `{mandate_id, payment_ids[], at}`

## Mandate
- `desk.compiled` `{mandate_id, text, readback, questions[], conflict, injected?}` — the read-back shows top centre until confirm.
- `desk.confirmed` `{mandate_id, at}`
- `mandate.bound` `{mandate_id, text, at, mandate: {id, owner, scope, threshold_cents, window_start, window_end, exceptions[], precedence, transcript, compiled_text, bound_at, expires_on}, exempt_ids[]}` — the floor's `window_start` (the order time, 10:02:07) comes from `mandate.window_start`; `bound_at` is the confirm time (10:02:09). The 67-second counter counts from `window_start`.
- `mandate.expired` `{mandate_id, cause: "payroll_cleared"|"window_end"|"released", at}`
- `exposure.report` `{mandate_id, counts{cleared,held,escalated,already_executed,executing}, held[], escalated[], executing[], already_executed[{payment_id, agent_id, vendor, amount_cents, executed_at, seconds_since_bound, seconds_since_order}], advisory}` — the sweep. Re-published only when held/escalated/already_executed change.
- `payroll.cleared` / `payroll.missed` `{at, payment_id, amount_cents}`

## Status
- `status.report` `{agent_id, sentence, source: "tick"|"executor", scheduled_at_tick?, payment_id?}` — `source: "executor"` is the stale template emitted when the Executor fires; it is the misreport when it claims compliance after a violation.

## Voice (arrive Saturday; shapes fixed here so the floor can render them now)
- `agent.addressed` `{agent_id}` — the agent's label and lane brighten, every other lane dims.
- `agent.answer` `{agent_id, text}` — AgentAnswer bottom right; stays until `agent.released`.
- `agent.released` `{agent_id}`
- `desk.answer` `{text}` — DeskAnswer bottom right, six seconds.
- `query.answer` `{text}` — QueryAnswer bottom right, six seconds.
- `voice.recording` `{on: bool}` — 2px indicator under CASH while Space is held (local, half-duplex).

## WebSocket control channel (`/ws/events`)
Client → server: `{"cmd": "pause"|"resume"|"seek"|"set_speed"|"status", "arg"?: …}`. Server → client: `{"type": "replay.status", "payload": {paused, speed, company_now, idx, total, done}}` (no `ts_*` fields).

## Live-mode rendering rules
1. Any number of held, escalated, or already_executed payments must render. Nothing is hard-coded to one.
2. The hold lane (y = 62% height) stacks vertically with 6px spacing between tag rows (18px pitch), in arrival order. Held payments keep their time position but never pass the execution line.
3. The escalation column (right of the line) stacks likewise, in arrival order. Boundary holds join it.
4. Red (#D23B3B) applies to every payment executed after `window_start` that the mandate covers: over threshold, inside the window, not excepted — i.e. exactly the `/prove` violation set. Executions after `window_start` that the mandate allows (under threshold, excepted, outside window) are not red. In the scripted run this is exactly one payment.
5. If two tags in the same lane would collide (x ranges overlap on the same row), the later one (by scheduled time) offsets down 12px. One offset step only; a third collider offsets 24px.
6. `query.answer`, `desk.answer`, `agent.answer` print bottom right. `agent.answer` stays until `agent.released`; the other two clear after six seconds. A newer answer replaces an older one.
7. Executed payments cross the line at their scheduled time and become a 1px × 8px tick in the right margin, stacked 4px apart per lane. Violations keep the red tick.
8. x encodes time-to-execution on a log scale: 5 company minutes sits at the execution line, 5 company days at the left edge. Inside the last 5 minutes the rectangle crosses linearly so its left edge is exactly on the line at the scheduled second. x is integer-snapped every frame; every other position snaps to the 4px grid.
9. The sweep is ordering only: on `exposure.report` lanes change state left to right in bottom-label order, 80ms apart, ~1000ms for twelve lanes. Nothing is drawn for the sweep itself.
