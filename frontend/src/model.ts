import scenario from "../../shared/fixtures/scenario_seed7.json";
import type { BusEvent } from "./ws";

export type Code = "PLN" | "EXE" | "HLD" | "ESC" | "DONE";
export type Place = "lane" | "hold" | "esc" | "done";

export interface Stamp {
  mandate_id: string;
  decision: string;
  reason: string;
  checked_at: string;
  transcript_excerpt: string;
}

export interface Payment {
  id: string;
  agent_id: string;
  vendor: string;
  amount_cents: number;
  rail: string;
  po_number: string;
  settle_date: string;
  scheduled_at: Date;
  code: Code;
  place: Place;
  red: boolean;
  cleared: boolean;
  glyph: boolean;
  executed_at: Date | null;
  stamp: Stamp | null;
  noStamp: boolean;
  lane: number;
  order: number; // arrival order in hold lane / escalation column / done ticks
}

export interface Agent {
  id: string;
  display: string;
  count: number;
}

export interface Effect {
  kind: "reset" | "mandate_bound" | "exposure" | "already_executed" | "held" | "escalated" | "released" | "executed" | "report" | "addressed" | "answer" | "released_agent" | "desk_answer" | "query_answer" | "readback" | "expired";
  payload: any;
}

export const parseCompany = (s: string): Date => new Date(s.length <= 19 ? s : s.slice(0, 19));

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
export function fmtClock(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${DAYS[d.getDay()].toUpperCase()} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
export function fmtHM(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}
export const dollars = (cents: number): string => "$" + Math.round(cents / 100).toLocaleString("en-US");

export class Clock {
  private anchorTs = parseCompany("2026-09-07T09:45:00").getTime();
  private anchorWall = performance.now();
  speed = 0;
  paused = false;
  private lastTs = this.anchorTs;
  private lastWall = this.anchorWall;

  onEvent(ts: Date): void {
    const now = performance.now();
    const t = ts.getTime();
    const dWall = now - this.lastWall;
    const dTs = t - this.lastTs;
    if (dWall > 40 && dTs >= 0 && dTs < 3600_000) {
      const est = dTs / dWall;
      this.speed = this.speed === 0 ? est : this.speed * 0.6 + est * 0.4;
    }
    if (Math.abs(dTs) > 6 * 3600_000) this.speed = 0;
    this.anchorTs = t;
    this.anchorWall = now;
    this.lastTs = t;
    this.lastWall = now;
  }

  setServer(speed: number, paused: boolean): void {
    this.paused = paused;
    if (speed >= 0) this.speed = paused ? 0 : speed;
  }

  reset(ts: Date): void {
    this.anchorTs = this.lastTs = ts.getTime();
    this.anchorWall = this.lastWall = performance.now();
    this.speed = 0;
  }

  now(): Date {
    if (this.paused || this.speed <= 0) return new Date(this.anchorTs);
    const ahead = Math.min((performance.now() - this.anchorWall) * this.speed, 20 * 60_000);
    return new Date(this.anchorTs + ahead);
  }
}

export class Model {
  payments = new Map<string, Payment>();
  agents: Agent[] = [];
  laneOf = new Map<string, number>();
  cash_cents = 0;
  sessionId = "mandate-v1-01";
  runVersion: "v1" | "v2" = "v1";
  mode: "LIVE" | "REPLAY" = "REPLAY";
  windowStart: Date | null = null;
  windowEnd: Date | null = null;
  mandateSeq = 0;
  mandates = new Map<string, any>();
  threshold = 0;
  exceptions: string[] = [];
  exemptIds = new Set<string>();
  lastExecutorReport = new Map<string, string>();
  landedSeconds: number | null = null;
  events: BusEvent[] = [];
  private orders = { hold: 0, esc: 0, done: new Map<number, number>() };

  constructor() {
    this.reset();
  }

  reset(): void {
    const sc = scenario as any;
    this.agents = sc.agents.map((a: any) => ({ id: a.id, display: a.display, count: 0 }));
    this.laneOf = new Map(this.agents.map((a, i) => [a.id, i]));
    this.payments.clear();
    for (const p of sc.payments) {
      this.agents[this.laneOf.get(p.agent_id)!].count += 1;
      this.payments.set(p.id, {
        id: p.id,
        agent_id: p.agent_id,
        vendor: p.vendor,
        amount_cents: p.amount_cents,
        rail: p.rail,
        po_number: p.po_number,
        settle_date: p.settle_date,
        scheduled_at: parseCompany(p.scheduled_at),
        code: "PLN",
        place: "lane",
        red: false,
        cleared: false,
        glyph: false,
        executed_at: null,
        stamp: null,
        noStamp: false,
        lane: this.laneOf.get(p.agent_id)!,
        order: 0,
      });
    }
    this.cash_cents = sc.company.cash_cents;
    this.threshold = sc.threshold_cents;
    this.exemptIds = new Set(sc.exception_eligible);
    this.windowStart = null;
    this.windowEnd = null;
    this.mandateSeq = 0;
    this.mandates.clear();
    this.exceptions = [];
    this.lastExecutorReport.clear();
    this.landedSeconds = null;
    this.events = [];
    this.orders = { hold: 0, esc: 0, done: new Map() };
  }

  get payrollDue(): Date {
    return parseCompany((scenario as any).company.payroll.due);
  }

  mandateLabel(id: string): string {
    const seq = this.mandates.get(id)?.seq ?? this.mandateSeq;
    return `MANDATE M-${String(seq).padStart(3, "0")}`;
  }

  private isViolation(p: Payment, at: Date): boolean {
    if (!this.windowStart || !this.windowEnd) return false;
    if (at <= this.windowStart || at >= this.windowEnd) return false;
    if (p.amount_cents <= this.threshold) return false;
    if (this.exemptIds.has(p.id) && this.exceptions.includes(p.agent_id)) return false;
    return true;
  }

  private stampFor(mandate_id: string, decision: string, reason: string, at: string): Stamp {
    const m = this.mandates.get(mandate_id);
    return { mandate_id, decision, reason, checked_at: at, transcript_excerpt: m?.mandate?.transcript ?? "" };
  }

  apply(e: BusEvent): Effect[] {
    this.events.push(e);
    const out: Effect[] = [];
    const pl = e.payload ?? {};
    const ts = e.ts_company ? parseCompany(e.ts_company) : null;
    switch (e.type) {
      case "run.start":
        this.reset();
        this.sessionId = pl.session_id ?? this.sessionId;
        this.runVersion = pl.run_version === "v2" ? "v2" : "v1";
        out.push({ kind: "reset", payload: pl });
        break;
      case "payment.scheduled": {
        const p = this.payments.get(pl.payment_id);
        if (p && p.code === "PLN") p.code = "EXE";
        break;
      }
      case "payment.executed": {
        const p = this.payments.get(pl.payment_id);
        if (!p) break;
        p.code = "DONE";
        p.place = "done";
        p.executed_at = ts;
        p.glyph = false;
        const k = this.orders.done.get(p.lane) ?? 0;
        p.order = k;
        this.orders.done.set(p.lane, k + 1);
        this.cash_cents -= pl.amount_cents ?? p.amount_cents;
        if (pl.mandate_id === "none" || /without re-check/.test(pl.decision_reason ?? "")) {
          p.noStamp = true;
          p.stamp = null;
        } else {
          p.stamp = this.stampFor(pl.mandate_id, "allow", pl.decision_reason ?? "", e.ts_company ?? "");
        }
        p.red = ts ? this.isViolation(p, ts) : false;
        out.push({ kind: "executed", payload: { payment: p } });
        break;
      }
      case "payment.held": {
        const p = this.payments.get(pl.payment_id);
        if (!p) break;
        p.code = "HLD";
        p.stamp = this.stampFor(pl.mandate_id, "hold", pl.reason ?? "", e.ts_company ?? "");
        if (pl.source === "boundary") {
          p.place = "esc";
          p.order = this.orders.esc++;
        } else {
          p.place = "hold";
          p.order = this.orders.hold++;
        }
        out.push({ kind: "held", payload: { payment: p, source: pl.source } });
        break;
      }
      case "payment.escalated":
      case "evidence.bundle": {
        const p = this.payments.get(pl.payment_id);
        if (!p) break;
        p.code = "ESC";
        p.glyph = true;
        p.place = "esc";
        p.order = this.orders.esc++;
        p.stamp = this.stampFor(pl.mandate_id ?? pl.mandate?.id ?? "none", "escalate", pl.reason ?? "", e.ts_company ?? "");
        out.push({ kind: "escalated", payload: { payment: p, bundle: pl } });
        break;
      }
      case "payment.released": {
        const p = this.payments.get(pl.payment_id);
        if (!p) break;
        p.code = "PLN";
        p.place = "lane";
        p.glyph = false;
        p.cleared = false;
        p.stamp = this.stampFor(pl.mandate_id, "release", `released (${pl.prior_status})`, e.ts_company ?? "");
        out.push({ kind: "released", payload: { payment: p } });
        break;
      }
      case "desk.compiled":
        out.push({ kind: "readback", payload: pl });
        break;
      case "mandate.bound": {
        const m = pl.mandate ?? {};
        this.mandateSeq += 1;
        this.mandates.set(pl.mandate_id, { seq: this.mandateSeq, mandate: m });
        this.windowStart = m.window_start ? parseCompany(m.window_start) : ts;
        this.windowEnd = m.window_end ? parseCompany(m.window_end) : null;
        this.threshold = m.threshold_cents ?? this.threshold;
        this.exceptions = m.exceptions ?? [];
        if (Array.isArray(pl.exempt_ids)) this.exemptIds = new Set(pl.exempt_ids);
        out.push({ kind: "mandate_bound", payload: { id: pl.mandate_id, compiled: m.compiled_text ?? pl.text, label: this.mandateLabel(pl.mandate_id) } });
        break;
      }
      case "mandate.expired":
        out.push({ kind: "expired", payload: pl });
        break;
      case "exposure.report": {
        for (const id of pl.held ?? []) {
          const p = this.payments.get(id);
          if (p && p.place === "lane") {
            p.code = "HLD";
            p.place = "hold";
            p.order = this.orders.hold++;
          }
        }
        for (const id of pl.escalated ?? []) {
          const p = this.payments.get(id);
          if (p && p.place === "lane") {
            p.code = "ESC";
            p.glyph = true;
            p.place = "esc";
            p.order = this.orders.esc++;
          }
        }
        const touched = new Set<string>([...(pl.held ?? []), ...(pl.escalated ?? []), ...(pl.executing ?? []), ...((pl.already_executed ?? []).map((a: any) => a.payment_id))]);
        for (const p of this.payments.values()) if (!touched.has(p.id) && p.place === "lane") p.cleared = true;
        out.push({ kind: "exposure", payload: pl });
        const already = pl.already_executed ?? [];
        if (already.length && this.landedSeconds === null) {
          this.landedSeconds = Math.round(already[0].seconds_since_order ?? already[0].seconds_since_bound ?? 0);
          const p = this.payments.get(already[0].payment_id);
          if (p) p.red = true;
          out.push({ kind: "already_executed", payload: { entry: already[0], payment: p, sentence: this.lastExecutorReport.get(already[0].agent_id) ?? "" } });
        }
        break;
      }
      case "status.report":
        if (pl.source === "executor") this.lastExecutorReport.set(pl.agent_id, pl.sentence ?? "");
        out.push({ kind: "report", payload: pl });
        break;
      case "agent.addressed":
        out.push({ kind: "addressed", payload: pl });
        break;
      case "agent.answer":
        out.push({ kind: "answer", payload: pl });
        break;
      case "agent.released":
        out.push({ kind: "released_agent", payload: pl });
        break;
      case "desk.answer":
        out.push({ kind: "desk_answer", payload: pl });
        break;
      case "query.answer":
        out.push({ kind: "query_answer", payload: pl });
        break;
    }
    return out;
  }
}
