import { Application, Graphics, Text, TextStyle } from "pixi.js";
import gsap from "gsap";
import { COLOR, FONT, MOTION, RADIAL, snap } from "./tokens";
import { dollars, fmtHM, type Clock, type Model, type Payment } from "./model";

const monoStyle = (size: number, fill: number) => new TextStyle({ fontFamily: FONT.mono, fontSize: size, fill, letterSpacing: size * 0.04 });
const sansStyle = (size: number, fill: number) => new TextStyle({ fontFamily: FONT.sans, fontSize: size, fill, fontWeight: "500" });

interface Sprite {
  p: Payment;
  rect: Graphics;
  tag: Text;
  r: number; // drawn radius of the leading edge (tweened when parked)
  targetR: number | null; // null = follow company time
  color: number;
  barLen: number;
  lastCode: string;
  lastTagText: string;
  tagW: number; // cached tag width, measured only when the text changes
  push: number; // collision offset, px outward
}

interface Geo {
  cx: number;
  cy: number;
  R: number;
  labelR: number;
  farR: number;
  holdR: number;
}

const ONE_MIN = 60_000;
const LOG_RANGE = Math.log((RADIAL.daysAtFar * 1440) / RADIAL.minutesAtRing);
const N = 12;

function lerpColor(a: number, b: number, t: number): number {
  if (t <= 0) return a;
  if (t >= 1) return b;
  const ch = (x: number, s: number) => (x >> s) & 0xff;
  const mix = (s: number) => Math.round(ch(a, s) + (ch(b, s) - ch(a, s)) * t);
  return (mix(16) << 16) | (mix(8) << 8) | mix(0);
}

export class Floor {
  app = new Application();
  sprites = new Map<string, Sprite>();
  labels: Text[] = [];
  spokes: Graphics[] = [];
  laneOrder: number[] = [];
  geo: Geo = { cx: 960, cy: 508, R: 432, labelR: 121, farR: 151, holdR: 238 };
  private ringG = new Graphics();
  private recG = new Graphics();
  private execLabel!: Text;
  private mandateLabel!: Text;
  private cfo!: Text;
  private counter!: Text;
  private counterActive = false;
  private counterAnchor: string | null = null; // payment id the counter sits above
  private frames = 0;
  private fpsAt = performance.now();
  fps = 0;
  private emphasis: number | null = null;
  private emphasisT = 0;
  private emphasisState = { t: 0 };
  private labelBoxes: Array<[number, number, number, number]> = [];
  private static CANDIDATES: Array<[number, number]> = [0, 12, -12, 24, -24, 36, -36, 48, -48, 60, -60, 72, -72].flatMap((dy) => [0, 12, 24, 36].map((pp) => [pp, dy] as [number, number]));

  constructor(private model: Model, private clock: Clock, private onClickPayment: (p: Payment) => void) {}

  async init(canvas: HTMLCanvasElement): Promise<void> {
    await this.app.init({ canvas, resizeTo: window, background: COLOR.bg, antialias: true, resolution: window.devicePixelRatio || 1, autoDensity: true });
    this.app.stage.addChild(this.ringG);
    this.spokes = Array.from({ length: N }, () => new Graphics());
    this.spokes.forEach((g) => this.app.stage.addChild(g));
    this.execLabel = new Text({ text: "EXECUTION", style: sansStyle(11, COLOR.muted) });
    this.execLabel.anchor.set(0, 0.5);
    this.mandateLabel = new Text({ text: "MANDATE", style: sansStyle(11, COLOR.muted) });
    this.mandateLabel.anchor.set(0, 0.5);
    this.mandateLabel.visible = false;
    this.cfo = new Text({ text: "CFO", style: monoStyle(13, COLOR.text) });
    this.cfo.anchor.set(0.5, 0.5);
    this.counter = new Text({ text: "", style: monoStyle(24, COLOR.text) });
    this.counter.anchor.set(0.5, 1);
    this.recG.visible = false;
    this.app.stage.addChild(this.execLabel, this.mandateLabel, this.cfo, this.recG, this.counter);
    this.buildLabels();
    this.buildSprites();
    this.layoutViewport();
    for (const sp of this.sprites.values()) this.redraw(sp);
    window.addEventListener("resize", () => this.layoutViewport());
    this.app.ticker.add(() => this.frame());
  }

  bearing(i: number): number {
    return -Math.PI / 2 + (i * 2 * Math.PI) / N;
  }

  polar(i: number, r: number): [number, number] {
    const a = this.bearing(i);
    return [this.geo.cx + Math.cos(a) * r, this.geo.cy + Math.sin(a) * r];
  }

  layoutViewport(): void {
    const W = window.innerWidth;
    const H = window.innerHeight;
    const R = snap(RADIAL.r * Math.min(W, H));
    this.geo = { cx: snap(RADIAL.cx * W), cy: snap(RADIAL.cy * H), R, labelR: snap(RADIAL.labelR * R), farR: snap(RADIAL.farR * R), holdR: snap(RADIAL.holdR * R) };
    this.drawStatic();
  }

  private buildLabels(): void {
    this.laneOrder = this.model.agents.map((_, i) => i);
    this.labels = this.model.agents.map((a) => {
      const t = new Text({ text: a.display.toUpperCase(), style: monoStyle(11, COLOR.muted) });
      this.app.stage.addChild(t);
      return t;
    });
  }

  private buildSprites(): void {
    for (const p of this.model.payments.values()) {
      const rect = new Graphics();
      const tag = new Text({ text: "", style: monoStyle(10, COLOR.text) });
      tag.eventMode = "static";
      rect.eventMode = "static";
      tag.cursor = rect.cursor = "pointer";
      const sp: Sprite = { p, rect, tag, r: this.geo.farR, targetR: null, color: COLOR.text, barLen: this.barLenOf(p), lastCode: "", lastTagText: "", tagW: 0, push: 0 };
      tag.on("pointertap", () => this.onClickPayment(sp.p));
      rect.on("pointertap", () => this.onClickPayment(sp.p));
      this.app.stage.addChild(rect, tag);
      this.sprites.set(p.id, sp);
    }
  }

  private drawStatic(): void {
    const { cx, cy, R, labelR } = this.geo;
    this.drawRing();
    // 3 o'clock, lifted clear of the horizontal spoke's tag rows
    this.execLabel.position.set(cx + R + 8, cy - 44);
    this.mandateLabel.position.set(cx + R + 8, cy - 30);
    this.cfo.position.set(cx, cy);
    this.recG.clear();
    this.recG.rect(cx - 24, cy + 12, 48, 2).fill(COLOR.red);
    // labels sit on the 0.28R ring, text pointing outward along the spoke; tags route around them
    this.labelBoxes = [];
    this.labels.forEach((t, i) => {
      const a = this.bearing(i);
      const [x, y] = this.polar(i, labelR);
      const c = Math.cos(a);
      const s = Math.sin(a);
      if (Math.abs(c) < 0.01) {
        t.anchor.set(0.5, s < 0 ? 1 : 0);
        t.position.set(snap(x), snap(y + (s < 0 ? -4 : 4)));
      } else {
        t.anchor.set(c > 0 ? 0 : 1, 0.5);
        t.position.set(snap(x + (c > 0 ? 4 : -4)), snap(y));
      }
      const b = t.getBounds();
      this.labelBoxes.push([b.x - 4, b.y - 2, b.x + b.width + 4, b.y + b.height + 2]);
    });
    this.drawSpokes();
    if (!this.counterAnchor) {
      this.counter.anchor.set(0.5, 0);
      this.counter.position.set(cx, cy + 24);
    }
  }

  private drawRing(): void {
    const { cx, cy, R } = this.geo;
    const w = this.model.runVersion === "v2" ? 2 : 1;
    this.ringG.clear();
    this.ringG.circle(cx, cy, R).stroke({ width: w, color: COLOR.rule });
    this.mandateLabel.visible = this.model.runVersion === "v2";
  }

  drawLine(): void {
    this.drawRing();
  }

  private drawSpokes(): void {
    const { labelR, R } = this.geo;
    this.spokes.forEach((g, i) => {
      const color = this.spokeColor(i);
      const [x0, y0] = this.polar(i, labelR + 8);
      const [x1, y1] = this.polar(i, R);
      g.clear();
      g.moveTo(x0, y0).lineTo(x1, y1).stroke({ width: 1, color });
    });
  }

  private spokeColor(i: number): number {
    if (this.emphasis === null && this.emphasisT === 0) return COLOR.rule;
    const target = this.emphasis !== null && i === this.emphasis ? COLOR.text : COLOR.rule;
    return lerpColor(COLOR.rule, target, this.emphasisT);
  }

  barLenOf(p: Payment): number {
    const t = (p.amount_cents - 200_000) / (18_000_000 - 200_000);
    return snap(RADIAL.barMin + Math.min(1, Math.max(0, t)) * (RADIAL.barMax - RADIAL.barMin));
  }

  // radius of the leading edge for a payment at company time `now`
  timeR(p: Payment, now: Date, barLen: number): number {
    const { R, farR } = this.geo;
    const dtMin = (p.scheduled_at.getTime() - now.getTime()) / ONE_MIN;
    if (dtMin <= 0) return R + barLen;
    if (dtMin <= RADIAL.minutesAtRing) return R + (1 - dtMin / RADIAL.minutesAtRing) * barLen;
    const u = Math.min(1, Math.log(dtMin / RADIAL.minutesAtRing) / LOG_RANGE);
    return R - u * (R - farR);
  }

  tagText(p: Payment): string {
    return `${p.vendor.toUpperCase()}  ${dollars(p.amount_cents)}  ${p.rail}  ${p.po_number}  SETTLE ${p.settle_date.slice(5)}  ${fmtHM(p.scheduled_at)}  ${p.code}${p.glyph ? "  H" : ""}`;
  }

  baseColor(p: Payment): number {
    if (p.red) return COLOR.red;
    if (p.code === "HLD" || (p.code === "ESC" && p.place === "esc" && !p.glyph)) return COLOR.amber;
    if (p.cleared || p.code === "DONE") return COLOR.muted;
    return COLOR.text;
  }

  colorOf(p: Payment): number {
    const base = this.baseColor(p);
    if (this.emphasis === null && this.emphasisT === 0) return base;
    const target = this.emphasis !== null && p.lane === this.emphasis ? COLOR.text : COLOR.muted;
    return lerpColor(base, target, this.emphasisT);
  }

  private redraw(sp: Sprite, colorOnly = false): void {
    const p = sp.p;
    const color = this.colorOf(p);
    if (!colorOnly) {
      const text = this.tagText(p);
      if (text !== sp.lastTagText) {
        sp.tag.text = text;
        sp.lastTagText = text;
        sp.tagW = sp.tag.width;
      }
    }
    if (color !== sp.color || p.code !== sp.lastCode) {
      sp.color = color;
      sp.lastCode = p.code;
      sp.tag.style.fill = color;
      sp.rect.clear();
      if (p.place === "done") sp.rect.rect(0, -0.5, RADIAL.tickLen, 1).fill(color);
      else sp.rect.rect(0, -1, sp.barLen, 2).stroke({ width: 1, color, alignment: 0 });
      sp.tag.visible = p.place !== "done";
    }
  }

  moveTo(sp: Sprite, r: number | null, duration: number): void {
    sp.targetR = r;
    gsap.killTweensOf(sp);
    if (r !== null) gsap.to(sp, { r, duration, ease: MOTION.ease });
  }

  place(sp: Sprite, animate = true): void {
    const p = sp.p;
    const d = animate ? MOTION.drop : 0;
    if (p.place === "hold" || p.place === "esc") this.moveTo(sp, this.geo.holdR, d);
    else if (p.place === "done") this.moveTo(sp, this.geo.R + RADIAL.tickGap + p.order * 4, MOTION.fast);
    else this.moveTo(sp, null, d);
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
        this.drawSpokes();
        for (const sp of this.sprites.values()) this.redraw(sp, true);
      },
      onComplete: () => {
        if (target === 0) this.emphasis = null;
      },
    });
  }

  setRecording(on: boolean): void {
    this.recG.visible = on;
  }

  startCounter(): void {
    this.counterActive = true;
    this.counterAnchor = null;
  }

  stopCounter(at: number, paymentId?: string): void {
    this.counterActive = false;
    this.counter.text = String(at);
    this.counterAnchor = paymentId ?? null;
    if (this.counterAnchor) this.counter.anchor.set(0.5, 0.5);
  }

  resetAll(): void {
    for (const [id, sp] of this.sprites) {
      const p = this.model.payments.get(id);
      if (p) sp.p = p;
    }
    this.counterActive = false;
    this.counterAnchor = null;
    this.counter.text = "";
    gsap.killTweensOf(this.emphasisState);
    this.emphasis = null;
    this.emphasisT = this.emphasisState.t = 0;
    this.labels.forEach((lbl) => (lbl.style.fill = COLOR.muted));
    for (const sp of this.sprites.values()) {
      gsap.killTweensOf(sp);
      sp.targetR = null;
      sp.r = this.geo.farR;
      sp.lastCode = "";
      sp.lastTagText = "";
      sp.push = 0;
      this.redraw(sp);
    }
    this.drawStatic();
  }

  private frame(): void {
    const now = this.clock.now();
    const { cx, cy } = this.geo;
    const boxes: Array<[number, number, number, number]> = [...this.labelBoxes];
    const ordered = [...this.sprites.values()].sort((a, b) => a.p.scheduled_at.getTime() - b.p.scheduled_at.getTime());
    for (const sp of ordered) {
      const p = sp.p;
      if (sp.targetR === null) {
        let r = this.timeR(p, now, sp.barLen);
        if (p.place === "lane" && (p.code === "HLD" || p.code === "ESC")) r = Math.min(r, this.geo.R);
        sp.r = r;
      }
      const a = this.bearing(p.lane);
      const c = Math.cos(a);
      const s = Math.sin(a);
      let right = c >= -0.01;
      // collision: a later tag overlapping an earlier one moves 12px outward (up to three times); if the spoke is
      // too flat for outward motion to clear it, the tag steps 12px perpendicular to the spoke instead; as a last
      // resort it flips to the other side of its point
      let push = 0;
      let dy = 0;
      if (p.place !== "done") {
        const w = sp.tagW;
        let placed = false;
        for (const side of [right, !right]) {
          for (const [pp, dd] of Floor.CANDIDATES) {
            const rr = sp.r + pp;
            const x = cx + c * rr;
            const y = cy + s * rr + dd;
            const x0 = side ? x + RADIAL.tagGap : x - RADIAL.tagGap - w;
            const x1 = x0 + w;
            const y0 = y - 6;
            const y1 = y + 6;
            let hit = false;
            for (let k = 0; k < boxes.length; k++) {
              const b = boxes[k];
              if (y0 < b[3] && y1 > b[1] && x0 < b[2] && x1 > b[0]) {
                hit = true;
                break;
              }
            }
            if (!hit) {
              push = pp;
              dy = dd;
              right = side;
              placed = true;
              break;
            }
          }
          if (placed) break;
        }
      }
      sp.push = push;
      const r = sp.r + push;
      const x = cx + c * r;
      const y = cy + s * r + dy;
      if (p.place === "done") {
        sp.rect.position.set(x, y);
        sp.rect.rotation = a;
      } else {
        sp.rect.position.set(cx + c * (r - sp.barLen), cy + s * (r - sp.barLen) + dy);
        sp.rect.rotation = a;
        sp.tag.anchor.set(right ? 0 : 1, 0.5);
        sp.tag.position.set(Math.round(right ? x + RADIAL.tagGap : x - RADIAL.tagGap), Math.round(y));
        const w = sp.tagW;
        const x0 = right ? x + RADIAL.tagGap : x - RADIAL.tagGap - w;
        boxes.push([x0, y - 6, x0 + w, y + 6]);
      }
      if (this.counterAnchor === p.id) this.counter.position.set(Math.round(cx + c * (r + 40)), Math.round(cy + s * (r + 40) + dy) - 24);
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
