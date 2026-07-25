// Client for the teach/jog API (backend/app/api/teach.py).
//
// Every command answers with fresh arm state, so callers never need a follow-up
// GET. Transport-level failures are turned into the same {ok, detail} shape the
// backend uses — the UI only has one error path to render.

export interface Gripper {
  kind: "parallel" | "lite6" | "bio" | "none" | "unknown";
  supports_width: boolean;
  min_width: number;
  max_width: number;
  units: string; // "counts" on the xArm parallel gripper — NOT metres
  stroke_m: number; // physical opening at max_width, for display only
  width: number | null;
}

export interface ArmLimits {
  joints: [number, number][] | null;
  max_jog_linear: number;
  max_jog_angular: number;
  max_speed_linear: number;
  max_speed_angular: number;
  max_move_to_jump: number;
}

export interface PoseValues {
  x: number;
  y: number;
  z: number;
  roll: number;
  pitch: number;
  yaw: number;
}

export interface ArmSummary {
  id: string;
  name: string;
  model: string;
  state: string;
  connected: boolean;
  axis_count: number;
  gripper: Gripper;
  limits: ArmLimits;
}

export interface ArmState {
  id: string;
  state: string;
  connected: boolean;
  busy: boolean;
  pose: PoseValues | null;
  joints: number[] | null;
  gripper: Gripper;
  error_code: number | null;
  warn_code: number | null;
  detail: string;
}

export interface ActionResult {
  ok: boolean;
  detail: string;
  state?: ArmState | null;
}

export interface TaughtPose {
  name: string;
  pose: PoseValues | null;
  joints: number[] | null;
  gripper_width: number | null;
  note: string;
  saved_at: string;
}

export type JogSpace = "cartesian" | "joint";
export const CARTESIAN_AXES = ["x", "y", "z", "roll", "pitch", "yaw"] as const;
export type CartesianAxis = (typeof CARTESIAN_AXES)[number];
export const LINEAR_AXES: string[] = ["x", "y", "z"];

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, {
    headers: init?.body ? { "Content-Type": "application/json" } : undefined,
    ...init,
  });
  if (!r.ok) {
    // FastAPI puts validation/busy errors in `detail`; surface that verbatim.
    let detail = `HTTP ${r.status}`;
    try {
      const body = await r.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return r.json() as Promise<T>;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const listArms = () => request<ArmSummary[]>("/api/arms");
export const getArmState = (id: string) => request<ArmState>(`/api/arms/${id}/state`);

export const jog = (id: string, space: JogSpace, axis: string, delta: number, speed?: number) =>
  post<ActionResult>(`/api/arms/${id}/jog`, { space, axis, delta, speed });

export const moveToPose = (id: string, pose: PoseValues, speed?: number) =>
  post<ActionResult>(`/api/arms/${id}/move_to`, { pose, speed });

export const moveToJoints = (id: string, joints: number[], speed?: number) =>
  post<ActionResult>(`/api/arms/${id}/move_to`, { joints, speed });

export const setGripper = (id: string, action: "open" | "close" | "set", width?: number) =>
  post<ActionResult>(`/api/arms/${id}/gripper`, { action, width });

export const home = (id: string) => post<ActionResult>(`/api/arms/${id}/home`);
export const enableArm = (id: string, on: boolean) => post<ActionResult>(`/api/arms/${id}/enable`, { on });
export const clearErrors = (id: string) => post<ActionResult>(`/api/arms/${id}/clear_errors`);
export const stopArm = (id: string, emergency = true) =>
  post<ActionResult>(`/api/arms/${id}/stop`, { emergency });

// Connecting is a fleet-level concern; the teach tab just borrows the endpoint.
export const connectArm = (id: string) => post<ActionResult>(`/api/instruments/${id}/connect`);

export const listPoses = (id: string) => request<TaughtPose[]>(`/api/arms/${id}/poses`);
export const savePose = (id: string, name: string, note = "") =>
  post<TaughtPose[]>(`/api/arms/${id}/poses`, { name, note });
export const deletePose = (id: string, name: string) =>
  request<TaughtPose[]>(`/api/arms/${id}/poses/${encodeURIComponent(name)}`, { method: "DELETE" });
export const gotoPose = (id: string, name: string, speed?: number) =>
  post<ActionResult>(
    `/api/arms/${id}/poses/${encodeURIComponent(name)}/goto${speed ? `?speed=${speed}` : ""}`,
  );
