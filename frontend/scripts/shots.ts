/* Playwright verification: flat replay of mandate-v1-01 at speed 12, capture mandate_bound, exposure_report,
   already_executed; then mandate-v2-01 at the Halden boundary hold. Writes PNGs to shots/. */
import { spawn, type ChildProcess } from "node:child_process";
import { mkdirSync } from "node:fs";
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
  await cmd("seek", "Fri 19:15");
  await sleep(1500);

  // v2
  await page.keyboard.press("2");
  console.error("replay v2:", JSON.stringify(await post("/replay/mandate-v2-01?speed=12&stage=false")));
  await sleep(200);
  await resetSeen(page);
  await cmd("seek", "Mon 10:01:30");
  await waitEvent(page, has("held:boundary:p-001@10:03:14"));
  await sleep(700);
  await shot("v2-01-halden_hold");
  await cmd("seek", "Fri 19:15");
  ctl?.close();

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
