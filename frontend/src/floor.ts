import { Application, Container, Graphics, Text, TextStyle } from "pixi.js";
import gsap from "gsap";
import { COLOR, DESIGN_H, DESIGN_W, FONT, LAYOUT, MOTION, snap } from "./tokens";
import { dollars, fmtHM, type Clock, type Model, type Payment } from "./model";

const monoStyle = (size: number, fill: number) => new TextStyle({ fontFamily: FONT.mono, fontSize: size, fill, letterSpacing: size * 0.04 });
const sansStyle = (size: number, fill: number) => new TextStyle({ fontFamily: FONT.sans, fontSize: size, fill, fontWeight: "500" });

interface Sprite {
  p: Payment;
  rect: Graphics;
  tag: Text;
  tag2: Text; // second row, used only in the escalation column
  x: number; // right edge, drawn
  y: number; // drawn y (tweened)
  targetY: number;
  targetX: number | null; // null = follow time
  color: number;
  width: number;
  lastCode: string;
  lastTagText: string;
}

const ONE_MIN = 60_000;
const LOG_RANGE = Math.log((LAYOUT.daysAtLeft * 1440) / LAYOUT.minutesAtLine);

function lerpColor(a: number, b: number, t: number): number {
  if (t <= 0) return a;
  if (t >= 1) return b;
  const ch = (x: number, s: number) => (x >> s) & 0xff;
  const mix = (s: number) => Math.round(ch(a, s) + (ch(b, s) - ch(a, s)) * t);
  return (mix(16) << 16) | (mix(8) << 8) | mix(0);
}

export class Floor {
  app = new Application();
  lanes: Container[] = [];
  sprites = new Map<string, Sprite>();
  labels: Text[] = [];
  labelX: number[] = [];
  laneOrder: number[] = [];
  private lineG = new Graphics();
  private rulerG = new Graphics();
  private staticG = new Graphics();
  private execLabel!: Text;
  private mandateLabel!: Text;
  private counter!: Text;
  private counterActive = false;
  private frames = 0;
  private fpsAt = performance.now();
  fps = 0;
  private emphasis: number | null = null;
  private emphasisT = 0;
  private emphasisState = { t: 0 };

  constructor(private model: Model, private clock: Clock, private onClickPayment: (p: Payment) => void) {}

  async init(canvas: HTMLCanvasElement): Promise<void> {
    await this.app.init({ canvas, resizeTo: window, background: COLOR.bg, antialias: false, resolution: window.devicePixelRatio || 1, autoDensity: true });
    this.app.stage.sortableChildren = true;
    this.app.stage.addChild(this.staticG, this.rulerG, this.lineG);
    this.execLabel = new Text({ text: "EXECUTION", style: sansStyle(11, COLOR.muted) });
    this.execLabel.rotation = Math.PI / 2;
    this.execLabel.position.set(LAYOUT.lineX + 8, 72);
    this.mandateLabel = new Text({ text: "MANDATE", style: sansStyle(11, COLOR.muted) });
    this.mandateLabel.rotation = Math.PI / 2;
    this.mandateLabel.position.set(LAYOUT.lineX + 8, 72 + 88);
    this.mandateLabel.visible = false;
    this.counter = new Text({ text: "", style: monoStyle(24, COLOR.text) });
    this.counter.anchor.set(1, 1);
    this.counter.position.set(LAYOUT.lineX - 12, 88);
    this.app.stage.addChild(this.execLabel, this.mandateLabel, this.counter);
    this.buildLanes();
    this.buildLabels();
    this.drawStatic();
    for (const sp of this.sprites.values()) this.redraw(sp);
    this.layoutViewport();
    window.addEventListener("resize", () => this.layoutViewport());
    this.app.ticker.add(() => this.frame());
  }

  layoutViewport(): void {
    const s = Math.min(window.innerWidth / DESIGN_W, window.innerHeight / DESIGN_H);
    const ox = Math.floor((window.innerWidth - DESIGN_W * s) / 2);
    const oy = Math.floor((window.innerHeight - DESIGN_H * s) / 2);
    this.app.stage.scale.set(s);
    this.app.stage.position.set(ox, oy);
    const ui = document.getElementById("ui");
    if (ui) ui.style.transform = `translate(${ox}px, ${oy}px) scale(${s})`;
  }

  private buildLanes(): void {
    this.lanes = this.model.agents.map((_, i) => {
      const c = new Container();
      c.y = LAYOUT.laneTop + i * LAYOUT.laneH;
      this.app.stage.addChild(c);
      return c;
    });
    for (const p of this.model.payments.values()) {
      const rect = new Graphics();
      const tag = new Text({ text: "", style: monoStyle(10, COLOR.text) });
      const tag2 = new Text({ text: "", style: monoStyle(10, COLOR.text) });
      tag.anchor.set(1, 0.5);
      tag2.visible = false;
      tag.eventMode = "static";
      rect.eventMode = "static";
      tag.cursor = rect.cursor = "pointer";
      const sp: Sprite = { p, rect, tag, tag2, x: 0, y: this.laneCenter(p.lane), targetY: this.laneCenter(p.lane), targetX: null, color: COLOR.text, width: this.widthOf(p), lastCode: "", lastTagText: "" };
      tag.on("pointertap", () => this.onClickPayment(sp.p));
      rect.on("pointertap", () => this.onClickPayment(sp.p));
      this.app.stage.addChild(rect, tag, tag2);
      this.sprites.set(p.id, sp);
    }
  }

  private buildLabels(): void {
    this.labels = this.model.agents.map((a) => new Text({ text: a.display.toUpperCase(), style: monoStyle(11, COLOR.muted) }));
    const minSlot = this.labels.map((t) => snap(t.width + 24));
    const span = LAYOUT.lineX - 24;
    const free = Math.max(0, span - minSlot.reduce((s, w) => s + w, 0));
    const total = this.model.agents.reduce((s, a) => s + a.count, 0) || 1;
    let x = 24;
    this.labelX = [];
    this.laneOrder = this.model.agents.map((_, i) => i);
    this.model.agents.forEach((a, i) => {
      const t = this.labels[i];
      t.position.set(snap(x), LAYOUT.labelY);
      this.labelX.push(snap(x));
      this.app.stage.addChild(t);
      x += minSlot[i] + (a.count / total) * free;
    });
  }

  private drawStatic(): void {
    const g = this.staticG;
    g.clear();
    for (let i = 0; i <= this.lanes.length; i++) g.rect(24, LAYOUT.laneTop + i * LAYOUT.laneH, LAYOUT.lineX - 24, 1).fill(COLOR.rule);
    g.rect(24, LAYOUT.holdY - 8, LAYOUT.lineX - 24, 1).fill(COLOR.rule);
    this.labelX.forEach((x) => g.rect(x, LAYOUT.labelY - 8, 1, 6).fill(COLOR.rule));
    const r = this.rulerG;
    r.clear();
    for (let y = LAYOUT.laneTop - 24; y <= LAYOUT.labelY - 16; y += LAYOUT.rulerStep) r.rect(LAYOUT.lineX - 4, y, 9, 1).fill(COLOR.rule);
    this.drawLine();
  }

  drawLine(): void {
    const w = this.model.runVersion === "v2" ? 2 : 1;
    this.lineG.clear();
    this.lineG.rect(LAYOUT.lineX, 64, w, LAYOUT.labelY - 16 - 64).fill(COLOR.rule);
    this.mandateLabel.visible = this.model.runVersion === "v2";
  }

  laneCenter(lane: number): number {
    return LAYOUT.laneTop + lane * LAYOUT.laneH + 8; // rows at +8/+20/+32/+44: four 12px rows, all on the 4px grid
  }

  rebind(): void {
    for (const [id, sp] of this.sprites) {
      const p = this.model.payments.get(id);
      if (p) sp.p = p;
    }
  }

  widthOf(p: Payment): number {
    const t = (p.amount_cents - 200_000) / (18_000_000 - 200_000);
    return snap(40 + Math.min(1, Math.max(0, t)) * 140);
  }

  // right edge for a payment at company time `now`
  timeX(p: Payment, now: Date, w: number): number {
    const dtMin = (p.scheduled_at.getTime() - now.getTime()) / ONE_MIN;
    if (dtMin <= 0) return LAYOUT.lineX + w;
    if (dtMin <= LAYOUT.minutesAtLine) return LAYOUT.lineX + w * (1 - dtMin / LAYOUT.minutesAtLine);
    const u = Math.min(1, Math.log(dtMin / LAYOUT.minutesAtLine) / LOG_RANGE);
    return LAYOUT.lineX - u * (LAYOUT.lineX - LAYOUT.leftX);
  }

  tagText(p: Payment): string {
    return `${p.vendor.toUpperCase()}  ${dollars(p.amount_cents)}  ${p.rail}  ${p.po_number}  SETTLE ${p.settle_date.slice(5)}  ${fmtHM(p.scheduled_at)}  ${p.code}${p.glyph ? "  H" : ""}`;
  }

  // escalation column: same telemetry on two rows so it fits the right margin
  tagRows(p: Payment): [string, string] {
    return [
      `${p.vendor.toUpperCase()}  ${dollars(p.amount_cents)}  ${p.code}${p.glyph ? "  H" : ""}`,
      `${p.rail}  ${p.po_number}  SETTLE ${p.settle_date.slice(5)}  ${fmtHM(p.scheduled_at)}`,
    ];
  }

  baseColor(p: Payment): number {
    if (p.red) return COLOR.red;
    if (p.code === "HLD" || (p.code === "ESC" && p.place === "esc" && !p.glyph)) return COLOR.amber;
    if (p.cleared || p.code === "DONE") return COLOR.muted;
    return COLOR.text;
  }

  // emphasis: the addressed lane reads #E6E6E3, every other lane #7C8087; blended by emphasisT during the 240ms tween
  colorOf(p: Payment): number {
    const base = this.baseColor(p);
    if (this.emphasis === null && this.emphasisT === 0) return base;
    const target = this.emphasis !== null && p.lane === this.emphasis ? COLOR.text : COLOR.muted;
    return lerpColor(base, target, this.emphasisT);
  }

  private redraw(sp: Sprite, colorOnly = false): void {
    const p = sp.p;
    const color = this.colorOf(p);
    if (colorOnly) {
      if (color === sp.color) return;
      sp.color = color;
      sp.tag.style.fill = color;
      sp.tag2.style.fill = color;
      sp.rect.clear();
      if (p.place === "done") sp.rect.rect(0, 0, 1, 8).fill(color);
      else sp.rect.rect(0, 0, sp.width, 2).stroke({ width: 1, color, alignment: 0 });
      return;
    }
    const inColumn = p.place === "esc";
    const [row1, row2] = inColumn ? this.tagRows(p) : [this.tagText(p), ""];
    const key = `${inColumn ? "c" : "l"}|${row1}|${row2}`;
    if (key !== sp.lastTagText) {
      sp.tag.text = row1;
      sp.tag2.text = row2;
      sp.tag2.visible = inColumn;
      sp.lastTagText = key;
    }
    if (color !== sp.color || p.code !== sp.lastCode) {
      sp.color = color;
      sp.lastCode = p.code;
      sp.tag.style.fill = color;
      sp.tag2.style.fill = color;
      sp.rect.clear();
      if (p.place === "done") sp.rect.rect(0, 0, 1, 8).fill(color);
      else sp.rect.rect(0, 0, sp.width, 2).stroke({ width: 1, color, alignment: 0 });
      sp.tag.visible = p.place !== "done";
    }
  }

  moveTo(sp: Sprite, y: number, x: number | null, duration: number): void {
    sp.targetY = y;
    sp.targetX = x;
    gsap.to(sp, { y, duration, ease: MOTION.ease, overwrite: "auto" });
    if (x !== null) gsap.to(sp, { x, duration, ease: MOTION.ease, overwrite: false });
  }

  place(sp: Sprite, animate = true): void {
    const p = sp.p;
    const d = animate ? MOTION.drop : 0;
    if (p.place === "hold") this.moveTo(sp, LAYOUT.holdY + p.order * LAYOUT.stackPitch, null, d);
    else if (p.place === "esc") this.moveTo(sp, LAYOUT.escTop + p.order * LAYOUT.escPitch, LAYOUT.escX + sp.width, d);
    else if (p.place === "done") this.moveTo(sp, this.laneCenter(p.lane), LAYOUT.lineX + 12 + p.order * 4, MOTION.fast);
    else this.moveTo(sp, this.laneCenter(p.lane), null, d);
  }

  sweep(laneChanges: Map<number, Payment[]>): void {
    this.laneOrder.forEach((lane, i) => {
      gsap.delayedCall(i * MOTION.stagger, () => {
        for (const p of laneChanges.get(lane) ?? []) {
          const sp = this.sprites.get(p.id);
          if (sp) this.place(sp, true);
        }
        for (const sp of this.sprites.values()) if (sp.p.lane === lane) this.redraw(sp);
      });
    });
  }

  applyState(p: Payment, animate = true): void {
    const sp = this.sprites.get(p.id);
    if (!sp) return;
    this.place(sp, animate);
    this.redraw(sp);
  }

  setEmphasis(lane: number | null): void {
    if (lane !== null) this.emphasis = lane;
    const target = lane === null ? 0 : 1;
    gsap.killTweensOf(this.emphasisState);
    gsap.to(this.emphasisState, {
      t: target,
      duration: MOTION.fast,
      ease: MOTION.ease,
      onUpdate: () => {
        this.emphasisT = this.emphasisState.t;
        this.labels.forEach((lbl, i) => {
          const to = this.emphasis !== null && i === this.emphasis ? COLOR.text : COLOR.muted;
          lbl.style.fill = lerpColor(COLOR.muted, to, this.emphasisT);
        });
        for (const sp of this.sprites.values()) this.redraw(sp, true);
      },
      onComplete: () => {
        if (target === 0) this.emphasis = null;
      },
    });
  }

  startCounter(): void {
    this.counterActive = true;
  }

  stopCounter(at: number): void {
    this.counterActive = false;
    this.counter.text = String(at);
  }

  resetAll(): void {
    this.rebind();
    this.counterActive = false;
    this.counter.text = "";
    gsap.killTweensOf(this.emphasisState);
    this.emphasis = null;
    this.emphasisT = this.emphasisState.t = 0;
    this.labels.forEach((lbl) => (lbl.style.fill = COLOR.muted));
    for (const sp of this.sprites.values()) {
      gsap.killTweensOf(sp);
      sp.y = sp.targetY = this.laneCenter(sp.p.lane);
      sp.targetX = null;
      sp.lastCode = "";
      sp.lastTagText = "";
      sp.tag.alpha = sp.tag2.alpha = sp.rect.alpha = 1;
      this.redraw(sp);
    }
    this.drawLine();
  }

  private frame(): void {
    const now = this.clock.now();
    const rows = new Map<number, Array<[number, number, number]>>();
    const ordered = [...this.sprites.values()].sort((a, b) => a.p.scheduled_at.getTime() - b.p.scheduled_at.getTime());
    for (const sp of ordered) {
      const p = sp.p;
      if (sp.targetX === null) {
        let x = this.timeX(p, now, sp.width);
        if (p.place === "hold") x = Math.min(x, LAYOUT.lineX);
        if (p.place === "lane" && (p.code === "HLD" || p.code === "ESC")) x = Math.min(x, LAYOUT.lineX);
        sp.x = Math.round(x);
      }
      let y = sp.y;
      if (p.place === "lane") {
        const left = sp.x - sp.width - sp.tag.width - 8;
        const list = rows.get(p.lane) ?? [];
        let row = 0;
        while (row < 3 && list.some(([l, r, rw]) => rw === row && left < r && sp.x > l)) row += 1;
        list.push([left, sp.x, row]);
        rows.set(p.lane, list);
        y = Math.round(sp.y) + row * 12;
      }
      if (p.place === "esc") {
        // escalation column: bar right of the line, two tag rows beneath it, all left-aligned
        sp.rect.position.set(LAYOUT.escX, Math.round(y) - 1);
        sp.tag.anchor.set(0, 0);
        sp.tag.position.set(LAYOUT.escX, Math.round(y) + 4);
        sp.tag2.position.set(LAYOUT.escX, Math.round(y) + 16);
      } else {
        sp.tag.anchor.set(1, 0.5);
        sp.rect.position.set(sp.x - (p.place === "done" ? 0 : sp.width), Math.round(y) - (p.place === "done" ? 4 : 1));
        sp.tag.position.set(sp.x - sp.width - 8, Math.round(y));
      }
    }
    if (this.counterActive && this.model.windowStart) {
      const s = Math.max(0, Math.floor((now.getTime() - this.model.windowStart.getTime()) / 1000));
      const txt = String(s);
      if (this.counter.text !== txt) this.counter.text = txt;
    }
    this.frames++;
    const t = performance.now();
    if (t - this.fpsAt >= 1000) {
      this.fps = Math.round((this.frames * 1000) / (t - this.fpsAt));
      this.frames = 0;
      this.fpsAt = t;
      if (import.meta.env.DEV) console.log(`fps ${this.fps}`);
    }
  }
}
