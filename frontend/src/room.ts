import { Container, Graphics, Text, TextStyle, type Application } from "pixi.js";
import gsap from "gsap";
import { COLOR, CSS, FONT, MOTION, ROOM, snap } from "./tokens";
import type { BusEvent } from "./ws";

const mono = (size: number, fill: number) => new TextStyle({ fontFamily: FONT.mono, fontSize: size, fill, letterSpacing: size * 0.04 });

export type Side = "left" | "right" | "far";
const LEFT = ["treasury", "ap_east", "ap_west", "procurement", "payroll"]; // near → far
const FAR = ["tax", "saas_renewals"]; // left → right
const RIGHT = ["expenses", "controller_a", "controller_b", "collections", "fx"]; // far → near
export const DISPLAY: Record<string, string> = { treasury: "Treasury", ap_east: "AP East", ap_west: "AP West", procurement: "Procurement", payroll: "Payroll", tax: "Tax", saas_renewals: "Renewals", expenses: "Expenses", controller_a: "Controller A", controller_b: "Controller B", collections: "Collections", fx: "FX" };

export interface RoomHooks {
  onWord?: (agentId: string, text: string) => void;
  onAnswerDone?: (agentId: string) => void;
  onSeatHover?: (agentId: string | null, x: number, y: number, side: Side) => void;
  onSeatTap?: (agentId: string) => void;
}
const ROW_TOKENS = 8;
const ROW_POOL = 10;
const VERDICT_FONT = 10; // the PRISM verdict appended to the RECORDED line

interface Seat {
  id: string;
  side: Side;
  depth: number; // 0 near … 4 far (far edge = 4)
  x: number;
  y: number;
  nx: number; // outward normal
  ny: number;
  group: Container;
  tick: Graphics;
  label: Text;
  papers: { tick: Graphics; tag: Text; data?: any }[];
}

interface Row {
  group: Container;
  tokens: Text[];
  rule: Graphics;
  tick: Graphics;
  underline: Graphics;
  data?: { row_id: string; text: string; figures: string[] };
  width: number;
}

const wait = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
const clean = (w: string) => w.replace(/^[^\w$]+|[^\w%.]+$/g, "").replace(/\.+$/, "");

export class Room {
  root = new Container();
  private W = 1920;
  private H = 1080;
  private tableG = new Graphics();
  private lineG = new Graphics();
  private crossG = new Graphics();
  private cfoTick = new Graphics();
  private cfo!: Text;
  private recG = new Graphics();
  private listening!: Text;
  private seats = new Map<string, Seat>();
  private agenda = new Container();
  private agendaLines: Text[] = [];
  private agendaRule = new Graphics();
  private footer!: Text;
  private learned!: Text;
  private rows: Row[] = [];
  private flagged!: Text;
  private flaggedUnderline = new Graphics();
  private recorded!: Text;
  private verdictA!: Text;
  private verdictB!: Text;
  private traceId: string | null = null;
  private pendingVerdicts = new Map<string, any>();
  private stackGroup = new Container();
  private q: HTMLDivElement;
  private a: HTMLDivElement;
  private evidence = new Map<string, any>();
  private runIndex = 1;
  private mode = "v2";
  private previous: { id: string; citations: string[] } | null = null;
  private retrieved: Row[] = [];
  private queue: Promise<void> = Promise.resolve();
  private shown = false;
  private corners = { tl: [0, 0], tr: [0, 0], bl: [0, 0], br: [0, 0] } as Record<string, [number, number]>;
  private ox = 0;
  private oy = 0;
  private viewport: () => [number, number, number, number] = () => [0, 0, window.innerWidth, window.innerHeight];

  constructor(private app: Application, private onEvidence: (id: string) => void, private hooks: RoomHooks = {}) {
    this.q = this.dom("room-q", `position:absolute;font:${ROOM.qFont}px ${FONT.sans};color:${CSS.muted};line-height:1.4;text-align:center;width:640px;pointer-events:none;`);
    this.a = this.dom("room-a", `position:absolute;font:${ROOM.aFont}px ${FONT.sans};color:${CSS.text};line-height:1.4;width:360px;max-height:4.2em;overflow:hidden;pointer-events:auto;`);
    this.root.visible = false;
    this.build();
    this.app.stage.addChild(this.root);
    this.relayout();
    window.addEventListener("resize", () => this.relayout());
  }

  /* The room draws inside the rectangle the provider returns (page px); the shell owns the rest. */
  setViewportProvider(fn: () => [number, number, number, number]): void {
    this.viewport = fn;
    this.relayout();
  }

  relayout(): void {
    const [x, y, w, h] = this.viewport();
    this.ox = snap(x);
    this.oy = snap(y);
    this.root.position.set(this.ox, this.oy);
    this.layout(w, h);
  }

  seatVariances(agentId: string): any[] {
    return (this.seats.get(agentId)?.papers ?? []).map((p) => p.data).filter(Boolean);
  }

  private dom(id: string, style: string): HTMLDivElement {
    const el = document.createElement("div");
    el.id = id;
    el.setAttribute("style", style + "display:none;");
    document.getElementById("ui")!.appendChild(el);
    return el;
  }

  // ---------------------------------------------------------------- build (once)

  private build(): void {
    this.root.addChild(this.tableG, this.lineG, this.crossG, this.cfoTick, this.recG);
    this.cfo = new Text({ text: "CFO", style: mono(13, COLOR.text) });
    this.cfo.anchor.set(0.5, 0.5);
    this.listening = new Text({ text: "LISTENING", style: mono(ROOM.rowFont, COLOR.muted) });
    this.listening.anchor.set(0.5, 0);
    this.listening.visible = false;
    this.recG.visible = false;
    this.root.addChild(this.cfo, this.listening);
    const seatSpec: Array<[string, Side, number]> = [
      ...LEFT.map((id, i): [string, Side, number] => [id, "left", i]),
      ...FAR.map((id, i): [string, Side, number] => [id, "far", i]),
      ...RIGHT.map((id, i): [string, Side, number] => [id, "right", 4 - i]),
    ];
    for (const [id, side, depth] of seatSpec) {
      const group = new Container();
      const tick = new Graphics();
      const label = new Text({ text: DISPLAY[id].toUpperCase(), style: mono(ROOM.fontFar, COLOR.muted) });
      label.eventMode = "static";
      label.cursor = "pointer";
      const papers: Seat["papers"] = [0, 1].map(() => {
        const t = new Graphics();
        const tag = new Text({ text: "", style: mono(ROOM.rowFont, COLOR.muted) });
        tag.eventMode = "static";
        tag.cursor = "pointer";
        t.visible = tag.visible = false;
        group.addChild(t, tag);
        return { tick: t, tag };
      });
      group.addChild(tick, label);
      this.root.addChild(group);
      const seat: Seat = { id, side, depth, x: 0, y: 0, nx: 0, ny: 0, group, tick, label, papers };
      papers.forEach((p) => p.tag.on("pointertap", () => p.data?.evidence_id && this.onEvidence(p.data.evidence_id)));
      label.on("pointerover", () => this.hooks.onSeatHover?.(id, this.ox + seat.x, this.oy + seat.y, side));
      label.on("pointerout", () => this.hooks.onSeatHover?.(null, 0, 0, side));
      label.on("pointertap", () => this.hooks.onSeatTap?.(id));
      this.seats.set(id, seat);
    }
    for (let i = 0; i < 6; i++) {
      const t = new Text({ text: "", style: i === 1 ? mono(ROOM.headFont, COLOR.text) : mono(ROOM.rowFont, COLOR.muted) });
      this.agendaLines.push(t);
      this.agenda.addChild(t);
    }
    this.footer = this.agendaLines[5];
    this.learned = new Text({ text: "", style: mono(ROOM.rowFont, COLOR.muted) });
    this.learned.style.wordWrap = true;
    this.learned.style.lineHeight = 16;
    this.learned.visible = false;
    this.agenda.addChild(this.agendaRule, this.learned);
    this.root.addChild(this.agenda);
    for (let i = 0; i < ROW_POOL; i++) {
      const group = new Container();
      const tokens: Text[] = [];
      for (let k = 0; k < ROW_TOKENS; k++) {
        const t = new Text({ text: "", style: mono(ROOM.rowFont, COLOR.muted) });
        t.eventMode = "static";
        t.cursor = "pointer";
        tokens.push(t);
        group.addChild(t);
      }
      const rule = new Graphics();
      const tick = new Graphics();
      const underline = new Graphics();
      group.addChild(rule, tick, underline);
      group.visible = false;
      const row: Row = { group, tokens, rule, tick, underline, width: 0 };
      tokens.forEach((t) => t.on("pointertap", () => row.data && this.onEvidence(row.data.row_id)));
      this.rows.push(row);
      this.stackGroup.addChild(group);
    }
    this.flagged = new Text({ text: "", style: mono(ROOM.rowFont, COLOR.text) });
    this.flagged.visible = false;
    this.recorded = new Text({ text: "", style: mono(ROOM.rowFont, COLOR.muted) });
    this.recorded.visible = false;
    this.verdictA = new Text({ text: "", style: mono(VERDICT_FONT,COLOR.muted) });
    this.verdictB = new Text({ text: "", style: mono(VERDICT_FONT,COLOR.text) });
    this.verdictA.visible = this.verdictB.visible = false;
    this.stackGroup.addChild(this.flagged, this.flaggedUnderline, this.recorded, this.verdictA, this.verdictB);
    this.root.addChild(this.stackGroup);
  }

  // ---------------------------------------------------------------- layout (on resize only)

  layout(W: number, H: number): void {
    this.W = W;
    this.H = H;
    const cx = snap(W / 2);
    const topY = snap(ROOM.topY * H);
    const botY = snap(ROOM.botY * H);
    const topW = snap(ROOM.topW * W);
    const botW = snap(ROOM.botW * W);
    const tl: [number, number] = [cx - topW / 2, topY];
    const tr: [number, number] = [cx + topW / 2, topY];
    const bl: [number, number] = [cx - botW / 2, botY];
    const br: [number, number] = [cx + botW / 2, botY];
    this.corners = { tl, tr, bl, br };
    this.tableG.clear();
    this.tableG.moveTo(tl[0], tl[1]).lineTo(tr[0], tr[1]).lineTo(br[0], br[1]).lineTo(bl[0], bl[1]).closePath().stroke({ width: 1, color: COLOR.rule });
    const place = (seat: Seat, x: number, y: number, nx: number, ny: number, size: number) => {
      seat.x = snap(x);
      seat.y = snap(y);
      seat.nx = nx;
      seat.ny = ny;
      seat.tick.clear();
      seat.tick.moveTo(seat.x - nx * ROOM.seatTick / 2, seat.y - ny * ROOM.seatTick / 2).lineTo(seat.x + nx * ROOM.seatTick / 2, seat.y + ny * ROOM.seatTick / 2).stroke({ width: 1, color: COLOR.rule });
      seat.label.style.fontSize = size;
      seat.label.style.letterSpacing = size * 0.04;
      if (seat.side === "far") {
        seat.label.anchor.set(0.5, 1);
        seat.label.position.set(seat.x, snap(seat.y - ROOM.labelGap));
      } else {
        seat.label.anchor.set(seat.side === "left" ? 1 : 0, 0.5);
        seat.label.position.set(snap(seat.x + nx * ROOM.labelGap), seat.y);
      }
    };
    for (const seat of this.seats.values()) {
      const size = seat.side === "far" ? ROOM.fontFar : seat.depth === 0 ? ROOM.fontNear : seat.depth <= 2 ? ROOM.fontMid : ROOM.fontFar;
      if (seat.side === "left" || seat.side === "right") {
        const [ax, ay] = seat.side === "left" ? bl : br;
        const [bx, by] = seat.side === "left" ? tl : tr;
        const t = (seat.depth + 0.5) / 5;
        const x = ax + (bx - ax) * t;
        const y = ay + (by - ay) * t;
        const dx = bx - ax;
        const dy = by - ay;
        const len = Math.hypot(dx, dy) || 1;
        const nx = seat.side === "left" ? dy / len : -dy / len;
        const ny = seat.side === "left" ? -dx / len : dx / len;
        place(seat, x, y, nx, ny, size);
      } else {
        const t = (seat.depth + 0.5) / 2;
        place(seat, tl[0] + (tr[0] - tl[0]) * t, topY, 0, -1, size);
      }
      this.layoutPapers(seat);
    }
    this.cfo.position.set(cx, snap(ROOM.cfoY * H));
    this.cfoTick.clear();
    this.cfoTick.rect(cx, botY - ROOM.seatTick / 2, 1, ROOM.seatTick).fill(COLOR.rule);
    this.recG.clear();
    this.recG.rect(cx - 24, snap(ROOM.cfoY * H) + 12, 48, 2).fill(COLOR.red);
    this.listening.position.set(cx, snap(ROOM.cfoY * H) + 18);
    this.q.style.left = `${this.ox + cx - 320}px`;
    this.q.style.top = `${this.oy + snap(ROOM.cfoY * H) + 32}px`;
    this.layoutAgenda();
    this.lineG.clear();
    this.crossG.clear();
  }

  private layoutPapers(seat: Seat): void {
    const inward = -1;
    seat.papers.forEach((p, i) => {
      if (!p.data) {
        p.tick.visible = p.tag.visible = false;
        return;
      }
      const len = snap(ROOM.paperMin + Math.min(1, Math.abs(p.data.delta_cents) / 100_000_000) * (ROOM.paperMax - ROOM.paperMin));
      const color = p.data.bad ? COLOR.amber : COLOR.muted;
      const y = snap(seat.y + (seat.side === "far" ? 16 + i * 16 : (i - 0.5) * 16));
      let x0: number;
      if (seat.side === "left") x0 = snap(seat.x + seat.nx * inward * 14 + 4);
      else if (seat.side === "right") x0 = snap(seat.x + seat.nx * inward * 14 - len - 4);
      else x0 = snap(seat.x - len / 2);
      p.tick.clear();
      p.tick.rect(x0, y, len, 1).fill(color);
      p.tag.text = `${String(p.data.account).replace(/^\d+-/, "")}  ${p.data.delta_pct >= 0 ? "+" : ""}${Number(p.data.delta_pct).toFixed(0)}%`;
      p.tag.style.fill = color;
      p.tag.anchor.set(seat.side === "right" ? 1 : 0, 0.5);
      p.tag.position.set(seat.side === "right" ? x0 - 6 : x0 + len + 6, y);
      p.tick.visible = p.tag.visible = true;
    });
  }

  private layoutAgenda(): void {
    const cx = this.W / 2;
    const lines = this.agendaLines;
    let maxW = 0;
    for (const t of lines) maxW = Math.max(maxW, t.width);
    const x = snap(cx - maxW / 2);
    const pitch = [16, 24, 16, 16, 16, 16];
    let y = snap(ROOM.agendaY * this.H - 56);
    lines.forEach((t, i) => {
      t.position.set(x, y);
      y += pitch[i];
      if (i === 4) {
        this.agendaRule.clear();
        this.agendaRule.rect(x, y + 2, Math.max(maxW, 160), 1).fill(COLOR.rule);
        y += 8;
      }
    });
    this.learned.position.set(x, y + 16);
    this.learned.style.wordWrapWidth = snap(Math.min(560, this.W * 0.29));
  }

  // ---------------------------------------------------------------- state

  setVisible(v: boolean): void {
    this.shown = v;
    this.root.visible = v;
    this.q.style.display = v && this.q.textContent ? "block" : "none";
    this.a.style.display = v && this.a.textContent ? "block" : "none";
  }

  setRecording(on: boolean): void {
    this.recG.visible = on && this.shown;
    this.listening.visible = on && this.shown;
  }

  setEvidence(json: any): void {
    this.evidence.clear();
    for (const r of json?.evidence ?? []) this.evidence.set(r.id, r);
  }

  evidenceRow(id: string): any | undefined {
    return this.evidence.get(id);
  }

  private footerText(): string {
    return `RUN ${this.mode === "v1" ? 1 : 2} · ${this.mode === "v1" ? "FREEFORM" : "GROUNDED"}`;
  }

  setRun(index: number, mode: string): void {
    this.runIndex = index;
    this.mode = mode;
    this.footer.text = this.footerText();
    gsap.to(this.flaggedUnderline, { alpha: 0, duration: MOTION.fast, ease: MOTION.ease });
    for (const r of this.rows) gsap.to(r.underline, { alpha: 0, duration: MOTION.fast, ease: MOTION.ease });
  }

  private loadMeeting(pl: any): void {
    const p = pl.periods ?? {};
    const mon = (s: string) => ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"][Number(String(s).slice(5, 7)) - 1] ?? s;
    const pct = (v: number | null | undefined) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${Number(v).toFixed(0)}%`);
    const usdK = (c: number) => {
      const d = Math.abs(c) / 100;
      const s = d >= 1_000_000 ? `$${(d / 1_000_000).toFixed(2)}M` : d >= 1000 ? `$${Math.round(d / 1000)}K` : `$${d.toFixed(0)}`;
      return (c < 0 ? "-" : "+") + s;
    };
    this.agendaLines[0].text = `${mon(p.current)} vs ${mon(p.prior)}`;
    this.agendaLines[1].text = `REVENUE ${pct(pl.headline?.revenue_pct)}  EXPENSES ${pct(pl.headline?.expense_pct)}`;
    (pl.top_variances ?? []).slice(0, 3).forEach((v: any, i: number) => {
      this.agendaLines[2 + i].text = `${String(v.account).replace(/^\d+-/, "").replace(/-/g, "_")}  ${usdK(v.delta_cents)}  ${pct(v.delta_pct)}  [${v.evidence_id ?? "—"}]`;
    });
    this.mode = pl.mode ?? this.mode;
    this.runIndex = pl.run_index ?? this.runIndex;
    this.footer.text = this.footerText();
    for (const seat of this.seats.values()) {
      const items = (pl.owners ?? {})[seat.id] ?? [];
      seat.papers.forEach((pp, i) => (pp.data = items[i]));
      this.layoutPapers(seat);
    }
    this.layoutAgenda();
  }

  // ---------------------------------------------------------------- events

  handle(e: BusEvent): void {
    const pl = e.payload ?? {};
    switch (e.type) {
      case "meeting.loaded":
        this.loadMeeting(pl);
        break;
      case "agent.addressed":
        this.enqueue(() => this.stageAddressed(pl.agent_id, pl.question ?? ""));
        break;
      case "agent.retrieving":
        this.enqueue(() => this.stageRetrieving(pl.agent_id, pl.rows ?? []));
        break;
      case "agent.verified":
        this.enqueue(() => this.stageVerified(pl.agent_id, pl.checks ?? []));
        break;
      case "agent.traced":
        this.enqueue(() => this.stageTraced(pl.session ?? "", pl.trace_id ?? null));
        break;
      case "prism.verdict":
        this.onVerdict(pl);
        break;
      case "agent.answer":
        this.enqueue(() => this.stageAnswer(pl.agent_id, pl.text ?? "", Number(pl.duration_ms ?? 0), pl.citations ?? []));
        break;
      case "agent.released":
        this.enqueue(() => this.stageReleased());
        break;
      case "meeting.learned":
        this.enqueue(() => this.stageLearned(pl.line ?? ""));
        break;
      case "desk.answer":
      case "query.answer":
        this.printQuestion(pl.text ?? "", 6000);
        break;
    }
  }

  private enqueue(stage: () => Promise<void>): void {
    this.queue = this.queue.then(stage).catch((err) => console.error("room stage", err));
  }

  private tween(target: object, vars: gsap.TweenVars): Promise<void> {
    return new Promise((resolve) => gsap.to(target, { ...vars, ease: MOTION.ease, onComplete: resolve }));
  }

  private printQuestion(text: string, clearAfter = 0): void {
    this.q.textContent = text;
    this.q.style.display = this.shown && text ? "block" : "none";
    if (clearAfter) window.setTimeout(() => this.q.textContent === text && ((this.q.textContent = ""), (this.q.style.display = "none")), clearAfter);
  }

  private async stageAddressed(agentId: string, question: string): Promise<void> {
    const seat = this.seats.get(agentId);
    if (!seat) return;
    this.printQuestion(question);
    const cx = snap(this.W / 2);
    const from: [number, number] = [cx, this.corners.bl[1]];
    const state = { t: 0 };
    const draw = () => {
      this.lineG.clear();
      this.lineG.moveTo(from[0], from[1]).lineTo(from[0] + (seat.x - from[0]) * state.t, from[1] + (seat.y - from[1]) * state.t).stroke({ width: 1, color: COLOR.muted });
    };
    const line = this.tween(state, { t: 1, duration: MOTION.slow, onUpdate: draw });
    seat.label.style.fill = COLOR.text;
    for (const p of seat.papers) if (p.data) p.tag.style.fill = p.data.bad ? COLOR.amber : COLOR.text;
    const dims: Promise<void>[] = [];
    for (const s of this.seats.values()) {
      if (s.id === agentId) dims.push(this.tween(s.group, { alpha: 1, duration: MOTION.fast }));
      else if (this.previous && s.id === this.previous.id) dims.push(this.tween(s.group, { alpha: ROOM.previous, duration: MOTION.fast }));
      else dims.push(this.tween(s.group, { alpha: ROOM.dim, duration: MOTION.fast }));
    }
    dims.push(this.tween(this.agenda, { alpha: ROOM.dim, duration: MOTION.fast }));
    await Promise.all([line, ...dims]);
  }

  private tableEdge(y: number, side: "left" | "right"): number {
    const [ax, ay] = side === "left" ? this.corners.tl : this.corners.tr;
    const [bx, by] = side === "left" ? this.corners.bl : this.corners.br;
    const t = by === ay ? 0 : Math.max(0, Math.min(1, (y - ay) / (by - ay)));
    return ax + (bx - ax) * t;
  }

  /* Everything inside the table that a stack must not cover: paper tags and the agenda, as [x0, x1, y0, y1]. */
  private obstacles(): Array<[number, number, number, number]> {
    const out: Array<[number, number, number, number]> = [];
    for (const s of this.seats.values()) {
      for (const p of s.papers) {
        if (!p.tag.visible) continue;
        const w = p.tag.width;
        const x0 = s.side === "right" ? p.tag.x - w : s.side === "far" ? p.tag.x - w / 2 : p.tag.x;
        out.push([x0, x0 + w, p.tag.y - 8, p.tag.y + 8]);
      }
    }
    let aw = 0;
    for (const t of this.agendaLines) aw = Math.max(aw, t.width);
    const ay1 = this.learned.visible ? this.learned.y + this.learned.height : this.footer.y + 16;
    out.push([this.agendaLines[0].x, this.agendaLines[0].x + aw, this.agendaLines[0].y, ay1]);
    return out;
  }

  /* The stack sits in front of the seat where it fits best: above the seat's paper row, level with it, or below.
     Returns the origin and the scale needed to clear the obstacles (never under 0.8). */
  private stackOrigin(seat: Seat, rowW: number, n: number): [number, number, number] {
    const stackH = n * ROOM.rowPitch + 40; // rows plus the flagged and RECORDED lines
    if (seat.side === "far") return [snap(seat.x - rowW / 2), snap(seat.y + 40), 1];
    const obs = this.obstacles();
    const cx = this.W / 2;
    const topY = this.corners.tl[1];
    const botY = this.corners.bl[1];
    const candidates: Array<[number, number]> = [
      [snap(seat.y - 20 - stackH), seat.depth <= 2 ? 0.1 : 0],
      [snap(seat.y - (n * ROOM.rowPitch) / 2), 0],
      [snap(seat.y + 24), seat.depth >= 3 ? 0.1 : 0],
    ];
    let best: [number, number, number] | null = null;
    let bestScore = -1;
    for (const [oy, pref] of candidates) {
      const y0 = oy;
      const y1 = oy + stackH;
      if (y0 < topY + 8 || y1 > botY - 8) continue; // stays inside the table
      const ym = (y0 + y1) / 2;
      // obstacles left of centre push the start right; obstacles right of centre cap the end
      let xs = this.tableEdge(ym, "left") + 24;
      let limit = this.tableEdge(ym, "right") - 8;
      if (seat.side === "left") xs = Math.max(xs, seat.x + 24);
      for (const [x0, x1, oy0, oy1] of obs) {
        if (oy1 < y0 || oy0 > y1) continue;
        if ((x0 + x1) / 2 < cx) xs = Math.max(xs, x1 + 16);
        else limit = Math.min(limit, x0 - 16);
      }
      const avail = limit - xs;
      const scale = Math.max(0.8, Math.min(1, avail / rowW));
      const score = Math.min(1, avail / rowW) + pref;
      if (score > bestScore) {
        bestScore = score;
        const x = seat.side === "right" && avail >= rowW ? Math.max(xs, Math.min(seat.x - 24 - rowW, limit - rowW)) : xs;
        best = [snap(x), oy, scale];
      }
    }
    return best ?? [snap(seat.side === "left" ? seat.x + 24 : seat.x - 24 - rowW), snap(seat.y - (n * ROOM.rowPitch) / 2), 0.8];
  }

  private fillRow(row: Row, data: any, index: number): void {
    row.data = { row_id: data.row_id, text: data.text, figures: data.figures ?? [] };
    const parts = [`[${data.row_id}]`, ...String(data.text).split(/\s{2,}/)].slice(0, ROW_TOKENS);
    let x = 0;
    row.tokens.forEach((t, k) => {
      t.text = parts[k] ?? "";
      t.style.fill = COLOR.muted;
      t.visible = k < parts.length;
      t.position.set(x, 0);
      if (k < parts.length) x += t.width + 12;
    });
    row.width = snap(x - 12);
    row.rule.clear();
    row.rule.rect(0, ROOM.rowPitch - 3, row.width, 1).fill(COLOR.rule);
    row.tick.clear();
    row.underline.clear();
    row.tick.alpha = row.underline.alpha = 1;
    row.group.visible = true;
    row.group.alpha = 0;
    row.group.scale.set(1);
    row.group.position.set(this.agendaLines[2].x, this.agendaLines[2].y + index * ROOM.rowPitch);
  }

  private async stageRetrieving(agentId: string, rows: any[]): Promise<void> {
    const seat = this.seats.get(agentId);
    if (!seat) return;
    this.retrieved = [];
    const n = Math.min(rows.length, ROW_POOL);
    for (let i = 0; i < n; i++) this.fillRow(this.rows[i], rows[i], i);
    const rowW = Math.max(...this.rows.slice(0, n).map((r) => r.width), 160);
    const [ox, oy, scale] = this.stackOrigin(seat, rowW, n);
    const pitch = ROOM.rowPitch * scale;
    const moves: Promise<void>[] = [];
    for (let i = 0; i < n; i++) {
      const row = this.rows[i];
      this.retrieved.push(row);
      row.group.scale.set(scale);
      moves.push(this.tween(row.group, { x: ox, y: snap(oy + i * pitch, 1), alpha: 1, duration: MOTION.fast, delay: i * MOTION.stagger }));
    }
    this.flagged.visible = this.recorded.visible = false;
    this.flaggedUnderline.clear();
    this.flagged.position.set(ox, snap(oy + n * pitch + 8));
    this.recorded.position.set(ox, snap(oy + n * pitch + 8));
    await Promise.all(moves);
  }

  private async stageVerified(_agentId: string, checks: any[]): Promise<void> {
    const rowById = new Map(this.retrieved.map((r) => [r.data?.row_id, r]));
    const bad: string[] = [];
    const pause = Math.min(120, Math.floor(1200 / Math.max(1, checks.length)));
    for (const c of checks) {
      const row = rowById.get(c.row_id);
      if (c.ok) {
        if (row) {
          row.tick.clear();
          row.tick.rect(row.width + 8, 4, 1, 6).fill(COLOR.muted);
          this.click();
        }
      } else {
        bad.push(c.figure);
        const tok = row?.tokens.find((t) => t.visible && clean(t.text) === clean(String(c.figure)));
        if (tok && row) {
          row.underline.rect(tok.x, 12, tok.width, 1).fill(COLOR.red);
        }
      }
      await wait(pause);
    }
    if (bad.length) {
      this.flagged.text = bad.join("   ");
      this.flagged.visible = true;
      this.flaggedUnderline.clear();
      this.flaggedUnderline.alpha = 1;
      this.flaggedUnderline.rect(this.flagged.x, this.flagged.y + 12, this.flagged.width, 1).fill(COLOR.red);
      this.recorded.position.set(this.flagged.x, this.flagged.y + 20);
    }
  }

  private click(): void {
    try {
      const a = new Audio("/audio/tick.wav");
      a.play().catch(() => undefined);
    } catch {
      /* absent */
    }
  }

  private async stageTraced(session: string, traceId: string | null): Promise<void> {
    this.recorded.text = `RECORDED · PRISM · ${session}`;
    this.recorded.alpha = 0;
    this.recorded.visible = true;
    this.traceId = traceId;
    this.verdictA.visible = this.verdictB.visible = false;
    await this.tween(this.recorded, { alpha: 1, duration: MOTION.fast });
    const pending = traceId ? this.pendingVerdicts.get(traceId) : undefined;
    if (pending) {
      this.pendingVerdicts.delete(traceId!);
      this.showVerdict(pending);
    }
  }

  /* PRISM's own reading of the trace, appended to the RECORDED line. verify.py gated the sentence before speech;
     this is the independent record and auditor, never the thing that blocked it. */
  private onVerdict(pl: any): void {
    const id = pl.trace_id ?? null;
    if (id && this.recorded.visible && this.traceId === id) {
      this.showVerdict(pl);
      return;
    }
    if (id) {
      this.pendingVerdicts.set(id, pl);
      if (this.pendingVerdicts.size > 20) this.pendingVerdicts.delete(this.pendingVerdicts.keys().next().value!);
    }
  }

  private showVerdict(pl: any): void {
    let a = "";
    let b = "";
    if (pl.status === "scored" && pl.flagged) {
      a = "PRISM · ";
      b = "FLAGGED";
    } else if (pl.status === "scored") a = `PRISM · SCORED ${Math.round(Number(pl.score ?? 0))} · NO FLAG`;
    else if (pl.status === "recorded") a = `PRISM · RECORDED · ${String(pl.trace_id ?? "").slice(0, 8)}`;
    else a = "PRISM · NOT RECORDED";
    const x = snap(this.recorded.x + this.recorded.width + 16);
    this.verdictA.text = a;
    this.verdictA.position.set(x, this.recorded.y + 1);
    this.verdictB.text = b;
    this.verdictB.position.set(snap(x + this.verdictA.width, 1), this.recorded.y + 1);
    this.verdictA.alpha = this.verdictB.alpha = 0;
    this.verdictA.visible = true;
    this.verdictB.visible = b.length > 0;
    void this.tween(this.verdictA, { alpha: 1, duration: MOTION.fast });
    if (b) void this.tween(this.verdictB, { alpha: 1, duration: MOTION.fast });
  }

  private async stageAnswer(agentId: string, text: string, durationMs: number, citations: string[]): Promise<void> {
    const seat = this.seats.get(agentId);
    if (!seat) return;
    if (this.previous && this.previous.id !== agentId && this.previous.citations.some((c) => citations.includes(c))) {
      const p = this.seats.get(this.previous.id)!;
      this.crossG.clear();
      this.crossG.moveTo(p.x, p.y).lineTo(seat.x, seat.y).stroke({ width: 1, color: COLOR.muted });
      this.crossG.alpha = 0;
      void this.tween(this.crossG, { alpha: 1, duration: MOTION.fast });
    }
    const a = this.a;
    a.innerHTML = "";
    if (seat.side === "left") {
      const w = snap(Math.max(160, Math.min(360, seat.label.x - 16)));
      a.style.width = `${w}px`;
      a.style.left = `${this.ox + snap(seat.label.x - w)}px`;
      a.style.textAlign = "right";
    } else if (seat.side === "right") {
      const w = snap(Math.max(160, Math.min(360, this.W - 16 - seat.label.x)));
      a.style.width = `${w}px`;
      a.style.left = `${this.ox + snap(seat.label.x)}px`;
      a.style.textAlign = "left";
    } else {
      a.style.width = "360px";
      a.style.left = `${this.ox + snap(seat.x - 180)}px`;
      a.style.textAlign = "center";
    }
    a.style.top = `${this.oy + snap(seat.side === "far" ? seat.y - 80 : seat.y + 16)}px`;
    a.style.display = this.shown ? "block" : "none";
    const words = text.split(/\s+/).filter(Boolean);
    const total = durationMs > 0 ? durationMs : words.length * ROOM.wordMs;
    const step = words.length ? total / words.length : 0;
    const figures = new Map<string, { row: Row; tok: Text }>();
    for (const row of this.retrieved) {
      for (const f of row.data?.figures ?? []) {
        const tok = row.tokens.find((t) => t.visible && t.text.includes(String(f)));
        if (tok && !figures.has(clean(String(f)))) figures.set(clean(String(f)), { row, tok });
      }
      for (const t of row.tokens) if (t.visible && !figures.has(clean(t.text))) figures.set(clean(t.text), { row, tok: t });
    }
    const t0 = performance.now();
    await new Promise<void>((resolve) => {
      let i = 0;
      const tick = () => {
        if (i >= words.length) {
          this.hooks.onAnswerDone?.(agentId);
          return resolve();
        }
        const w = words[i++];
        const m = w.match(/^\[?(E\d+)[,\].]*$/);
        if (m) {
          const span = document.createElement("span");
          span.textContent = w + " ";
          span.style.cssText = `color:${CSS.muted};cursor:pointer;`;
          span.onclick = () => this.onEvidence(m![1]);
          a.appendChild(span);
        } else {
          a.appendChild(document.createTextNode(w + " "));
          const hit = figures.get(clean(w));
          if (hit) {
            hit.tok.style.fill = COLOR.text;
            window.setTimeout(() => (hit.tok.style.fill = COLOR.muted), 400);
          }
        }
        a.scrollTop = a.scrollHeight; // the three-line window under the seat follows the words; the transcript keeps the whole answer
        this.hooks.onWord?.(agentId, a.textContent ?? "");
        const due = t0 + i * step;
        window.setTimeout(tick, Math.max(0, due - performance.now()));
      };
      tick();
    });
    this.previous = { id: agentId, citations };
  }

  private async stageReleased(): Promise<void> {
    const back: Promise<void>[] = [];
    this.retrieved.forEach((row, i) => back.push(this.tween(row.group, { x: this.agendaLines[2].x, y: this.agendaLines[2].y + i * ROOM.rowPitch, alpha: 0, duration: MOTION.fast, delay: i * MOTION.stagger })));
    back.push(this.tween(this.lineG, { alpha: 0, duration: MOTION.fast }), this.tween(this.crossG, { alpha: 0, duration: MOTION.fast }), this.tween(this.recorded, { alpha: 0, duration: MOTION.fast }), this.tween(this.flagged, { alpha: 0, duration: MOTION.fast }), this.tween(this.flaggedUnderline, { alpha: 0, duration: MOTION.fast }), this.tween(this.verdictA, { alpha: 0, duration: MOTION.fast }), this.tween(this.verdictB, { alpha: 0, duration: MOTION.fast }));
    for (const s of this.seats.values()) {
      back.push(this.tween(s.group, { alpha: 1, duration: MOTION.fast }));
      s.label.style.fill = COLOR.muted;
      for (const p of s.papers) if (p.data) p.tag.style.fill = p.data.bad ? COLOR.amber : COLOR.muted;
    }
    back.push(this.tween(this.agenda, { alpha: 1, duration: MOTION.fast }));
    this.q.style.opacity = "0";
    this.a.style.opacity = "0";
    await Promise.all(back);
    for (const row of this.retrieved) {
      row.group.visible = false;
      row.group.scale.set(1);
      row.tick.clear();
      row.underline.clear();
    }
    this.retrieved = [];
    this.lineG.clear();
    this.lineG.alpha = 1;
    this.crossG.clear();
    this.crossG.alpha = 1;
    this.recorded.visible = this.flagged.visible = false;
    this.recorded.alpha = this.flagged.alpha = 1;
    this.verdictA.visible = this.verdictB.visible = false;
    this.traceId = null;
    this.flaggedUnderline.clear();
    this.flaggedUnderline.alpha = 1;
    this.q.textContent = "";
    this.a.innerHTML = "";
    this.q.style.display = this.a.style.display = "none";
    this.q.style.opacity = this.a.style.opacity = "1";
  }

  private async stageLearned(line: string): Promise<void> {
    this.learned.text = `LEARNED · ${line.replace(/^-\s*[\d-]+\s+\S+:\s*/, "")}`;
    this.learned.visible = true;
    this.learned.alpha = 0;
    this.learned.x = this.footer.x + 12;
    await this.tween(this.learned, { x: this.footer.x, alpha: 1, duration: MOTION.fast });
  }
}
