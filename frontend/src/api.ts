import { API } from "./ws";

async function post(path: string, body?: unknown): Promise<any> {
  const r = await fetch(API + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await r.text();
  let data: any = text;
  try {
    data = JSON.parse(text);
  } catch {
    /* plain text */
  }
  if (!r.ok) throw new Error(typeof data === "object" && data?.detail ? String(data.detail) : `${r.status} ${path}`);
  return data;
}

async function get(path: string): Promise<any> {
  const r = await fetch(API + path);
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json();
}

export const api = {
  health: () => get("/health"),
  replay: (session: string, speed: number, stage = false) => post(`/replay/${session}?speed=${speed}&stage=${stage ? "true" : "false"}`),
  cmd: (cmd: string, arg?: unknown) => post("/replay/cmd", { cmd, arg }),
  run: () => post("/run"),
  inject: () => post("/desk/inject"),
  compile: (text: string) => post("/desk/compile", { text }),
  confirm: (mandate_id: string) => post("/desk/confirm", { mandate_id }),
  prove: (v1 = "mandate-v1-01", v2 = "mandate-v2-01") => get(`/prove?v1=${v1}&v2=${v2}`),
  record: (agent: string) => get(`/record/${agent}`),
  explainLoad: (dir?: string, mode = "v2", index = 1) => post("/explain/load", { dir, mode, index }),
  explainEvidence: () => get("/explain/evidence"),
};

export async function netCheck(): Promise<{ backend: boolean; voice: boolean }> {
  const backend = await api
    .health()
    .then(() => true)
    .catch(() => false);
  const voice = await fetch("https://api.elevenlabs.io/", { mode: "no-cors", cache: "no-store" })
    .then(() => true)
    .catch(() => false);
  return { backend, voice };
}
