import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "@fontsource/inter-tight/400.css";
import "@fontsource/inter-tight/500.css";
import gsap from "gsap";
import { api, netCheck } from "./api";
import { Floor } from "./floor";
import { Clock, Model, dollars, fmtClock, parseCompany, type Payment } from "./model";
import * as ui from "./overlays";
import { Room } from "./room";
import { MOTION } from "./tokens";
import { Voice } from "./voice";
import { connect, send, wsOpen, type BusEvent } from "./ws";

const model = new Model();
const clock = new Clock();
let floor: Floor;
let room: Room;
let voice: Voice;
let meetingMode = false;
let pendingMandate: string | null = null;
let net = "NET —";
let netOk = false;
let lastX = 0;
let lastEvent: { type: string; ts?: string; marker?: string } | null = null;
const seen: string[] = [];

declare global {
  interface Window {
    __floor: { ready: boolean; last: typeof lastEvent; seen: string[]; fps: number; events: number; session: string };
  }
}

function mark(s: string): void {
  seen.push(s);
  if (seen.length > 2000) seen.splice(0, 1000);
}

function publishHook(): void {
  window.__floor = { ready: true, last: lastEvent, seen, fps: floor.fps, events: model.events.length, session: model.sessionId };
}

function playAudio(name: string): void {
  try {
    const a = new Audio(`/audio/${name}`);
    a.play().catch(() => undefined);
  } catch {
    /* absent */
  }
}

function status(): void {
  ui.setStatus(meetingMode ? "MEETING" : model.mode, meetingMode ? model.meetingSession : model.sessionId, net);
}

function setMeetingMode(on: boolean): void {
  meetingMode = on;
  floor.setVisible(!on);
  room.setVisible(on);
  ui.setPaymentsHud(!on);
  ui.closeOverlays();
  status();
}

async function openEvidence(id: string): Promise<void> {
  let row = room.evidenceRow(id);
  if (!row) {
    const json = await api.explainEvidence().catch(() => null);
    if (json) {
      room.setEvidence(json);
      row = room.evidenceRow(id);
    }
  }
  if (row) ui.openEvidenceRow(row);
  else ui.showTransientAnswer(`${id}: evidence not loaded`);
}

function onEvent(e: BusEvent): void {
  if (e.type === "replay.status") {
    clock.setServer(Number(e.payload.speed ?? -1), Boolean(e.payload.paused));
    return;
  }
  if (e.ts_company) clock.onEvent(parseCompany(e.ts_company));
  lastEvent = { type: e.type, ts: e.ts_company };
  mark(`${e.type}@${(e.ts_company ?? "").slice(11)}`);
  room.handle(e);
  if (e.type === "meeting.loaded") {
    if (e.payload.session_id) model.meetingSession = e.payload.session_id;
    void api.explainEvidence().then((json) => room.setEvidence(json)).catch(() => undefined);
    if (!meetingMode) setMeetingMode(true);
    else status();
  }
  if (e.type === "payment.held") mark(`held:${e.payload.source}:${e.payload.payment_id}@${(e.ts_company ?? "").slice(11)}`);
  const effects = model.apply(e);
  for (const fx of effects) {
    switch (fx.kind) {
      case "reset":
        floor.resetAll();
        ui.hideMandate();
        ui.clearAnswer();
        ui.closeOverlays();
        if (e.ts_company) clock.reset(parseCompany(e.ts_company));
        status();
        break;
      case "readback":
        if (!fx.payload.questions?.length) {
          pendingMandate = fx.payload.mandate_id;
          ui.showReadback(fx.payload.readback, `READ-BACK  ${String(fx.payload.mandate_id).toUpperCase()}  ·  C CONFIRMS`);
        } else {
          ui.showReadback(fx.payload.readback, "DESK ASKS");
        }
        break;
      case "mandate_bound":
        ui.showMandate(fx.payload.compiled, fx.payload.label);
        floor.startCounter();
        playAudio("click.wav");
        break;
      case "exposure": {
        const changes = new Map<number, Payment[]>();
        for (const id of [...(fx.payload.held ?? []), ...(fx.payload.escalated ?? [])]) {
          const p = model.payments.get(id);
          if (!p) continue;
          if (!changes.has(p.lane)) changes.set(p.lane, []);
          changes.get(p.lane)!.push(p);
        }
        floor.sweep(changes);
        lastEvent = { type: e.type, ts: e.ts_company, marker: fx.payload.already_executed?.length ? "already_executed" : "sweep" };
        mark(fx.payload.already_executed?.length ? "already_executed" : "sweep");
        break;
      }
      case "already_executed": {
        floor.stopCounter(model.landedSeconds ?? 0, fx.payload.payment?.id);
        if (fx.payload.payment) floor.applyState(fx.payload.payment, false);
        playAudio("thud.wav");
        const p = fx.payload.payment as Payment | undefined;
        const at = fx.payload.entry?.executed_at ? fmtClock(parseCompany(fx.payload.entry.executed_at)).slice(4) : "";
        ui.showReport(fx.payload.sentence || "(no status report)", `EXECUTE_PAYMENT  ${p ? dollars(p.amount_cents) : ""}  ${at}`);
        break;
      }
      case "held":
        if (fx.payload.source === "boundary" && model.windowStart) {
          const at = e.ts_company ? parseCompany(e.ts_company) : null;
          if (at) floor.stopCounter(Math.round((at.getTime() - model.windowStart.getTime()) / 1000), fx.payload.payment?.id);
        }
        floor.applyState(fx.payload.payment, true);
        break;
      case "escalated":
      case "released":
      case "executed":
        floor.applyState(fx.payload.payment, true);
        break;
      case "expired":
        ui.hideMandate();
        break;
      case "addressed": {
        const lane = model.laneOf.get(fx.payload.agent_id);
        floor.setEmphasis(lane ?? null);
        break;
      }
      case "answer": {
        const a = model.agents.find((x) => x.id === fx.payload.agent_id);
        ui.showAgentAnswer(a?.display ?? fx.payload.agent_id, fx.payload.text ?? "");
        break;
      }
      case "released_agent":
        floor.setEmphasis(null);
        ui.clearAnswer();
        break;
      case "desk_answer":
      case "query_answer":
        ui.showTransientAnswer(fx.payload.text ?? "");
        break;
    }
  }
  ui.setCash(model.cash_cents);
  publishHook();
}

async function selectSession(s: string): Promise<void> {
  model.sessionId = s;
  model.mode = "REPLAY";
  status();
}

async function startReplay(speed: number, stage: boolean): Promise<void> {
  model.mode = "REPLAY";
  try {
    send("seek", "Fri 19:15"); // end any running replay; HTTP /replay/cmd is shadowed by /replay/{session_id}
    await new Promise((r) => setTimeout(r, 200));
    await api.replay(meetingMode ? model.meetingSession : model.sessionId, speed, meetingMode ? false : stage);
  } catch (err) {
    ui.showTransientAnswer(`replay: ${(err as Error).message}`);
  }
  status();
}

async function onKey(e: KeyboardEvent): Promise<void> {
  if (e.repeat) return;
  if (e.key === " ") {
    e.preventDefault();
    if (!netOk) {
      ui.showTransientAnswer("NET DOWN: mic disabled, use V to type");
      return;
    }
    void voice.start();
    return;
  }
  if (e.key === "m" || e.key === "M") {
    setMeetingMode(!meetingMode);
    return;
  }
  if (e.key === "Escape") {
    ui.closeOverlays();
    return;
  }
  if (ui.anyOverlayOpen() && e.key.toLowerCase() !== "p" && e.key.toLowerCase() !== "k") return;
  switch (e.key) {
    case "1":
      if (meetingMode) {
        model.meetingSession = "explain-v1-01";
        room.setRun(1, "v1");
        status();
      } else await selectSession("mandate-v1-01");
      break;
    case "2":
      if (meetingMode) {
        model.meetingSession = "explain-v2-01";
        room.setRun(2, "v2");
        status();
      } else await selectSession("mandate-v2-01");
      break;
    case "r":
      await startReplay(1, true);
      break;
    case "R":
      await startReplay(8, false);
      break;
    case ".":
      if (clock.paused) {
        clock.paused = false;
        send("resume");
      } else {
        clock.paused = true;
        send("pause");
      }
      break;
    case "l":
    case "L":
      if (meetingMode) {
        await api.explainLoad(undefined, model.meetingSession.includes("v1") ? "v1" : "v2", 1).catch((err) => ui.showTransientAnswer(`load: ${(err as Error).message}`));
        break;
      }
      model.mode = "LIVE";
      status();
      await api.run().catch((err) => ui.showTransientAnswer(`live: ${(err as Error).message}`));
      break;
    case "i":
    case "I": {
      const out = await api.inject().catch((err) => ({ error: (err as Error).message }));
      if (out.error) ui.showTransientAnswer(`inject: ${out.error}`);
      else {
        pendingMandate = out.mandate_id;
        ui.showReadback(out.readback_text, `READ-BACK  ${String(out.mandate_id).toUpperCase()}  ·  C CONFIRMS`);
      }
      break;
    }
    case "c":
    case "C":
      if (!pendingMandate) break;
      await api.confirm(pendingMandate).catch((err) => ui.showTransientAnswer(`confirm: ${(err as Error).message}`));
      pendingMandate = null;
      break;
    case "o":
    case "O":
      ui.openOrderInput(
        async (text) => {
          const out = await api.compile(text).catch((err) => ({ error: (err as Error).message }));
          if (out.error) ui.showTransientAnswer(`compile: ${out.error}`);
          else if (out.questions?.length) ui.showReadback(out.readback_text, "DESK ASKS");
          else {
            pendingMandate = out.mandate_id;
            ui.showReadback(out.readback_text, `READ-BACK  ${String(out.mandate_id).toUpperCase()}  ·  C CONFIRMS`);
          }
        },
        () => undefined,
      );
      break;
    case "v":
    case "V":
      ui.openOrderInput(
        (text) => void voice.speakText(text, model.mode === "REPLAY" ? model.sessionId : null, null).catch((err) => ui.showTransientAnswer(`voice: ${(err as Error).message}`)),
        () => undefined,
      );
      break;
    case "p":
    case "P":
      if (document.getElementById("prove")!.style.display === "block") ui.closeOverlays();
      else ui.openProve(await api.prove().catch(() => ({})));
      break;
    case "k":
    case "K":
      if (document.getElementById("record")!.style.display === "block") ui.closeOverlays();
      else ui.openRecord(model);
      break;
    case "t":
    case "T":
      ui.toggleTimer();
      break;
    case "x":
    case "X": {
      const now = performance.now();
      if (now - lastX < 1000) location.reload();
      lastX = now;
      break;
    }
  }
}

async function boot(): Promise<void> {
  await Promise.all([document.fonts.load('10px "IBM Plex Mono"'), document.fonts.load('11px "Inter Tight"')]).catch(() => undefined);
  floor = new Floor(model, clock, (p) => ui.openEvidence(p));
  await floor.init(document.getElementById("floor") as HTMLCanvasElement);
  room = new Room(floor.app, (id) => void openEvidence(id));
  ui.setCash(model.cash_cents);
  ui.setPayroll(41_200_000, model.payrollDue);
  status();
  publishHook();
  if (import.meta.env.DEV) (window as any).__m = model;
  window.addEventListener("bus:event", (ev) => {
    try {
      onEvent(ev.detail);
    } catch (err) {
      console.error("event handler failed", ev.detail.type, err);
      lastEvent = { type: ev.detail.type, ts: ev.detail.ts_company };
      publishHook();
    }
  });
  window.addEventListener("keydown", (e) => void onKey(e));
  voice = new Voice({
    onRecording: (on) => {
      floor.setRecording(on);
      room.setRecording(on);
    },
    onAnswer: () => undefined, // text arrives on the bus as agent.answer / desk.answer / query.answer
    onError: (msg) => ui.showTransientAnswer(msg),
    isReplaying: () => model.mode === "REPLAY",
  });
  window.addEventListener("keyup", (e) => {
    if (e.key === " ") void voice.stop();
  });
  if (import.meta.env.DEV) {
    (window as any).__inject = (ev: BusEvent) => window.dispatchEvent(new CustomEvent<BusEvent>("bus:event", { detail: ev }));
    (window as any).__meeting = (on: boolean) => setMeetingMode(on);
  }
  connect();
  gsap.ticker.add(() => {
    ui.setClock(clock.now());
    if (window.__floor) window.__floor.fps = floor.fps;
  });
  const poll = async () => {
    const n = await netCheck();
    netOk = n.backend && n.voice;
    net = netOk ? "NET OK" : `NET DOWN${n.backend ? "" : " api"}${n.voice ? "" : " voice"}`;
    status();
  };
  void poll();
  window.setInterval(() => void poll(), 5000);
  window.setInterval(() => {
    if (wsOpen && model.mode === "REPLAY") send("status");
  }, 1000);
  void MOTION;
}

void boot();
