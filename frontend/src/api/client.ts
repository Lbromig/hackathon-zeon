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

// --- camera preflight / streaming / snapshots (see backend/app/api/camera.py) ---

export interface CameraPreflightDevice {
  name: string;
  model: string;
  serial: string | null;
  vendor_id: number | null;
  product_id: number | null;
  link_speed_bps: number | null;
  is_realsense: boolean;
  is_usb3: boolean | null;   // null when the link speed could not be read
  claimed_by: string[];
}

export interface CameraPreflightBackend {
  backend: string;
  diagnosis: string;         // ok | permission_denied | exclusive_access | ...
  detail: string;
  remedy: string;
}

export interface CameraPreflightResult {
  usable: boolean;           // true only if frames actually flowed
  responsible_app: string;
  devices: CameraPreflightDevice[];
  backends: CameraPreflightBackend[];
}

export interface CameraSnapshot {
  name: string;
  path: string;
  bytes: number;
  label?: string;
}

export async function getCameraPreflight(): Promise<CameraPreflightResult> {
  const r = await fetch("/api/camera/preflight");
  if (!r.ok) throw new Error(`preflight failed: ${r.status}`);
  return r.json();
}

export async function takeSnapshot(label = ""): Promise<CameraSnapshot> {
  const r = await fetch(`/api/camera/snapshot?label=${encodeURIComponent(label)}`, {
    method: "POST",
  });
  if (!r.ok) throw new Error((await r.json()).detail ?? `snapshot failed: ${r.status}`);
  return r.json();
}

export async function listSnapshots(): Promise<CameraSnapshot[]> {
  const r = await fetch("/api/camera/snapshots");
  return r.ok ? r.json() : [];
}

// WebRTC. Lower latency than MJPEG, at the cost of aiortc on the backend and a
// signalling round trip here. Falls back to the caller on 503.
export async function startWebRTC(pc: RTCPeerConnection): Promise<void> {
  pc.addTransceiver("video", { direction: "recvonly" });
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  const r = await fetch("/api/camera/webrtc/offer", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sdp: offer.sdp, type: offer.type }),
  });
  if (!r.ok) throw new Error((await r.json()).detail ?? `webrtc failed: ${r.status}`);
  await pc.setRemoteDescription(await r.json());
}
