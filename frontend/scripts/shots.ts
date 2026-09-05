/* Playwright verification: flat replay of mandate-v1-01 at speed 12, capture mandate_bound, exposure_report,
   already_executed; then mandate-v2-01 at the Halden boundary hold. Writes PNGs to shots/. */
import { spawn, type ChildProcess } from "node:child_process";
import { mkdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { chromium, type Page } from "playwright";

const ROOT = resolve(import.meta.dirname ?? ".", "..", "..");
const API = "http://localhost:8000";
const APP = "http://localhost:5173";
const OUT = resolve(ROOT, "shots");

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function waitFor(url: string, ms = 60000): Promise<void> {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    try {
      const r = await fetch(url);
      if (r.ok) return;
    } catch {
      /* retry */
    }
    await sleep(300);
  }
  throw new Error(`timeout waiting for ${url}`);
}

async function alreadyUp(url: string): Promise<boolean> {
  try {
    return (await fetch(url)).ok;
  } catch {
    return false;
  }
}

async function post(path: string, body?: unknown): Promise<any> {
  const r = await fetch(API + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  return r.json().catch(() => ({}));
}

let ctl: WebSocket | null = null;
async function control(): Promise<WebSocket> {
  if (ctl && ctl.readyState === WebSocket.OPEN) return ctl;
  ctl = new WebSocket("ws://localhost:8000/ws/events");
  await new Promise<void>((ok, bad) => {
    ctl!.addEventListener("open", () => ok());
    ctl!.addEventListener("error", () => bad(new Error("ws control failed")));
  });
  return ctl;
}

// /replay/cmd over HTTP is shadowed by /replay/{session_id}; use the WS command channel.
async function cmd(c: string, arg?: unknown): Promise<void> {
  (await control()).send(JSON.stringify(arg === undefined ? { cmd: c } : { cmd: c, arg }));
  await sleep(100);
}

async function waitEvent(page: Page, pred: (seen: string[]) => boolean, ms = 120000): Promise<void> {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    const seen: string[] = (await page.evaluate(() => (window as any).__floor?.seen)) ?? [];
    if (pred(seen)) return;
    await sleep(50);
  }
  throw new Error("timeout waiting for event");
}

const has = (prefix: string) => (seen: string[]) => seen.some((s) => s.startsWith(prefix));
const resetSeen = (page: Page) => page.evaluate(() => ((window as any).__floor.seen.length = 0));

async function main(): Promise<void> {
  mkdirSync(OUT, { recursive: true });
  const children: ChildProcess[] = spawned;
  if (!(await alreadyUp(`${API}/health`))) {
    children.push(spawn("uv", ["run", "uvicorn", "app:app", "--port", "8000"], { cwd: resolve(ROOT, "backend"), stdio: "ignore", env: { ...process.env, MANDATE_DECIDER: "rules", PRISM_HANDLERS: "off" } }));
  }
  if (!(await alreadyUp(APP))) {
    children.push(spawn("npx", ["vite", "--port", "5173", "--strictPort"], { cwd: resolve(ROOT, "frontend"), stdio: "ignore" }));
  }
  await waitFor(`${API}/health`);
  await waitFor(APP);

  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
  const fpsLog: number[] = [];
  page.on("console", (m) => {
    const t = m.text();
    if (t.startsWith("fps ")) fpsLog.push(Number(t.slice(4)));
  });
  await page.goto(APP);
  await page.waitForFunction(() => (window as any).__floor?.ready, null, { timeout: 30000 });
  await sleep(1500);

  const shots: string[] = [];
  const shot = async (name: string) => {
    const p = resolve(OUT, `${name}.png`);
    await page.screenshot({ path: p });
    shots.push(p);
  };

  // v1
  await page.keyboard.press("1");
  await cmd("seek", "Fri 19:15");
  await sleep(300);
  console.error("replay v1:", JSON.stringify(await post("/replay/mandate-v1-01?speed=12&stage=false")));
  await sleep(200);
  await resetSeen(page);
  await cmd("seek", "Mon 10:01:30");
  await waitEvent(page, has("mandate.bound@"));
  await sleep(400);
  await shot("v1-01-mandate_bound");
  await waitEvent(page, has("exposure.report@"));
  await sleep(1200);
  await shot("v1-02-exposure_report");
  await waitEvent(page, has("already_executed"));
  await sleep(700);
  await shot("v1-03-already_executed");
  await cmd("pause");
  await page.evaluate(() => (window as any).__inject({ type: "agent.addressed", payload: { agent_id: "ap_west" } }));
  await sleep(500);
  await shot("v1-04-agent_addressed");
  await page.evaluate(() => (window as any).__inject({ type: "agent.released", payload: { agent_id: "ap_west" } }));
  await cmd("resume");
  await cmd("seek", "Fri 19:15");
  await sleep(1500);

  // v2
  await page.keyboard.press("2");
  console.error("replay v2:", JSON.stringify(await post("/replay/mandate-v2-01?speed=12&stage=false")));
  await sleep(200);
  await resetSeen(page);
  await cmd("seek", "Mon 10:01:30");
  await waitEvent(page, has("mandate.bound@"));
  await sleep(400);
  await shot("v2-01-mandate_bound");
  await waitEvent(page, has("exposure.report@"));
  await sleep(1200);
  await shot("v2-02-exposure_report");
  await waitEvent(page, has("held:boundary:p-001@10:03:14"));
  await sleep(700);
  await shot("v2-03-halden_hold");
  await cmd("seek", "Fri 19:15");
  ctl?.close();

  // MEETING mode: drive the room by injecting the recorded meeting events with real pacing
  const rec = (sid: string) => readFileSync(resolve(ROOT, "recordings", `${sid}.jsonl`), "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l));
  const inject = (ev: any) => page.evaluate((e) => (window as any).__inject(e), ev);
  const turns = (events: any[], agent: string) => {
    const kinds = ["agent.addressed", "agent.retrieving", "agent.verified", "agent.traced", "agent.answer", "agent.released"];
    const out: Record<string, any> = {};
    let seen = false;
    for (const e of events) {
      if (e.type === "agent.addressed") seen = e.payload.agent_id === agent && !out["agent.answer"];
      if (seen && kinds.includes(e.type) && !out[e.type]) out[e.type] = e;
    }
    return out;
  };
  const v1 = rec("explain-v1-01");
  const v2 = rec("explain-v2-01");
  const answerText = () => page.evaluate(() => document.getElementById("room-a")?.textContent ?? "");
  const answerShown = () => page.waitForFunction(() => document.getElementById("room-a")?.style.display === "block", null, { timeout: 60000 });
  const answerGone = () => page.waitForFunction(() => document.getElementById("room-a")?.style.display !== "block", null, { timeout: 120000 });
  const answerHas = (s: string) => page.waitForFunction((needle) => (document.getElementById("room-a")?.textContent ?? "").includes(needle), s, { timeout: 120000 });
  await page.evaluate(() => (window as any).__meeting(true));
  await inject(v1.find((e) => e.type === "meeting.loaded"));
  await sleep(700);
  await shot("room-01-rest");
  const t1 = turns(v1, "procurement");
  await inject(t1["agent.addressed"]);
  await sleep(500);
  await inject(t1["agent.retrieving"]);
  await sleep(1600);
  await shot("room-02-addressed-rows");
  await inject(t1["agent.verified"]);
  await sleep(1800);
  await shot("room-03-v1-verified-red");
  await inject(t1["agent.traced"]);
  await inject({ ...t1["agent.answer"], payload: { ...t1["agent.answer"].payload, duration_ms: 1500 } });
  await answerShown();
  await inject(t1["agent.released"]);
  await answerGone();
  await sleep(400);
  await page.keyboard.press("2");
  await inject(v2.find((e) => e.type === "meeting.loaded"));
  const t2 = turns(v2, "procurement");
  await inject(t2["agent.addressed"]);
  await inject(t2["agent.retrieving"]);
  await inject(t2["agent.verified"]);
  await inject(t2["agent.traced"]);
  await inject(t2["agent.answer"]);
  // the frame is taken the moment a row figure is printed, while that token is lit
  const rowFigs: string[] = t2["agent.retrieving"].payload.rows.flatMap((r: any) => r.figures ?? []);
  const words: string[] = t2["agent.answer"].payload.text.split(/\s+/);
  const lit = rowFigs.find((f) => words.some((w) => w.includes(f)));
  if (lit) await answerHas(lit);
  else {
    await answerShown();
    await sleep(1500);
  }
  await shot("room-04-v2-answer-midprint");
  console.error("room-04 lit figure:", lit ?? "(none)", "printed:", (await answerText()).slice(0, 120));
  await inject(t2["agent.released"]);
  await answerGone();
  await sleep(400);
  // cross-examination: the next speaker cites rows the previous speaker cited
  const t3 = turns(v2, "controller_a");
  await inject({ ...t3["agent.addressed"], payload: { ...t3["agent.addressed"].payload, question: "Controller A, do you agree with Procurement's read?" } });
  await inject({ ...t3["agent.retrieving"], payload: { ...t3["agent.retrieving"].payload, rows: t2["agent.retrieving"].payload.rows } });
  await inject({ ...t3["agent.verified"], payload: { ...t3["agent.verified"].payload, checks: [] } });
  await inject(t3["agent.traced"]);
  await inject({ ...t3["agent.answer"], payload: { ...t3["agent.answer"].payload, text: `I read the same rows: cloud hosting is the expense story ${t2["agent.answer"].payload.citations.map((c: string) => `[${c}]`).join(" ")}.`, citations: t2["agent.answer"].payload.citations, duration_ms: 3000 } });
  await answerHas("story");
  await sleep(300);
  await shot("room-05-cross-examination");
  await inject(t3["agent.released"]);
  await answerGone();
  await sleep(400);
  const learned = v2.find((e) => e.type === "memory.learned");
  await inject({ ...learned, type: "meeting.learned" });
  await sleep(700);
  await shot("room-06-learned");

  const fps = fpsLog.length ? fpsLog.slice(-8) : [await page.evaluate(() => (window as any).__floor?.fps)];
  await browser.close();
  for (const c of children) c.kill();
  console.log(JSON.stringify({ shots, fps_samples: fps, fps_avg: Math.round(fps.reduce((a, b) => a + b, 0) / fps.length) }, null, 2));
  process.exit(0);
}

const spawned: ChildProcess[] = [];
process.on("exit", () => spawned.forEach((c) => c.kill()));
main().catch((err) => {
  console.error(err);
  process.exit(1);
});
