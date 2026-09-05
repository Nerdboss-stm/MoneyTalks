export interface BusEvent {
  type: string;
  ts_wall?: string;
  ts_company?: string;
  payload: Record<string, any>;
}

declare global {
  interface WindowEventMap {
    "bus:event": CustomEvent<BusEvent>;
    "bus:open": CustomEvent<void>;
    "bus:close": CustomEvent<void>;
  }
}

export const API = "/api";
const URL = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/ws/events`;
const RECONNECT_MS = 2000;

let socket: WebSocket | null = null;
let timer: number | null = null;
export let wsOpen = false;

function scheduleReconnect(): void {
  if (timer !== null) return;
  timer = window.setTimeout(() => {
    timer = null;
    connect();
  }, RECONNECT_MS);
}

export function connect(): void {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
  socket = new WebSocket(URL);
  socket.onopen = () => {
    wsOpen = true;
    window.dispatchEvent(new CustomEvent("bus:open"));
  };
  socket.onmessage = (m: MessageEvent<string>) => {
    let ev: BusEvent;
    try {
      ev = JSON.parse(m.data) as BusEvent;
    } catch {
      return;
    }
    if (!ev || typeof ev.type !== "string") return;
    window.dispatchEvent(new CustomEvent<BusEvent>("bus:event", { detail: ev }));
    window.dispatchEvent(new CustomEvent<BusEvent>(`bus:${ev.type}`, { detail: ev }));
  };
  socket.onclose = () => {
    socket = null;
    wsOpen = false;
    window.dispatchEvent(new CustomEvent("bus:close"));
    scheduleReconnect();
  };
  socket.onerror = () => {
    socket?.close();
  };
}

export function send(cmd: string, arg?: unknown): boolean {
  if (!socket || socket.readyState !== WebSocket.OPEN) return false;
  socket.send(JSON.stringify(arg === undefined ? { cmd } : { cmd, arg }));
  return true;
}
