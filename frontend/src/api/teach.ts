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
  max_move_to_rotation: number;
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
  /** Hand-guiding is active (xArm mode 2): the arm is back-drivable and commanded
   *  motion will not behave normally until it is switched off. */
  free_drive: boolean;
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

export type SpeedTier = "slow" | "medium" | "fast";

/** One row of the workflow-waypoint checklist, from `core/waypoints.py`'s spec. */
export interface WaypointRow {
  device: string;
  name: string;
  /** The workflow step it serves — also the order it makes sense to teach in. */
  step: number;
  /** Intended tier; the backend resolves it to mm/s and °/s and clamps to soft limits. */
  speed: SpeedTier;
  note: string;
  taught: boolean;
  saved_at: string | null;
  /** Joints were captured, so replay uses angles the arm physically reached. */
  has_joints: boolean;
}

/** One arm's checklist. Contains **only** that arm's own waypoints — the API is the
 *  reason a picker built from this cannot express an invalid pairing (R-WP-3). */
export interface ArmWaypoints {
  device: string;
  taught: number;
  total: number;
  complete: boolean;
  waypoints: WaypointRow[];
  /** Taught names that are not spec waypoints for this arm — scratch points. */
  extra: string[];
}

export interface WaypointProblem {
  /** missing | wrong_device | name_mismatch | ad_hoc | unusable */
  kind: string;
  device: string;
  name: string;
  detail: string;
  blocking: boolean;
}

export interface WaypointReport {
  devices: ArmWaypoints[];
  problems: WaypointProblem[];
  total: number;
  taught: number;
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
/** Replay a taught pose. `tier` wins over `speed` on the backend, so the checklist can
 *  replay a waypoint at the speed the workflow will use rather than the jog slider's. */
export const gotoPose = (
  id: string,
  name: string,
  opts: { speed?: number; tier?: SpeedTier } = {},
) => {
  const q = new URLSearchParams();
  if (opts.tier) q.set("tier", opts.tier);
  else if (opts.speed) q.set("speed", String(opts.speed));
  const query = q.toString();
  return post<ActionResult>(
    `/api/arms/${id}/poses/${encodeURIComponent(name)}/goto${query ? `?${query}` : ""}`,
  );
};

/** The whole fleet's waypoint checklist plus everything wrong with the library.
 *  One request, because the panel needs the arm's rows and its warnings together. */
export const getWaypointReport = () => request<WaypointReport>("/api/arms/waypoints");

/** One arm's checklist. Only that arm's own waypoints, by construction. */
export const listWaypoints = (id: string) => request<ArmWaypoints>(`/api/arms/${id}/waypoints`);

/** Hand-guiding. The arm becomes back-drivable — support it before enabling, and
 *  note that commanded moves do not behave normally until it is switched off. */
export const setFreeDrive = (id: string, on: boolean) =>
  post<ActionResult>(`/api/arms/${id}/free_drive`, { on });

export type CapAction = "grab" | "ungrab" | "unscrew";

/** Cap manipulation. `unscrew` is a ratchet: 180° bites with the jaws opening and the
 *  wrist unwinding between them, because the tool cabling cannot take a full 360°. */
export const capAction = (
  id: string,
  action: CapAction,
  opts: { half_turns?: number; width?: number; speed?: number } = {},
) => post<ActionResult>(`/api/arms/${id}/cap`, { action, ...opts });
