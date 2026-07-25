// Thin API client for the FastAPI backend.

export interface DeviceSummary {
  id: string;
  name: string;
  kind: "arm" | "liquid_handler" | "camera" | string;
  model: string;
  vendor: string;
  state: string;
  status: Record<string, unknown>;
}

export interface WorkflowEvent {
  step?: string;
  phase: string; // started | verifying | passed | retrying | failed | done
  attempt?: number;
  devices?: string[];
  capability?: string;
  verification?: { ok: boolean; confidence: number; detail: string };
}

export async function listInstruments(): Promise<DeviceSummary[]> {
  const r = await fetch("/api/instruments");
  return r.json();
}

export async function connectAll(): Promise<Record<string, string>> {
  const r = await fetch("/api/instruments/connect", { method: "POST" });
  return r.json();
}

export function cameraFrameUrl(id: string): string {
  return `/api/instruments/${id}/frame?t=${Date.now()}`;
}

// Open a websocket that emits parsed JSON messages to `onMessage`.
export function openSocket<T>(path: string, onMessage: (msg: T) => void): WebSocket {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}${path}`);
  ws.onmessage = (e) => onMessage(JSON.parse(e.data) as T);
  return ws;
}
