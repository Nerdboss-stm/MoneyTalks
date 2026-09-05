import { DISPLAY, type Side } from "./room";
import { SHELL } from "./tokens";
import { txn } from "./txns";
import type { BusEvent } from "./ws";

export interface ShellHooks {
  onRun: (index: 1 | 2) => void;
  onAsk: (text: string) => void;
  onHoldStart: () => void;
  onHoldEnd: () => void;
  onEvidence: (id: string) => void;
}

interface Card {
  agentId: string;
  el: HTMLElement;
  txt: HTMLElement;
  verdict: HTMLElement;
  recorded: HTMLElement;
  hasAudio: boolean;
  text: string;
  printed: boolean;
  done: boolean;
}

const $ = <T extends HTMLElement = HTMLElement>(id: string): T => document.getElementById(id) as T;
const esc = (s: string): string => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
const CITE = /(\[E\d+(?:\s*,\s*E\d+)*\])/;
const MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
const mon = (s: unknown): string => MON[Number(String(s).slice(5, 7)) - 1] ?? String(s ?? "—");
const pct = (v: unknown): string => (v == null || v === "" ? "—" : `${Number(v) >= 0 ? "+" : ""}${Number(v).toFixed(0)}%`);
const usd = (cents: number): string => (cents < 0 ? "-" : "") + "$" + (Math.abs(cents) / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const usdK = (cents: number): string => {
  const d = Math.abs(cents) / 100;
  const s = d >= 1_000_000 ? `$${(d / 1_000_000).toFixed(2)}M` : d >= 1000 ? `$${Math.round(d / 1000)}K` : `$${d.toFixed(0)}`;
  return (cents < 0 ? "-" : "+") + s;
};
const ARROW = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 12 12 4M6 4h6v6"/></svg>';

export class Shell {
  private root = $("shell");
  private transcript = $("transcript");
  private cards: Card[] = [];
  private evidence = new Map<string, any>();
  private lastQuestion = { text: "", at: 0 };
  private net = { backend: false, voice: false };
  private proveOpen = false;
  private meterBars: HTMLElement[];
  private prism: { host: string; session?: string; project?: string; trace?: string } = { host: "https://prism.blockconvey.com" };

  constructor(private hooks: ShellHooks) {
    for (const b of this.root.querySelectorAll<HTMLButtonElement>("#runs button")) b.addEventListener("click", () => this.hooks.onRun(Number(b.dataset.run) === 1 ? 1 : 2));
    const ptt = $<HTMLButtonElement>("ptt");
    ptt.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      if (!ptt.disabled) this.hooks.onHoldStart();
    });
    const up = () => this.hooks.onHoldEnd();
    ptt.addEventListener("pointerup", up);
    ptt.addEventListener("pointerleave", up);
    ptt.addEventListener("pointercancel", up);
    const meter = $("meter");
    meter.innerHTML = "<i></i>".repeat(SHELL.meterBars);
    this.meterBars = [...meter.querySelectorAll<HTMLElement>("i")];
    const ask = $<HTMLInputElement>("ask");
    ask.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter") {
        const v = ask.value.trim();
        ask.value = "";
        ask.blur();
        if (v) this.hooks.onAsk(v);
      } else if (e.key === "Escape") ask.blur();
    });
    ask.addEventListener("keyup", (e) => e.stopPropagation());
    $("drawer").querySelector(".close")!.addEventListener("click", () => this.closeDrawer());
    document.addEventListener(
      "pointerdown",
      (e) => {
        const t = e.target as HTMLElement;
        if (this.drawerOpen() && !$("drawer").contains(t)) this.closeDrawer();
        if (this.proveOpen && !$("prove-modal").contains(t)) this.closeProve();
      },
      true,
    );
    this.root.addEventListener("click", (e) => {
      const chip = (e.target as HTMLElement).closest<HTMLElement>(".chip[data-id]");
      if (chip) {
        e.stopPropagation();
        this.hooks.onEvidence(chip.dataset.id!);
      }
    });
  }

  // ---------------------------------------------------------------- chrome

  setVisible(on: boolean): void {
    this.root.hidden = !on;
    if (!on) this.closeAll();
  }

  centerRect(): [number, number, number, number] {
    if (this.root.hidden) return [0, 0, window.innerWidth, window.innerHeight];
    return [SHELL.left, SHELL.top, window.innerWidth - SHELL.left - SHELL.right, window.innerHeight - SHELL.top - SHELL.bottom];
  }

  setStatus(mode: string, session: string): void {
    const pill = $("pill-mode");
    pill.textContent = mode === "LIVE" ? "LIVE" : "REPLAY";
    pill.classList.toggle("live", mode === "LIVE");
    $("meeting-sub").textContent = session;
  }

  setNet(backend: boolean, voice: boolean): void {
    this.net = { backend, voice };
    const pill = $("pill-net");
    const ok = backend && voice;
    pill.textContent = ok ? "NET OK" : `NET DOWN${backend ? "" : " · API"}${voice ? "" : " · VOICE"}`;
    pill.classList.toggle("down", !ok);
    const ptt = $<HTMLButtonElement>("ptt");
    ptt.disabled = !ok;
    ptt.textContent = ok ? "HOLD SPACE TO SPEAK" : backend ? "VOICE DOWN · TYPE INSTEAD" : "NET DOWN · MIC OFF";
    const ask = $<HTMLInputElement>("ask");
    ask.disabled = !backend;
    ask.placeholder = backend ? "Ask the table. Enter sends." : "NET DOWN · typed questions need the API";
  }

  setPrism(links: { host: string; session?: string; project?: string; trace?: string }): void {
    this.prism = links;
    $<HTMLAnchorElement>("prism-link").href = links.session ?? links.project ?? links.host;
  }

  setRun(index: number): void {
    for (const b of this.root.querySelectorAll<HTMLButtonElement>("#runs button")) b.classList.toggle("on", Number(b.dataset.run) === index);
  }

  setRecording(on: boolean): void {
    const ptt = $<HTMLButtonElement>("ptt");
    ptt.classList.toggle("rec", on);
    if (!ptt.disabled) ptt.textContent = on ? "LISTENING · RELEASE TO SEND" : "HOLD SPACE TO SPEAK";
    if (!on) this.setLevel(new Array(SHELL.meterBars).fill(0));
  }

  setLevel(bars: number[]): void {
    this.meterBars.forEach((el, i) => {
      const v = Math.max(0, Math.min(1, bars[i] ?? 0));
      el.style.height = `${2 + Math.round((v * 22) / 2) * 2}px`;
      el.classList.toggle("on", v > 0.08);
    });
  }

  setEvidence(json: any): void {
    this.evidence.clear();
    for (const r of json?.evidence ?? []) this.evidence.set(r.id, r);
    // the full ranked table: every account-level variance the engine ranked, cited by its variance row
    const ranked: any[] = (json?.variances ?? []).filter((v: any) => v && !String(v.key ?? "").startsWith("TOTAL:"));
    if (!ranked.length) return;
    const idOf = new Map<string, string>();
    for (const r of json?.evidence ?? []) if (r.kind === "variance" && r.target && !idOf.has(r.target)) idOf.set(r.target, r.id);
    const rows = ranked
      .slice()
      .sort((a: any, b: any) => (a.rank ?? 1e9) - (b.rank ?? 1e9) || Math.abs(b.delta_cents ?? 0) - Math.abs(a.delta_cents ?? 0))
      .map((v: any) => ({
        account: v.key,
        delta_cents: v.delta_cents,
        delta_pct: v.delta_pct,
        owner_agent: v.owner_agent,
        bad: v.category === "revenue" || /^4\d{3}-/.test(String(v.key ?? "")) ? Number(v.delta_cents ?? 0) < 0 : Number(v.delta_cents ?? 0) > 0,
        evidence_id: idOf.get(v.key) ?? "",
      }));
    this.renderAgenda(rows);
  }

  // ---------------------------------------------------------------- events

  handle(e: BusEvent): void {
    const pl = e.payload ?? {};
    switch (e.type) {
      case "meeting.loaded":
        this.loadMeeting(pl);
        break;
      case "agent.addressed":
        this.cfoTurn(String(pl.question ?? ""), true);
        this.newCard(String(pl.agent_id ?? ""));
        break;
      case "agent.verified": {
        const c = this.cardOf(pl.agent_id);
        if (c) this.verdict(c, Boolean(pl.ok), Array.isArray(pl.checks) ? pl.checks.length : (pl.unverifiable ?? []).length);
        break;
      }
      case "agent.traced": {
        const c = this.cardOf(pl.agent_id);
        if (c && pl.recorded !== false) c.recorded.textContent = `Recorded · PRISM · ${pl.session ?? ""}${pl.trace_id ? ` · ${pl.trace_id}` : ""}`;
        this.scroll();
        break;
      }
      case "agent.answer": {
        const c = this.cardOf(pl.agent_id) ?? this.newCard(String(pl.agent_id ?? ""));
        c.text = String(pl.text ?? "");
        c.hasAudio = Boolean(pl.audio_url);
        if (!c.verdict.textContent && pl.verify) this.verdict(c, Boolean(pl.verify.ok), Number(pl.verify.figures_checked ?? 0));
        if (!DISPLAY[c.agentId]) this.fill(c);
        break;
      }
      case "agent.released": {
        const c = this.cardOf(pl.agent_id);
        if (c) {
          if (!c.printed) this.fill(c);
          c.done = true;
          c.el.classList.remove("speaking");
          c.el.classList.add("done");
        }
        break;
      }
      case "desk.answer":
      case "query.answer":
        this.sys(`Desk · ${pl.text ?? ""}`);
        break;
      case "meeting.learned":
        this.sys(`Learned · ${String(pl.line ?? "").replace(/^-\s*[\d-]+\s+\S+:\s*/, "")}`);
        break;
      case "meeting.end":
        this.sys(`Meeting ended · ${pl.turns ?? "—"} turns · ${pl.unverifiable ?? 0} unverifiable`);
        break;
    }
  }

  private loadMeeting(pl: any): void {
    const p = pl.periods ?? {};
    const periods = `${mon(p.current)} vs ${mon(p.prior)}`;
    $("periods").textContent = periods;
    const rev = $("rev");
    const exp = $("exp");
    rev.textContent = pct(pl.headline?.revenue_pct);
    exp.textContent = pct(pl.headline?.expense_pct);
    rev.classList.toggle("bad", Number(pl.headline?.revenue_pct ?? 0) < 0);
    exp.classList.toggle("bad", Number(pl.headline?.expense_pct ?? 0) > 0);
    this.renderAgenda(pl.top_variances ?? []);
    if (pl.session_id) $("meeting-sub").textContent = String(pl.session_id);
    if (pl.mode) this.setRun(pl.mode === "v1" ? 1 : 2);
    this.transcript.innerHTML = "";
    this.cards = [];
    this.sys(`Meeting loaded · ${pl.session_id ?? ""} · ${periods}`);
  }

  private renderAgenda(list: any[]): void {
    const rows = $("agenda-rows");
    rows.innerHTML = "";
    if (!list.length) rows.innerHTML = '<div id="empty">No variances ranked.</div>';
    for (const v of list) {
      const id = String(v.evidence_id ?? "");
      const el = document.createElement("div");
      el.className = `row${v.bad ? " bad" : ""}`;
      el.setAttribute("role", "button");
      el.tabIndex = 0;
      el.innerHTML =
        `<span class="name">${esc(String(v.account ?? "").replace(/^\d+-/, ""))}</span>` +
        `<span class="num">${esc(usdK(Number(v.delta_cents ?? 0)))}<span class="pct">${esc(pct(v.delta_pct))}</span></span>` +
        `<span class="owner">${esc(DISPLAY[v.owner_agent] ?? v.owner_agent ?? "")}</span>` +
        `<span class="chipcol">${id ? `<span class="chip" data-id="${esc(id)}">${esc(id)}</span>` : ""}</span>`;
      if (id) {
        el.addEventListener("click", () => this.hooks.onEvidence(id));
        el.addEventListener("keydown", (k) => {
          if (k.key === "Enter" || k.key === " ") {
            k.preventDefault();
            k.stopPropagation();
            this.hooks.onEvidence(id);
          }
        });
      }
      rows.appendChild(el);
    }
    $("agenda-sub").textContent = `${list.length} ranked`;
  }

  // ---------------------------------------------------------------- transcript

  private scroll(): void {
    this.transcript.scrollTop = this.transcript.scrollHeight;
  }

  sys(text: string): void {
    const el = document.createElement("div");
    el.className = "turn sys";
    el.textContent = text;
    this.transcript.appendChild(el);
    this.scroll();
  }

  notice(text: string): void {
    this.sys(text);
  }

  /* Typed questions are echoed at once; the bus repeats them in agent.addressed, so that copy is skipped once. */
  cfoTurn(text: string, fromBus = false): void {
    if (!text) return;
    const now = performance.now();
    if (fromBus) {
      const echoed = this.lastQuestion.text === text && now - this.lastQuestion.at < 15000;
      this.lastQuestion = { text: "", at: 0 };
      if (echoed) return;
    } else this.lastQuestion = { text, at: now };
    const el = document.createElement("div");
    el.className = "turn cfo";
    el.innerHTML = `<div class="who">CFO</div><div class="q"></div>`;
    el.querySelector(".q")!.textContent = text;
    this.transcript.appendChild(el);
    this.scroll();
  }

  private newCard(agentId: string): Card {
    const el = document.createElement("div");
    el.className = "card";
    el.dataset.agent = agentId;
    el.innerHTML = `<div class="head"><span class="name"></span><span class="bars" aria-hidden="true"><i></i><i></i><i></i></span></div><div class="txt"></div><div class="verdict"></div><div class="recorded"></div>`;
    el.querySelector(".name")!.textContent = DISPLAY[agentId] ?? agentId;
    this.transcript.appendChild(el);
    const card: Card = { agentId, el, txt: el.querySelector(".txt")!, verdict: el.querySelector(".verdict")!, recorded: el.querySelector(".recorded")!, hasAudio: false, text: "", printed: false, done: false };
    this.cards.push(card);
    if (this.cards.length > 60) this.cards.splice(0, 20);
    this.scroll();
    return card;
  }

  private cardOf(agentId: unknown, includeDone = false): Card | undefined {
    for (let i = this.cards.length - 1; i >= 0; i--) if (this.cards[i].agentId === agentId && (includeDone || !this.cards[i].done)) return this.cards[i];
    return undefined;
  }

  private verdict(c: Card, ok: boolean, n: number): void {
    c.verdict.textContent = ok ? `Verified · ${n} figure${n === 1 ? "" : "s"}` : `Unverified · ${n} figure${n === 1 ? "" : "s"}`;
    c.verdict.classList.toggle("bad", !ok);
    this.scroll();
  }

  private render(el: HTMLElement, text: string): void {
    el.innerHTML = "";
    for (const part of text.split(CITE)) {
      if (!part) continue;
      if (CITE.test(part)) {
        for (const id of part.match(/E\d+/g) ?? []) {
          const s = document.createElement("span");
          s.className = "chip";
          s.dataset.id = id;
          s.textContent = id;
          el.appendChild(s);
        }
      } else el.appendChild(document.createTextNode(part));
    }
  }

  private fill(c: Card): void {
    this.render(c.txt, c.text);
    c.printed = true;
    this.scroll();
  }

  /* Mirrors the room's word printer; the bars run while the words land when the answer has no audio. */
  printWord(agentId: string, text: string): void {
    const c = this.cardOf(agentId);
    if (!c) return;
    if (!c.hasAudio) this.setSpeaking(agentId);
    c.printed = true;
    this.render(c.txt, text);
    this.scroll();
  }

  /* Also lands after Escape, when the release has already closed the card: the transcript keeps the
     whole answer that was generated and verified, even though its playback was cut off. */
  answerDone(agentId: string): void {
    const c = this.cardOf(agentId, true);
    if (!c) return;
    this.render(c.txt, c.text || c.txt.textContent || "");
    c.printed = true;
    if (!c.hasAudio) this.setSpeaking(null);
    this.scroll();
  }

  setSpeaking(agentId: string | null): void {
    for (const c of this.cards) c.el.classList.remove("speaking");
    if (!agentId) return;
    const c = this.cardOf(agentId);
    if (c) c.el.classList.add("speaking");
  }

  // ---------------------------------------------------------------- seats

  seatHover(agentId: string | null, x: number, y: number, side: Side, variances: any[]): void {
    const pop = $("popover");
    if (!agentId) {
      pop.classList.remove("open");
      return;
    }
    const lines = variances.slice(0, 2).map((v) => `<div class="line${v.bad ? " bad" : ""}">${esc(String(v.account ?? "").replace(/^\d+-/, ""))}  ${esc(usdK(Number(v.delta_cents ?? 0)))}  ${esc(pct(v.delta_pct))}</div>`).join("");
    pop.innerHTML = `<div class="name">${esc(DISPLAY[agentId] ?? agentId)}</div>${lines || '<div class="line">no ranked variance</div>'}<div class="hint">${this.net.backend ? "Click to ask what changed" : "NET DOWN · cannot ask"}</div>`;
    pop.classList.add("open");
    const w = pop.offsetWidth;
    const h = pop.offsetHeight;
    const [cx, cy, cw, ch] = this.centerRect();
    let left = side === "left" ? x + 16 : side === "right" ? x - w - 16 : x - w / 2;
    let top = side === "far" ? y + 24 : y - h / 2;
    left = Math.max(cx + 8, Math.min(cx + cw - w - 8, left));
    top = Math.max(cy + 8, Math.min(cy + ch - h - 8, top));
    pop.style.left = `${Math.round(left / 4) * 4}px`;
    pop.style.top = `${Math.round(top / 4) * 4}px`;
  }

  // ---------------------------------------------------------------- evidence drawer

  drawerOpen(): boolean {
    return $("drawer").classList.contains("open");
  }

  evidenceRow(id: string): any | undefined {
    return this.evidence.get(id);
  }

  openEvidence(row: any): void {
    const body = $("drawer-body");
    $("drawer-title").textContent = `${row.id ?? ""}  ${String(row.kind ?? "").toUpperCase()}  ${row.target ?? ""}`.trim();
    const figs = Object.entries(row.figures ?? {})
      .map(([k, v]) => {
        let s: string;
        if (v == null || v === "") s = "—";
        else if (Array.isArray(v)) s = v.join(", ");
        else if (typeof v === "number" && (k.endsWith("_cents") || k === "prior" || k === "current")) s = usd(v);
        else if (typeof v === "number" && (k.endsWith("_pct") || k === "pct" || k === "share")) s = `${v}%`;
        else s = String(v);
        return `<span class="k">${esc(k.toUpperCase())}</span><span>${esc(s)}</span>`;
      })
      .join("");
    const ids: string[] = row.txn_ids ?? [];
    const shown = ids.slice(0, 80);
    const trs = shown
      .map((id) => {
        const t = txn(id);
        return `<tr title="${esc(id)}"><td>${esc(t?.date ?? "—")}</td><td>${esc(t?.customer || t?.vendor || "—")}</td><td class="amt">${t ? esc(usd(t.amount_cents)) : "—"}</td></tr>`;
      })
      .join("");
    body.innerHTML =
      `<div class="statement"></div>` +
      (figs ? `<div class="figs">${figs}</div>` : "") +
      `<div class="sub">TRANSACTIONS · ${ids.length}</div>` +
      (ids.length ? `<table><colgroup><col style="width:96px"><col><col style="width:124px"></colgroup><thead><tr><th>DATE</th><th>CUSTOMER / VENDOR</th><th class="amt">AMOUNT</th></tr></thead><tbody>${trs}</tbody></table>` : "") +
      (ids.length > shown.length ? `<div class="more">+${ids.length - shown.length} more</div>` : "");
    body.querySelector(".statement")!.textContent = String(row.statement ?? "");
    body.scrollTop = 0;
    $("drawer").classList.add("open");
  }

  closeDrawer(): void {
    $("drawer").classList.remove("open");
  }

  // ---------------------------------------------------------------- prove modal

  toggleProve(data: any): void {
    if (this.proveOpen) {
      this.closeProve();
      return;
    }
    const v1 = data?.v1 ?? {};
    const v2 = data?.v2 ?? {};
    const num = (v: unknown) => (v == null ? "—" : String(v));
    const rows: Array<[string, string, string, boolean]> = [
      ["Violations", String((v1.violations ?? []).length), String((v2.violations ?? []).length), true],
      ["Misreports", num(v1.misreports), num(v2.misreports), true],
      ["Escalations", num(v1.escalations), num(v2.escalations), true],
      ["Seconds to detect", num(v1.seconds_to_detect), num(v2.seconds_to_detect), true],
      ["Payroll status", num(v1.payroll_status), num(v2.payroll_status), false],
    ];
    const cell = (v: string, numeric: boolean, bad: boolean) => `<td class="v${numeric ? "" : " word"}${bad ? " bad" : ""}">${esc(v)}</td>`;
    const table = rows.map(([k, a, b, numeric]) => `<tr><td>${esc(k)}</td>${cell(a, numeric, k === "Violations" && a !== "0" && a !== "—")}${cell(b, numeric, k === "Violations" && b !== "0" && b !== "—")}</tr>`).join("");
    const remed = typeof data?.remediation === "string" && data.remediation ? `<div class="remed"><div class="k">REMEDIATION</div><div></div></div>` : "";
    const href = this.prism.session ?? this.prism.project ?? this.prism.host;
    const el = $("prove-modal");
    el.innerHTML =
      `<h2>Prove<small>GET /prove</small></h2>` +
      `<table><thead><tr><th></th><th>V1 · ${esc(v1.session_id ?? "mandate-v1-01")}</th><th>V2 · ${esc(v2.session_id ?? "mandate-v2-01")}</th></tr></thead><tbody>${table}</tbody></table>` +
      remed +
      `<div class="links"><a href="${esc(href)}" target="_blank" rel="noopener">Open ${esc(v1.session_id ?? "mandate-v1-01")} in PRISM${ARROW}</a><a href="${esc(href)}" target="_blank" rel="noopener">Open ${esc(v2.session_id ?? "mandate-v2-01")} in PRISM${ARROW}</a></div>`;
    if (remed) el.querySelector(".remed div:last-child")!.textContent = data.remediation;
    el.classList.add("open");
    $("scrim").classList.add("open");
    this.proveOpen = true;
  }

  closeProve(): void {
    $("prove-modal").classList.remove("open");
    $("scrim").classList.remove("open");
    this.proveOpen = false;
  }

  closeAll(): void {
    this.closeDrawer();
    this.closeProve();
    $("popover").classList.remove("open");
  }

  anyOpen(): boolean {
    return this.drawerOpen() || this.proveOpen;
  }
}
