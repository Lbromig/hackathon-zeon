// The engine's HTTP + websocket surface, and the TypeScript half of the frozen contracts.
//
// Everything the Workflow tab knows about the backend lives in this one file on purpose:
// `backend/app/api/engine.py` is being built in parallel against
// `docs/v2/EXECUTION_AND_VISUALIZATION.md` §2, so when the real routes land, any drift is
// absorbed here and no component changes.
//
// The types below are transcribed from `backend/app/engine/{actions,events}.py`, which are
// frozen. Field names are theirs, not ours — a renamed field here is a silently blank row.
// Every `Outputs` field is treated as *possibly absent* (`?`), because a handler that has not
// been written yet, or an older backend, simply will not send it: absent must render as
// "unknown", never as `false` or `0`.

// --- shared vocabulary (actions.py) -----------------------------------------

export type SpeedTier = "slow" | "medium" | "fast";
export type FailurePolicy = "halt" | "continue" | "retry";
export type SlotName = "frame" | "tip" | "tube" | "offset" | "selected_offset";
export type ActionState = "planned" | "running" | "complete" | "failed" | "skipped" | "aborted";
export type Origin = "plan" | "inject" | "expand";

/** `RunState` — `pausing` is a real state, never to be rendered as `paused` (D9/§3.5). */
export type RunState =
  | "idle" | "preflight" | "running" | "pausing" | "paused"
  | "complete" | "failed" | "aborted";

export type ReadinessState = "initializing" | "ready" | "degraded" | "failed";

/** The 14 kinds, in `ACTION_KINDS` order. */
export const ACTION_KINDS = [
  "lifecycle.initialize",
  "lifecycle.reconnect",
  "arm.waypoint",
  "arm.move_relative",
  "arm.gripper",
  "arm.decap",
  "arm.traverse",
  "lh.move_relative",
  "camera.snapshot",
  "camera.search_code",
  "vision.identify",
  "vision.solve_offset",
  "control.loop",
  "control.checkpoint",
] as const;
export type ActionKind = (typeof ACTION_KINDS)[number];

// --- typed outputs, one per kind (actions.py `OUTPUTS_FOR_KIND`) -------------
//
// The discriminant is `kind` on the *outputs*, which is NOT the action kind — e.g.
// `arm.waypoint` returns `kind: "move"`. `OUTPUTS_KIND_FOR_ACTION` below maps between them.

export interface InitializeOutputs {
  kind: "initialize";
  devices?: { device?: string; connected?: boolean; detail?: string; simulated?: boolean }[];
  homed?: string[];
  home_missing?: string[];
}

export interface ReconnectOutputs {
  kind: "reconnect";
  scope: "connect" | "enable" | "engage";
  connected?: boolean;
  enabled?: boolean;
  /** `engage` verifies with a zero-distance move — "enabled" is a weaker claim (Q5). */
  verified?: boolean;
  cleared_errors?: string[];
}

export interface MoveOutputs {
  kind: "move";
  pose_before?: number[];
  pose_after?: number[];
  joints_before?: number[];
  joints_after?: number[];
  waypoint?: string | null;
  offsets_mm?: Record<string, number>;
  resolved_speed?: Record<string, unknown>;
  /** Empty until review B7 lands in `arm.py`; treat `""`/absent as "not recorded". */
  path?: "joint_replay" | "joint_replay+cartesian_offset" | "cartesian" | "relative" | "";
}

export interface GripperOutputs {
  kind: "gripper";
  state: "open" | "close";
  width_before?: number | null;
  width_after?: number | null;
}

export interface DecapOutputs {
  kind: "decap";
  bites?: number;
  step_deg?: number;
  total_rotation_deg?: number;
  net_wrist_travel_deg?: number;
  preflight_ok?: boolean;
  /** Also empty until B7 — the reachable half is a `wrist_rewound_before_decap` warning. */
  wrist_rewound_before_decap?: boolean;
  rewind_deg?: number;
}

export interface TraverseOutputs {
  kind: "traverse";
  reached?: string[];
  /** The radius actually used after the shortest-segment clamp, not the one requested. */
  blend_deg?: number | null;
  resolved_speed?: Record<string, unknown>;
}

export interface LHMoveOutputs {
  kind: "lh_move";
  requested_mm?: Record<string, number>;
  applied_mm?: Record<string, number>;
  position_before?: Record<string, number>;
  position_after?: Record<string, number>;
  /** R-LH-4: a dead-reckoned position must not read as a measurement. */
  provenance?: "measured" | "dead_reckoned";
  drift_mm?: number | null;
}

export interface SnapshotOutputs {
  kind: "snapshot";
  width?: number;
  height?: number;
  captured_at?: string;
  /** True when the frame predates the motion before it. Absent = unknown, never false. */
  stale?: boolean;
  achieved_mode?: string;
  stream?: "color" | "ir" | "unknown";
}

export interface SearchCodeOutputs {
  kind: "search_code";
  marker_id: number;
  found?: boolean;
  center_px?: number[] | null;
  elapsed_s?: number;
}

export interface IdentifyOutputs {
  kind: "identify";
  target: "tip" | "tube";
  found?: boolean;
  point_px?: number[] | null;
  score?: number;
  method?: "tag_anchored" | "classical" | "none";
  marker_id?: number | null;
  T_cam_feature?: number[][] | null;
}

export interface OffsetOutputs {
  kind: "offset";
  /** Per axis, `null` where unobservable — which is not the same as 0.0. */
  residual_offset_mm?: Record<string, number | null>;
  /** Over the observed axes only, and `null` unless every axis is observed (B9). */
  magnitude_mm?: number | null;
  sigma_mm?: Record<string, number>;
  /** Advisory only (Q4/D16) — never presentable as a convergence gate (§3.5). */
  view_disagreement_mm?: number | null;
  observed_axes?: string[];
  method?: "tag_3d" | "axis_decoupled_jacobian" | "refused";
  refusal?: string;
  singular_values?: number[];
  condition_number?: number | null;
  contributions?: Record<string, unknown>[];
}

export interface LoopOutputs {
  kind: "loop";
  /** `aborted` is also what a *child failure* produces (review non-blocking 1) — take
   *  `ActionResult.status` and `ErrorInfo.message` as authority when rendering. */
  outcome?: "converged" | "stalled" | "aborted" | "exhausted";
  iterations?: number;
  materialized?: number;
  final_magnitude_mm?: number | null;
  threshold_mm?: number;
}

export interface CheckpointOutputs {
  kind: "checkpoint";
  acknowledged?: boolean;
  message?: string;
}

export type Outputs =
  | InitializeOutputs | ReconnectOutputs | MoveOutputs | GripperOutputs | DecapOutputs
  | TraverseOutputs | LHMoveOutputs | SnapshotOutputs | SearchCodeOutputs
  | IdentifyOutputs | OffsetOutputs | LoopOutputs | CheckpointOutputs;

/** `OUTPUTS_FOR_KIND`, as the outputs discriminant rather than the model class. */
export const OUTPUTS_KIND_FOR_ACTION: Record<ActionKind, Outputs["kind"]> = {
  "lifecycle.initialize": "initialize",
  "lifecycle.reconnect": "reconnect",
  "arm.waypoint": "move",
  "arm.move_relative": "move",
  "arm.gripper": "gripper",
  "arm.decap": "decap",
  "arm.traverse": "traverse",
  "lh.move_relative": "lh_move",
  "camera.snapshot": "snapshot",
  "camera.search_code": "search_code",
  "vision.identify": "identify",
  "vision.solve_offset": "offset",
  "control.loop": "loop",
  "control.checkpoint": "checkpoint",
};

// --- the result (actions.py) -------------------------------------------------

export interface Artifact {
  kind: "image/frame" | "image/overlay" | "json";
  /** Absolute on disk today (review non-blocking 4); never load it directly — use
   *  `artifactUrl()`, which goes through the run's artifact route. */
  path: string;
  url?: string;
  camera?: string | null;
  label?: string;
}

export interface ErrorInfo {
  type: string;
  message: string;
  retriable?: boolean;
  device?: string | null;
}

/** `Warning_` — `code` is machine-readable so the readiness panel matches on it, not text. */
export interface EngineWarning {
  code: string;
  message: string;
  device?: string | null;
}

export interface LogRef {
  run_id: string;
  aid: number;
}

export interface ActionResult {
  aid: number;
  index: number;
  kind: string;
  device?: string | null;
  status: "complete" | "failed" | "skipped" | "aborted";
  attempt?: number;
  started_at?: string;
  finished_at?: string;
  duration_ms?: number;
  /** D25/D29. Per action, because a computed offset in a simulated run is still simulated. */
  simulated?: boolean;
  inputs?: Record<string, unknown>;
  outputs?: Outputs | null;
  artifacts?: Artifact[];
  warnings?: EngineWarning[];
  error?: ErrorInfo | null;
  log_ref?: LogRef | null;
}

// --- the plan and the snapshot (events.py) ----------------------------------

export interface ActionRow {
  aid: number;
  index: number;
  kind: string;
  device?: string | null;
  label?: string;
  speed?: string;
  state?: ActionState;
  /** Set on loop-materialized rows; with `iteration` it gives the `18·4.7` label. */
  parent_aid?: number | null;
  iteration?: number | null;
  origin?: Origin;
  /** The kind-specific fields of the action — `params_of()` in `plan.py`. */
  params?: Record<string, unknown>;
  result?: ActionResult | null;
}

export interface RunSnapshot {
  seq: number;
  run_id: string;
  name: string;
  state: RunState;
  revision: number;
  cursor: number;
  started_at: string;
  actions: ActionRow[];
  readiness: ReadinessState;
  warnings: EngineWarning[];
  simulated: Record<string, boolean>;
}

// --- events (events.py, 12 types) -------------------------------------------

interface EventBase {
  seq: number;
  ts?: string;
  run_id?: string;
}

export interface RunStartedEvent extends EventBase {
  type: "run_started";
  name?: string;
  action_count?: number;
  simulated?: Record<string, boolean>;
}
export interface PlanReplacedEvent extends EventBase {
  type: "plan_replaced";
  revision?: number;
  cursor?: number;
  /** Rows *without* results — `plan_replaced` deliberately omits them. */
  actions?: ActionRow[];
}
export interface ActionStartedEvent extends EventBase {
  type: "action_started";
  aid: number;
  index: number;
  kind: string;
  device?: string | null;
  attempt?: number;
  inputs?: Record<string, unknown>;
}
export interface ActionLogEvent extends EventBase {
  type: "action_log";
  aid: number;
  level?: string;
  msg?: string;
}
export interface ActionFinishedEvent extends EventBase {
  type: "action_finished";
  aid: number;
  index: number;
  result: ActionResult;
}
export interface RunPausedEvent extends EventBase {
  type: "run_paused";
  reason?: "operator" | "action_failed" | "checkpoint" | "inject";
  aid?: number | null;
}
export interface RunResumedEvent extends EventBase { type: "run_resumed" }
export interface RunStateEvent extends EventBase { type: "run_state"; state: RunState }
export interface LoopIterationEvent extends EventBase {
  type: "loop_iteration";
  aid: number;
  iteration: number;
  magnitude_mm?: number | null;
  sigma_mm?: Record<string, number>;
  threshold_mm?: number | null;
  improving?: boolean;
}
export interface RunFinishedEvent extends EventBase {
  type: "run_finished";
  status?: "complete" | "failed" | "aborted";
  completed?: number;
  failed?: number;
  duration_ms?: number;
}
export interface ReadinessEvent extends EventBase {
  type: "readiness";
  state?: ReadinessState;
  warnings?: EngineWarning[];
  devices?: Record<string, unknown>;
}
export interface InjectRejectedEvent extends EventBase {
  type: "inject_rejected";
  reason?: string;
  after_aid?: number | null;
}

export type EngineEvent =
  | RunStartedEvent | PlanReplacedEvent | ActionStartedEvent | ActionLogEvent
  | ActionFinishedEvent | RunPausedEvent | RunResumedEvent | RunStateEvent
  | LoopIterationEvent | RunFinishedEvent | ReadinessEvent | InjectRejectedEvent;

export const EVENT_TYPES: readonly EngineEvent["type"][] = [
  "run_started", "plan_replaced", "action_started", "action_log", "action_finished",
  "run_paused", "run_resumed", "run_state", "loop_iteration", "run_finished",
  "readiness", "inject_rejected",
];

// --- pre-flight (plan.py `PreflightReport`) ---------------------------------
//
// `PreflightReport` is a dataclass, not a pydantic model, so the wire shape is whatever the
// API slice chooses. `preflightOf()` below accepts every plausible envelope rather than
// guessing one — a refused start that renders nothing is the worst outcome here.

export interface PreflightProblem {
  code: string;
  message: string;
  aid?: number | null;
  device?: string | null;
  waypoint?: string | null;
  blocking: boolean;
}

export interface PreflightReport {
  problems: PreflightProblem[];
  ok: boolean;
  readiness: ReadinessState;
  reason: string;
}

const EMPTY_PREFLIGHT: PreflightReport = {
  problems: [], ok: true, readiness: "initializing", reason: "",
};

/** Pull a `PreflightReport` out of a response body, an HTTPException `detail`, or neither. */
export function preflightOf(body: unknown): PreflightReport | null {
  if (!body || typeof body !== "object") return null;
  const bag = body as Record<string, unknown>;
  const inner = bag.detail && typeof bag.detail === "object" ? bag.detail as Record<string, unknown> : bag;
  const raw = (inner.problems ?? inner.preflight ?? inner.report) as unknown;
  // The refusal envelope nests the whole report (`detail.preflight`), which carries its own
  // `readiness` / `ok` — read them from there rather than re-deriving what the engine already
  // decided; `degraded` and `failed` are not interchangeable.
  const nested: Record<string, unknown> =
    raw && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : inner;
  const problems: PreflightProblem[] = Array.isArray(raw)
    ? raw.filter((p): p is PreflightProblem => !!p && typeof p === "object")
    : Array.isArray(nested.problems)
      ? (nested.problems as PreflightProblem[])
      : [];
  if (!problems.length && !nested.readiness && !inner.reason && !nested.reason) return null;
  const blocking = problems.filter((p) => p.blocking !== false);
  return {
    ...EMPTY_PREFLIGHT,
    problems,
    ok: typeof nested.ok === "boolean" ? nested.ok : blocking.length === 0,
    readiness: (nested.readiness as ReadinessState)
      ?? (blocking.length ? "failed" : problems.length ? "degraded" : "ready"),
    // The 409's own `reason` is the sentence written for the operator; the report's is the
    // summary. Prefer the former, fall back to the latter, then to the problems themselves.
    reason: typeof inner.reason === "string" && inner.reason ? inner.reason
      : typeof nested.reason === "string" && nested.reason ? nested.reason
        : blocking.map((p) => p.message).join("; "),
  };
}

// --- HTTP -------------------------------------------------------------------

/**
 * A failed engine call, with the reason the backend gave, **verbatim**.
 *
 * Three cases, and they must not be conflated:
 *
 *  * **`absent`** — the engine is not reachable at all: the fetch itself failed (status 0), or
 *    the route is not implemented (501). The tab degrades visibly.
 *  * **`notFound`** — a 404 *with a reason*. On the mounted API this is "no plan is loaded —
 *    POST /api/engine/plan first", which is an answer, not an outage: only `GET /snapshot` is
 *    guaranteed to answer without a plan, so the other routes 404 until one is loaded. Rendering
 *    that as "engine API unavailable" would be a false alarm on a perfectly healthy backend.
 *  * a 409 with a reason — the engine refused this request. Render the reason verbatim.
 *
 * The one place a 404 *does* mean absent is the snapshot route, which answers an empty snapshot
 * rather than 404 whenever the module is mounted — so the store treats it that way there, and
 * only there.
 */
export class EngineError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly body: unknown;
  readonly absent: boolean;
  readonly notFound: boolean;
  constructor(status: number, detail: string, body: unknown = null) {
    super(detail || `engine request failed (${status})`);
    this.name = "EngineError";
    this.status = status;
    this.detail = detail;
    this.body = body;
    this.absent = status === 0 || status === 501;
    this.notFound = status === 404;
  }
  get preflight(): PreflightReport | null {
    return preflightOf(this.body);
  }
}

/** The reason, as text, out of whatever the backend put in the body. Never invented. */
function detailOf(body: unknown, status: number): string {
  if (typeof body === "string" && body.trim()) return body.trim();
  if (body && typeof body === "object") {
    const bag = body as Record<string, unknown>;
    for (const key of ["detail", "reason", "message"]) {
      const v = bag[key];
      if (typeof v === "string" && v.trim()) return v.trim();
      if (v && typeof v === "object") {
        const nested = (v as Record<string, unknown>).reason ?? (v as Record<string, unknown>).message;
        if (typeof nested === "string" && nested.trim()) return nested.trim();
      }
    }
    // A structured refusal with no text field: show the JSON rather than swallow it.
    try {
      return JSON.stringify(body);
    } catch {
      /* fall through */
    }
  }
  return `HTTP ${status}`;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      ...init,
      headers: init?.body ? { "content-type": "application/json" } : undefined,
    });
  } catch (e) {
    // Network-level: the dev proxy is down, or the backend is not running at all.
    throw new EngineError(0, e instanceof Error ? e.message : String(e));
  }
  const text = await res.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      // A non-finite float upstream (review B5) lands here rather than crashing a caller.
      body = text;
    }
  }
  if (!res.ok) throw new EngineError(res.status, detailOf(body, res.status), body);
  return body as T;
}

const post = <T>(path: string, payload?: unknown) =>
  call<T>(path, { method: "POST", body: payload === undefined ? undefined : JSON.stringify(payload) });

/** The reconnect path — full current state, including `seq` to stream on from (D24). */
export const getSnapshot = () => call<RunSnapshot>("/api/engine/snapshot");

export type PlanName = "handover" | "startup";
/** Load a named plan. The response carries the new `run_id`, which the store keys on (B6). */
export const loadPlan = (name: PlanName) =>
  post<RunSnapshot>("/api/engine/plan", { name });

/** 409 + a `PreflightReport` when refused; `allow_degraded` is the operator's confirmation. */
export const startRun = (allowDegraded = false) =>
  post<{ state?: RunState }>("/api/engine/start", { allow_degraded: allowDegraded });

export const pauseRun = () => post<{ state?: RunState }>("/api/engine/pause");
export const resumeRun = () => post<{ state?: RunState }>("/api/engine/resume");
export const abortRun = () => post<{ state?: RunState }>("/api/engine/abort");

/** Pre-flight without starting, for the readiness panel (R-UI-13). */
export const getPreflight = async (): Promise<PreflightReport> =>
  preflightOf(await call<unknown>("/api/engine/preflight")) ?? EMPTY_PREFLIGHT;

/**
 * Insert an action after `after_aid`. Refusals come back as 409 with the reason, which the
 * UI renders verbatim — several cases are refused deliberately (D22/R-ENG-13).
 *
 * `null` is "at the very front of the plan", which `Plan.refusal_for_insert` accepts as
 * `after_aid in (None, 0)` and then refuses on the cursor rule once a run has moved past it.
 */
export const injectAction = (afterAid: number | null, action: Record<string, unknown>) =>
  post<RunSnapshot | { ok?: boolean }>("/api/engine/inject", { after_aid: afterAid, action });

/**
 * An artifact's URL. Served by **basename** under the run, never by the absolute `path` the
 * record carries — a client-supplied path is a traversal (review non-blocking 4).
 */
export function artifactUrl(runId: string, artifact: Artifact): string {
  if (artifact.url) return artifact.url;
  const name = (artifact.path || "").split(/[\\/]/).filter(Boolean).pop() ?? "";
  if (!runId || !name) return "";
  return `/api/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(name)}`;
}

// --- the event socket -------------------------------------------------------

export interface EngineSocketHandlers {
  onEvent: (event: EngineEvent) => void;
  onOpen?: () => void;
  onClose?: () => void;
  /** A frame that did not parse, or a `type` this build does not know. Never silent. */
  onUnknown?: (raw: string, reason: string) => void;
}

/**
 * Open `/ws/engine`, optionally asking for a replay from `since`.
 *
 * `since` is best-effort: the buffer holds a bounded 2000 events, so the server may answer
 * with a gap instead — which the store detects by `seq` and repairs with a fresh snapshot.
 */
export function openEngineSocket(handlers: EngineSocketHandlers, since = 0): WebSocket {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const query = since > 0 ? `?since=${since}` : "";
  const ws = new WebSocket(`${proto}://${location.host}/ws/engine${query}`);
  ws.onopen = () => handlers.onOpen?.();
  ws.onclose = () => handlers.onClose?.();
  ws.onmessage = (e) => {
    const raw = String(e.data);
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch (err) {
      // Literal NaN/Infinity on the wire lands here (review B5). Report it: one dropped
      // `action_finished` leaves a row stuck at "running" forever, and silence hides why.
      handlers.onUnknown?.(raw, err instanceof Error ? err.message : "unparseable frame");
      return;
    }
    if (!parsed || typeof parsed !== "object") {
      handlers.onUnknown?.(raw, "frame is not an object");
      return;
    }
    const type = (parsed as Record<string, unknown>).type;
    if (typeof type !== "string" || !EVENT_TYPES.includes(type as EngineEvent["type"])) {
      handlers.onUnknown?.(raw, `unknown event type ${String(type)}`);
      return;
    }
    handlers.onEvent(parsed as EngineEvent);
  };
  return ws;
}

// --- waypoints, for the inject form (R-WP-3) --------------------------------
//
// Read off the existing teach route rather than duplicated here: a picker that can express
// "left arm, a waypoint only the right arm owns" is a defect, and the only authority on
// ownership is `core.waypoints`.

export interface WaypointRow {
  device: string;
  name: string;
  step: number;
  speed: string;
  note: string;
  taught: boolean;
  saved_at?: string | null;
  has_joints?: boolean;
}
export interface ArmWaypoints {
  device: string;
  taught: number;
  total: number;
  complete: boolean;
  waypoints: WaypointRow[];
  extra?: string[];
}
export interface WaypointReport {
  devices: ArmWaypoints[];
  problems: { kind: string; device: string; name: string; detail: string; blocking: boolean }[];
  total: number;
  taught: number;
}

export const getWaypointReport = () => call<WaypointReport>("/api/arms/waypoints");
