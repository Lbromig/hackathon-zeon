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

/** A pose the hero workflow needs. Derived server-side from the choreography, so
 *  this list cannot drift from what the workflow actually visits. */
export interface RequiredPose {
  device: string;
  name: string;
  step: string;
  order: number;
  note: string;
  taught: boolean;
  saved_at: string;
}

export interface PreflightResult {
  ok: boolean;
  problems: string[];
  required: RequiredPose[];
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

/** Hand-guiding. The arm becomes back-drivable — support it before enabling, and
 *  note that commanded moves do not behave normally until it is switched off. */
export const setFreeDrive = (id: string, on: boolean) =>
  post<ActionResult>(`/api/arms/${id}/free_drive`, { on });

/** A hand-guided travel route, stored as joint waypoints. */
export interface TaughtPath {
  name: string;
  waypoints: number[][];
  recorded_at: string;
  note: string;
  raw_samples: number;
  length_deg: number;
}

export interface PathRecordState {
  recording: boolean;
  name: string;
  samples: number;
  duration_s: number;
  detail: string;
}

export const listPaths = (id: string) => request<TaughtPath[]>(`/api/arms/${id}/paths`);
export const getPathRecording = (id: string) =>
  request<PathRecordState>(`/api/arms/${id}/paths/recording`);
/** Starts sampling AND switches the arm to hand-guiding — you cannot walk a route
 *  the arm will not let you move. */
export const startPathRecording = (id: string, name: string) =>
  post<ActionResult>(`/api/arms/${id}/paths/${encodeURIComponent(name)}/record`);
export const stopPathRecording = (id: string, name: string, note = "") =>
  post<ActionResult>(
    `/api/arms/${id}/paths/${encodeURIComponent(name)}/record/stop?note=${encodeURIComponent(note)}`,
  );
export const deletePath = (id: string, name: string) =>
  request<TaughtPath[]>(`/api/arms/${id}/paths/${encodeURIComponent(name)}`, { method: "DELETE" });
/** `blend` (deg) arcs through corners instead of stopping at each waypoint. It is
 *  clamped server-side to the shortest segment, since the controller rejects a radius
 *  longer than the track. */
export const replayPath = (
  id: string,
  name: string,
  opts: { speed?: number; reverse?: boolean; blend?: number } = {},
) => {
  const q = new URLSearchParams();
  if (opts.speed) q.set("speed", String(opts.speed));
  if (opts.reverse) q.set("reverse", "true");
  if (opts.blend) q.set("blend", String(opts.blend));
  const qs = q.toString();
  return post<ActionResult>(
    `/api/arms/${id}/paths/${encodeURIComponent(name)}/replay${qs ? `?${qs}` : ""}`,
  );
};

/** Thin a saved path in place — fewer waypoints, fewer stops. Destructive. */
export const simplifyPath = (id: string, name: string, tolerance: number) =>
  post<TaughtPath>(
    `/api/arms/${id}/paths/${encodeURIComponent(name)}/simplify?tolerance=${tolerance}`,
  );

export type CapAction = "grab" | "ungrab" | "unscrew";

/** Cap manipulation. `unscrew` is a ratchet: 180° bites with the jaws opening and the
 *  wrist unwinding between them, because the tool cabling cannot take a full 360°. */
export const capAction = (
  id: string,
  action: CapAction,
  opts: { half_turns?: number; width?: number; speed?: number } = {},
) => post<ActionResult>(`/api/arms/${id}/cap`, { action, ...opts });

// Workflow readiness. Lives under /api/workflow but is consumed by the teach tab:
// the checklist is what gets the operator from "nothing taught" to a green pre-flight.
export const listRequiredPoses = () => request<RequiredPose[]>("/api/workflow/required_poses");
export const getPreflight = () => request<PreflightResult>("/api/workflow/preflight");
