export interface BusEvent {
  type: string;
  ts_wall: string;
  ts_company: string;
  payload: Record<string, unknown>;
}

export type BusEventDetail = BusEvent;

declare global {
  interface WindowEventMap {
    "bus:event": CustomEvent<BusEventDetail>;
    "bus:open": CustomEvent<void>;
    "bus:close": CustomEvent<void>;
  }
}

const URL = "ws://localhost:8000/ws/events";
const RECONNECT_MS = 2000;

let socket: WebSocket | null = null;
let timer: number | null = null;

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
    window.dispatchEvent(new CustomEvent("bus:open"));
  };
  socket.onmessage = (m: MessageEvent<string>) => {
    let ev: BusEvent;
    try {
      ev = JSON.parse(m.data) as BusEvent;
    } catch {
      return;
    }
    window.dispatchEvent(new CustomEvent<BusEventDetail>("bus:event", { detail: ev }));
    window.dispatchEvent(new CustomEvent<BusEventDetail>(`bus:${ev.type}`, { detail: ev }));
  };
  socket.onclose = () => {
    socket = null;
    window.dispatchEvent(new CustomEvent("bus:close"));
    scheduleReconnect();
  };
  socket.onerror = () => {
    socket?.close();
  };
}
