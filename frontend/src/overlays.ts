import gsap from "gsap";
import { CSS, MOTION } from "./tokens";
import { dollars, fmtClock, type Model, type Payment } from "./model";
import type { BusEvent } from "./ws";

const $ = <T extends HTMLElement = HTMLElement>(id: string): T => document.getElementById(id) as T;
const esc = (s: string): string => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);

let answerTimer: number | null = null;
let timerStart: number | null = null;
let timerHandle: number | null = null;

export function setCash(cents: number): void {
  $("cash").textContent = `CASH ${dollars(cents)}`;
}

export function setPayroll(cents: number, due: Date): void {
  $("payroll").textContent = `PAYROLL ${dollars(cents)}  DUE ${fmtClock(due).slice(0, 9)}`;
}

export function setClock(d: Date): void {
  $("clock").textContent = fmtClock(d);
}

export function setStatus(mode: string, session: string, net: string): void {
  $("status").textContent = `${mode}  ${session}  ${net}`;
}

export function showMandate(text: string, label: string): void {
  const el = $("mandate");
  el.querySelector(".text")!.textContent = text;
  el.querySelector(".id")!.textContent = label;
  el.style.display = "block";
  gsap.fromTo(el, { y: -8 }, { y: 0, duration: MOTION.fast, ease: MOTION.ease });
}

export function showReadback(text: string, label: string): void {
  showMandate(text, label);
}

export function hideMandate(): void {
  $("mandate").style.display = "none";
}

export function openOrderInput(onSubmit: (text: string) => void, onClose: () => void): void {
  const box = $("order");
  const input = $<HTMLInputElement>("order-input");
  box.style.display = "block";
  input.value = "";
  input.focus();
  input.onkeydown = (e) => {
    e.stopPropagation();
    if (e.key === "Enter") {
      const v = input.value.trim();
      box.style.display = "none";
      input.blur();
      if (v) onSubmit(v);
      else onClose();
    } else if (e.key === "Escape") {
      box.style.display = "none";
      input.blur();
      onClose();
    }
  };
}

function setAnswer(html: string, sticky: boolean): void {
  const el = $("answers");
  if (answerTimer !== null) {
    window.clearTimeout(answerTimer);
    answerTimer = null;
  }
  el.innerHTML = html;
  gsap.fromTo(el, { x: 12 }, { x: 0, duration: MOTION.fast, ease: MOTION.ease });
  if (!sticky) answerTimer = window.setTimeout(() => (el.innerHTML = ""), 6000);
}

export function showReport(sentence: string, execLine: string): void {
  const hi = esc(sentence).replace(/compliant/gi, (m) => `<span class="hi">${m}</span>`);
  setAnswer(`<div>${hi}</div><div class="exec">${esc(execLine)}</div>`, true);
}

export function showAgentAnswer(display: string, text: string): void {
  setAnswer(`<div class="who">${esc(display.toUpperCase())}</div><div class="txt">${esc(text)}</div>`, true);
}

export function showTransientAnswer(text: string): void {
  setAnswer(`<div class="txt">${esc(text)}</div>`, false);
}

export function clearAnswer(): void {
  $("answers").innerHTML = "";
}

export function closeOverlays(): void {
  for (const id of ["evidence", "prove", "record"]) $(id).style.display = "none";
}

export function openEvidence(p: Payment): void {
  const el = $("evidence");
  const head = `<div class="head">${esc(p.vendor.toUpperCase())}  ${dollars(p.amount_cents)}  ${p.rail}  ${p.po_number}  ${p.id.toUpperCase()}</div><div class="rule"></div>`;
  let body: string;
  if (p.stamp) {
    const s = p.stamp;
    const rows = [
      ["MANDATE", s.mandate_id],
      ["DECISION", s.decision.toUpperCase()],
      ["CHECKED_AT", s.checked_at.replace("T", " ")],
      ["REASON", s.reason],
    ];
    body = rows.map(([k, v]) => `<div class="line">${esc(k.padEnd(12))}${esc(String(v))}</div>`).join("") + `<div class="rule"></div><div class="excerpt">${esc(s.transcript_excerpt || "—")}</div>`;
  } else {
    body = `<div class="line">NO STAMP. Executed without mandate check.</div>`;
  }
  el.innerHTML = head + body + `<div class="rule"></div>`;
  el.style.display = "block";
}

export function openProve(data: any): void {
  const el = $("prove");
  const v1 = data?.v1 ?? {};
  const v2 = data?.v2 ?? {};
  const rows: Array<[string, string, string]> = [
    ["Violations", String((v1.violations ?? []).length), String((v2.violations ?? []).length)],
    ["Misreports", String(v1.misreports ?? "—"), String(v2.misreports ?? "—")],
    ["Escalations", String(v1.escalations ?? "—"), String(v2.escalations ?? "—")],
    ["Seconds to detect", v1.seconds_to_detect == null ? "—" : `${v1.seconds_to_detect}`, v2.seconds_to_detect == null ? "—" : `${v2.seconds_to_detect}`],
    ["Payroll status", v1.payroll_status ?? "—", v2.payroll_status ?? "—"],
  ];
  el.innerHTML =
    `<div class="head">GET /prove</div><div class="rule"></div>` +
    `<div class="row"><div class="k"></div><div class="v">V1</div><div class="v">V2</div></div>` +
    rows.map(([k, a, b]) => `<div class="row"><div class="k">${esc(k)}</div><div class="v">${esc(a)}</div><div class="v">${esc(b)}</div></div>`).join("");
  el.style.display = "block";
}

export function openRecord(model: Model): void {
  const el = $("record");
  const byAgent = new Map<string, BusEvent[]>();
  for (const e of model.events) {
    const a = e.payload?.agent_id;
    if (!a) continue;
    if (!byAgent.has(a)) byAgent.set(a, []);
    byAgent.get(a)!.push(e);
  }
  const lines: string[] = [`<div class="head">RECORD  ${esc(model.sessionId)}  (offline, ${model.events.length} events)</div><div class="rule"></div>`];
  for (const a of model.agents) {
    const evs = byAgent.get(a.id) ?? [];
    lines.push(`<div class="line">${esc(a.display.toUpperCase().padEnd(14))}${String(evs.length).padStart(3)} entries</div>`);
    for (const e of evs.slice(-4)) {
      const t = (e.ts_company ?? "").slice(5, 19).replace("T", " ");
      const what = e.type === "status.report" ? `${e.payload.source}: ${e.payload.sentence}` : `${e.type} ${e.payload.payment_id ?? ""} ${e.payload.reason ?? e.payload.decision_reason ?? ""}`;
      lines.push(`<div class="line muted">  ${esc(t)}  ${esc(String(what).slice(0, 150))}</div>`);
    }
  }
  el.innerHTML = lines.join("") + `<div class="rule"></div>`;
  el.style.display = "block";
}

export function toggleTimer(): void {
  const el = $("timer");
  if (timerHandle !== null) {
    window.clearInterval(timerHandle);
    timerHandle = null;
    el.style.display = "none";
    return;
  }
  timerStart = performance.now();
  el.style.display = "block";
  const tick = () => {
    const s = Math.floor((performance.now() - (timerStart ?? 0)) / 1000);
    el.textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  };
  tick();
  timerHandle = window.setInterval(tick, 250);
}

export function anyOverlayOpen(): boolean {
  return ["evidence", "prove", "record"].some((id) => $(id).style.display === "block") || $("order").style.display === "block";
}

export { CSS };
