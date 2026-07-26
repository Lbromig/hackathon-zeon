// Camera feeds + detections (backend/app/api/cameras.py).
//
// The video is a plain MJPEG <img> src; detections arrive separately (here, or
// live on /ws/state) so the overlay can be redrawn without touching the video.

/** Pinhole intrinsics. RealSense reports these from the factory — no ChArUco pass. */
export interface Intrinsics {
  fx: number;
  fy: number;
  cx: number;
  cy: number;
  width: number;
  height: number;
  coeffs: number[];
}

export interface Detection {
  kind: string; // apriltag | tube | cap | well ...
  polygon: [number, number][]; // normalized [0,1] image coords
  center: [number, number];
  source: "apriltag" | "cv" | "projection" | string;
  marker_id: number | null;
  entity_id: string | null;
  confidence: number;
  distance_m: number | null; // from the tag pose; needs intrinsics
  depth_m: number | null; // measured by the depth sensor; RGB-D only
  camera_xyz: [number, number, number] | null; // metres, camera frame
}

/** Per-camera block carried on /ws/state (only cameras currently streaming). */
export interface CameraFrameState {
  w: number;
  h: number;
  seq: number;
  fps: number;
  error: string;
  has_depth: boolean;
  intrinsics: Intrinsics | null;
  detections: Detection[];
}

export interface CameraSummary {
  id: string;
  name: string;
  model: string;
  state: string;
  connected: boolean;
  streaming: boolean;
  width: number;
  height: number;
  fps: number;
  error: string;
  has_depth: boolean;
  serial: string;
  configured: { width?: number; height?: number; fps?: number };
  intrinsics: Intrinsics | null;
}

/** A physically attached RealSense — what you paste into CAM_* in .env. */
export interface RealSenseDevice {
  serial: string;
  name: string;
  firmware: string;
  assigned_to: string | null;
}

export interface CameraDevices {
  devices: RealSenseDevice[];
  error: string;
}

export const listCameras = async (): Promise<CameraSummary[]> => {
  const r = await fetch("/api/cameras");
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
};

/** Enumerate attached RealSense units. Reports its own failure in `error`. */
export const listDevices = async (): Promise<CameraDevices> => {
  const r = await fetch("/api/cameras/devices");
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
};

export const connectCamera = async (id: string): Promise<{ ok: boolean; detail: string }> => {
  const r = await fetch(`/api/cameras/${id}/connect`, { method: "POST" });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
};

/**
 * MJPEG endpoint for an <img>. The cache-buster matters: without it a browser
 * will happily reuse a dead multipart response after a reconnect.
 */
export const streamUrl = (id: string, nonce: number | string = ""): string =>
  `/api/cameras/${id}/stream${nonce ? `?t=${nonce}` : ""}`;

export const snapshotUrl = (id: string): string =>
  `/api/cameras/${id}/snapshot?t=${Date.now()}`;

export const stopCamera = async (id: string): Promise<void> => {
  await fetch(`/api/cameras/${id}/stop`, { method: "POST" });
};
