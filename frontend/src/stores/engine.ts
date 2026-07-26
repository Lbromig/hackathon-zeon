// The single live picture of the run: seeded by `GET /api/engine/snapshot`, kept current by
// `/ws/engine` (D24 / R-ENG-18).
//
// A module-level `reactive()` singleton, matching the house style (no Pinia, D10). One store
// per app rather than one per component, because the run keeps streaming while the operator is
// on the Cameras tab and the reality banner has to stay truthful everywhere.
//
// The four rules this file exists to enforce:
//
//  1. **`seq` is the ordering authority**, not arrival order — the API thread emits too. A
//     lower-or-equal `seq` is dropped (every state-carrying event is absolute, so a re-apply
//     is a no-op); a *gap* is repaired with a fresh snapshot, because a skip is not.
//  2. **`seq` restarts at 1 per run** (review B6). So `run_id` is checked first: a different
//     run means the counter restarted, and the store re-snapshots instead of dropping the
//     whole of run B as "already seen".
//  3. **Results are keyed by `aid`, never by index** (D4). An injection renumbers every index
//     after it; a completed action's outputs must survive that.
//  4. **`RunStateChanged` is the state authority.** `run_paused` is a pointer at a row and is
//     emitted on a *halt* too, so acting on it would claim "paused" on a dead run (review
//     non-blocking 14) — the mirror image of the lie §3.5 forbids.
import { computed, reactive } from "vue";
import {
  EngineError, artifactUrl, getPreflight, getSnapshot, injectAction, loadPlan,
  openEngineSocket, pauseRun, resumeRun, abortRun, startRun,
  type ActionResult, type ActionRow, type ActionState, type EngineEvent,
  type EngineWarning, type LoopIterationEvent, type PlanName, type PreflightReport,
  type ReadinessState, type RunSnapshot, type RunState,
} from "../api/engine";

/** Whether the engine API exists at all. `absent` is a first-class rendered state: the routes
 *  are being built in a parallel slice, and a blank tab is not an acceptable answer. */
export type ApiHealth = "unknown" | "ok" | "absent";
export type SocketHealth = "idle" | "connecting" | "live" | "closed";

export interface LiveLogLine {
  level: string;
  msg: string;
  seq: number;
}

export interface Rejection {
  reason: string;
  afterAid: number | null;
  at: number;
}

/** One rendered plan row: the snapshot row, plus the state and result we hold by `aid`. */
export interface ChainRow {
  row: ActionRow;
  aid: number;
  /** `3` or `18·4.7` — loop index · iteration · row within the iteration (non-blocking 6). */
  display: string;
  /** 0 for a top-level row, 1 for a loop-materialized one. */
  depth: number;
  state: ActionState;
  result: ActionResult | null;
  /** A `planned` row in a finished run was never reached — not "pending" (non-blocking 15). */
  notReached: boolean;
  isCursor: boolean;
}

interface EngineState {
  runId: string;
  name: string;
  runState: RunState;
  readiness: ReadinessState;
  revision: number;
  cursor: number;
  startedAt: string;
  actionCount: number;

  rows: ActionRow[];
  results: Record<number, ActionResult>;
  states: Record<number, ActionState>;
  attempts: Record<number, number>;
  simulated: Record<string, boolean>;

  /** Pre-flight / readiness warnings. Unioned with per-action warnings by `allWarnings`. */
  warnings: EngineWarning[];
  readinessDevices: Record<string, unknown>;
  preflight: PreflightReport | null;

  loopIterations: Record<number, LoopIterationEvent[]>;
  liveLogs: Record<number, LiveLogLine[]>;
  /** Where a pause or a halt landed. A *pointer*, never a state. */
  pausePointer: { aid: number | null; reason: string } | null;
  finished: { status: string; completed: number; failed: number; durationMs: number } | null;
  rejections: Rejection[];

  api: ApiHealth;
  apiError: string;
  socket: SocketHealth;
  lastSeq: number;
  /** Transport diagnostics, shown in the tab: silence about a dropped frame is how a row
   *  gets stuck at "running" with nobody knowing why. */
  gaps: number;
  resyncs: number;
  dropped: number;
  badFrames: string[];
  /** Set when a gap could not be repaired immediately (resync rate limit) — the view is
   *  possibly incomplete and says so. */
  orderingDegraded: boolean;

  busy: string;
  commandError: string;
  nowMs: number;
}

const MAX_LIVE_LOGS = 200;
const MAX_BAD_FRAMES = 5;
const RESYNC_MIN_INTERVAL_MS = 1000;

const s = reactive<EngineState>({
  runId: "", name: "", runState: "idle", readiness: "initializing",
  revision: 0, cursor: 0, startedAt: "", actionCount: 0,
  rows: [], results: {}, states: {}, attempts: {}, simulated: {},
  warnings: [], readinessDevices: {}, preflight: null,
  loopIterations: {}, liveLogs: {}, pausePointer: null, finished: null, rejections: [],
  api: "unknown", apiError: "", socket: "idle", lastSeq: 0,
  gaps: 0, resyncs: 0, dropped: 0, badFrames: [], orderingDegraded: false,
  busy: "", commandError: "", nowMs: Date.now(),
});

// --- transport ---------------------------------------------------------------

let ws: WebSocket | null = null;
let reconnectTimer: number | null = null;
let clockTimer: number | null = null;
let backoffMs = 500;
let resyncing = false;
let lastResyncAt = 0;
let pending: EngineEvent[] = [];
let started = false;

function noteApiError(e: unknown, what: string): void {
  if (e instanceof EngineError) {
    s.apiError = `${what}: ${e.detail}`;
    // 404 on the snapshot route means `api/engine.py` is not mounted. That is a state the
    // whole tab renders, not a console message.
    if (e.absent) s.api = "absent";
    return;
  }
  s.apiError = `${what}: ${e instanceof Error ? e.message : String(e)}`;
}

function clearRunScopedState(): void {
  s.results = {};
  s.states = {};
  s.attempts = {};
  s.loopIterations = {};
  s.liveLogs = {};
  s.pausePointer = null;
  s.finished = null;
  s.rejections = [];
  s.lastSeq = 0;
}

function applySnapshot(snap: RunSnapshot): void {
  if (snap.run_id !== s.runId) clearRunScopedState();
  s.runId = snap.run_id ?? "";
  s.name = snap.name ?? "";
  s.runState = snap.state ?? "idle";
  s.readiness = snap.readiness ?? "initializing";
  s.revision = snap.revision ?? 0;
  s.cursor = snap.cursor ?? 0;
  s.startedAt = snap.started_at ?? "";
  s.warnings = snap.warnings ?? [];
  s.simulated = snap.simulated ?? {};
  s.rows = (snap.actions ?? []).map((row) => ({ ...row, result: null }));
  s.actionCount = s.rows.length;
  for (const row of snap.actions ?? []) {
    s.states[row.aid] = row.state ?? "planned";
    // The snapshot is the only carrier of results for a client that missed the events.
    if (row.result) s.results[row.aid] = row.result;
  }
  // Read `seq` from the snapshot, not from the rows: `Runner.snapshot` samples it *before*
  // the rows on purpose, so re-applying a straddling event is a no-op rather than a skip.
  s.lastSeq = snap.seq ?? 0;
  s.api = "ok";
  s.apiError = "";
}

async function resync(reason: string): Promise<void> {
  if (resyncing) return;
  const now = Date.now();
  if (now - lastResyncAt < RESYNC_MIN_INTERVAL_MS) {
    // Repairing faster than this would be a resync storm against a backend that is already
    // struggling. Say the view may be incomplete instead of pretending it is not.
    s.orderingDegraded = true;
    return;
  }
  resyncing = true;
  lastResyncAt = now;
  s.resyncs++;
  void reason;
  try {
    applySnapshot(await getSnapshot());
    s.orderingDegraded = false;
  } catch (e) {
    noteApiError(e, "snapshot");
  } finally {
    resyncing = false;
    const queued = pending.slice().sort((a, b) => a.seq - b.seq);
    pending = [];
    for (const event of queued) ingest(event);
  }
}

function ingest(event: EngineEvent): void {
  if (resyncing) {
    pending.push(event);
    return;
  }
  // Rule 2 first: `seq` restarts at 1 per run, so a run change must be detected before any
  // comparison against `lastSeq` (review B6). An empty `run_id` carries no information.
  if (event.run_id && s.runId && event.run_id !== s.runId) {
    pending.push(event);
    void resync("run_id changed");
    return;
  }
  if (!s.runId && event.run_id) s.runId = event.run_id;

  if (s.lastSeq > 0 && event.seq <= s.lastSeq) {
    s.dropped++;                       // duplicate or out of order — re-applying is a no-op
    return;
  }
  if (s.lastSeq > 0 && event.seq > s.lastSeq + 1) {
    s.gaps++;                          // a skip is *not* a no-op: rebuild from the snapshot
    pending.push(event);
    void resync("gap");
    if (!resyncing && s.orderingDegraded) {
      // Rate-limited: apply anyway rather than freeze. Every state-carrying event is
      // absolute, so this is a partial picture that the next snapshot repairs.
      pending = pending.filter((e) => e !== event);
      apply(event);
      s.lastSeq = event.seq;
    }
    return;
  }
  apply(event);
  s.lastSeq = event.seq;
}

function apply(event: EngineEvent): void {
  switch (event.type) {
    case "run_started":
      s.name = event.name ?? s.name;
      s.actionCount = event.action_count ?? s.actionCount;
      // The per-device reality map. Persistent and unmissable (D25/D29, R-SIM-8).
      s.simulated = event.simulated ?? {};
      s.finished = null;
      s.pausePointer = null;
      break;

    case "plan_replaced": {
      s.revision = event.revision ?? s.revision;
      s.cursor = event.cursor ?? s.cursor;
      const rows = event.actions ?? [];
      // Rows arrive without results (`row_payload` excludes them) — the `results` map by
      // `aid` is what keeps a completed action's outputs across the renumbering (D4).
      s.rows = rows.map((row) => ({ ...row, result: null }));
      s.actionCount = rows.length;
      for (const row of rows) s.states[row.aid] = row.state ?? s.states[row.aid] ?? "planned";
      break;
    }

    case "action_started":
      s.states[event.aid] = "running";
      s.attempts[event.aid] = event.attempt ?? 1;
      break;

    case "action_log": {
      const lines = s.liveLogs[event.aid] ?? (s.liveLogs[event.aid] = []);
      lines.push({ level: event.level ?? "INFO", msg: event.msg ?? "", seq: event.seq });
      if (lines.length > MAX_LIVE_LOGS) lines.splice(0, lines.length - MAX_LIVE_LOGS);
      break;
    }

    case "action_finished":
      s.results[event.aid] = event.result;
      s.states[event.aid] = event.result?.status ?? "complete";
      break;

    case "run_paused":
      // A pointer at a row, deliberately *not* a state change: this is also emitted on a
      // halt, where the state goes to `failed` (review non-blocking 14).
      s.pausePointer = { aid: event.aid ?? null, reason: event.reason ?? "operator" };
      break;

    case "run_resumed":
      s.pausePointer = null;
      break;

    case "run_state":
      s.runState = event.state;
      if (event.state === "running") s.pausePointer = null;
      break;

    case "loop_iteration": {
      const iters = s.loopIterations[event.aid] ?? (s.loopIterations[event.aid] = []);
      const at = iters.findIndex((i) => i.iteration === event.iteration);
      if (at >= 0) iters[at] = event;
      else iters.push(event);
      break;
    }

    case "run_finished":
      s.finished = {
        status: event.status ?? "complete",
        completed: event.completed ?? 0,
        failed: event.failed ?? 0,
        durationMs: event.duration_ms ?? 0,
      };
      break;

    case "readiness":
      s.readiness = event.state ?? s.readiness;
      s.warnings = event.warnings ?? [];
      s.readinessDevices = event.devices ?? {};
      break;

    case "inject_rejected":
      // Every connected client sees the refusal, not only whoever sent it (D22/R-ENG-13).
      s.rejections.unshift({
        reason: event.reason ?? "refused, with no reason given",
        afterAid: event.after_aid ?? null,
        at: Date.now(),
      });
      s.rejections.splice(6);
      break;
  }
}

function openSocket(): void {
  if (ws) return;
  s.socket = "connecting";
  ws = openEngineSocket({
    onEvent: ingest,
    onOpen: () => {
      s.socket = "live";
      backoffMs = 500;
      // The socket may have missed events while it was down, and `since` is best-effort:
      // re-snapshot on every (re)connect and let `seq` sort out the overlap.
      void resync("socket opened");
    },
    onClose: () => {
      s.socket = "closed";
      ws = null;
      scheduleReconnect();
    },
    onUnknown: (raw, reason) => {
      // A frame we could not read is a frame whose state we do not have. Show it.
      s.badFrames.unshift(`${reason}: ${raw.slice(0, 200)}`);
      s.badFrames.splice(MAX_BAD_FRAMES);
      void resync("unreadable frame");
    },
  }, s.lastSeq);
}

function scheduleReconnect(): void {
  if (reconnectTimer !== null) return;
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null;
    openSocket();
  }, backoffMs);
  backoffMs = Math.min(backoffMs * 2, 5000);
}

/** Start streaming. Idempotent — App.vue calls it once for the whole session. */
export function connectEngine(): void {
  if (started) return;
  started = true;
  void refresh();
  openSocket();
  clockTimer = window.setInterval(() => (s.nowMs = Date.now()), 1000);
}

export function disconnectEngine(): void {
  started = false;
  if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
  if (clockTimer !== null) window.clearInterval(clockTimer);
  reconnectTimer = clockTimer = null;
  ws?.close();
  ws = null;
  s.socket = "idle";
}

/** Re-seed from the snapshot, and refresh pre-flight for the readiness panel. */
export async function refresh(): Promise<void> {
  try {
    applySnapshot(await getSnapshot());
  } catch (e) {
    noteApiError(e, "snapshot");
    return;
  }
  try {
    s.preflight = await getPreflight();
  } catch (e) {
    // Pre-flight is advisory for the panel; a missing route must not blank the tab.
    if (e instanceof EngineError && !e.absent) s.apiError = `preflight: ${e.detail}`;
  }
}

// --- commands ----------------------------------------------------------------

async function command(name: string, run: () => Promise<unknown>): Promise<boolean> {
  s.busy = name;
  s.commandError = "";
  try {
    await run();
    return true;
  } catch (e) {
    if (e instanceof EngineError) {
      s.commandError = e.detail;                      // verbatim: it is the operator's answer
      if (e.absent) s.api = "absent";
      const report = e.preflight;
      if (report) s.preflight = report;
    } else {
      s.commandError = e instanceof Error ? e.message : String(e);
    }
    return false;
  } finally {
    s.busy = "";
    // The refused-start path changes nothing server-side, but pause/inject do — re-seed so
    // the tab never diverges from the runner because a command half-succeeded.
    void resync("after command");
  }
}

export const engineCommands = {
  load: (name: PlanName) => command("load", () => loadPlan(name)),
  start: (allowDegraded = false) => command("start", () => startRun(allowDegraded)),
  pause: () => command("pause", () => pauseRun()),
  resume: () => command("resume", () => resumeRun()),
  abort: () => command("abort", () => abortRun()),
  inject: (afterAid: number, action: Record<string, unknown>) =>
    command("inject", () => injectAction(afterAid, action)),
};

export function clearCommandError(): void {
  s.commandError = "";
}

// --- derived -----------------------------------------------------------------

const isFinished = (state: RunState) =>
  state === "complete" || state === "failed" || state === "aborted";

/**
 * The plan chain, with the `18·4.7` labels and the nesting.
 *
 * The label carries the iteration because 8 rows × 12 iterations share 8 kinds: a flat
 * `18.4` repeats 96 times and identifies nothing (review non-blocking 6).
 */
const chain = computed<ChainRow[]>(() => {
  const rows = s.rows.slice().sort((a, b) => a.index - b.index);
  const indexOfAid = new Map<number, number>(rows.map((r) => [r.aid, r.index]));
  const seenInIteration = new Map<string, number>();
  const finishedRun = isFinished(s.runState);

  return rows.map((row) => {
    const state = s.states[row.aid] ?? row.state ?? "planned";
    let display = String(row.index);
    let depth = 0;
    if (row.parent_aid != null && row.iteration != null) {
      const key = `${row.parent_aid}:${row.iteration}`;
      const n = (seenInIteration.get(key) ?? 0) + 1;
      seenInIteration.set(key, n);
      display = `${indexOfAid.get(row.parent_aid) ?? "?"}·${row.iteration}.${n}`;
      depth = 1;
    }
    return {
      row,
      aid: row.aid,
      display,
      depth,
      state,
      result: s.results[row.aid] ?? null,
      notReached: finishedRun && state === "planned",
      isCursor: row.index === s.cursor && !finishedRun,
    };
  });
});

/** How much of the run is real. A mixed run has to be as visible as a fully simulated one. */
export type Reality = "unknown" | "simulated" | "real" | "mixed";

const reality = computed<{ kind: Reality; simulated: string[]; real: string[] }>(() => {
  const simulated: string[] = [];
  const real: string[] = [];
  for (const [device, sim] of Object.entries(s.simulated)) (sim ? simulated : real).push(device);
  // `ActionResult.simulated` is the second, independent witness (D25) — a pure-computation
  // action has no device but is still simulated when the pixels it read were.
  let resultSim = false;
  let resultReal = false;
  for (const result of Object.values(s.results)) {
    if (result.simulated === true) resultSim = true;
    if (result.simulated === false) resultReal = true;
  }
  const anySim = simulated.length > 0 || resultSim;
  const anyReal = real.length > 0 || resultReal;
  if (anySim && anyReal) return { kind: "mixed", simulated, real };
  if (anySim) return { kind: "simulated", simulated, real };
  if (anyReal) return { kind: "real", simulated, real };
  return { kind: "unknown", simulated, real };
});

/**
 * Every warning the operator should see, keyed by `Warning_.code`.
 *
 * `RunSnapshot.warnings` only ever carries pre-flight problems, so R-INIT-4's "no taught
 * HOME" — a per-action `ctx.warn` — reaches the panel only through `result.warnings`
 * (review non-blocking 3). Union both, dedupe on code + device.
 */
const allWarnings = computed<(EngineWarning & { fromAid?: number })[]>(() => {
  const out: (EngineWarning & { fromAid?: number })[] = [];
  const seen = new Set<string>();
  const push = (w: EngineWarning, fromAid?: number) => {
    const key = `${w.code}|${w.device ?? ""}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({ ...w, fromAid });
  };
  for (const w of s.warnings) push(w);
  for (const result of Object.values(s.results)) {
    for (const w of result.warnings ?? []) push(w, result.aid);
  }
  return out;
});

const counts = computed(() => {
  let complete = 0;
  let failed = 0;
  let running = 0;
  for (const item of chain.value) {
    if (item.state === "complete") complete++;
    else if (item.state === "failed" || item.state === "aborted") failed++;
    else if (item.state === "running") running++;
  }
  return { complete, failed, running, total: chain.value.length };
});

/** The row a failure landed on, if any — what Start/Resume have to reason about. */
const failedRow = computed<ChainRow | null>(
  () => chain.value.find((item) => item.state === "failed") ?? null,
);

/**
 * Why Resume is dangerous after a mid-loop failure (review B4): the cursor lands *past* the
 * whole materialized region, so resuming skips the rest of the iteration and executes the
 * action after the loop — on this workflow, retracting the pipette while the tube is still
 * misaligned. The backend should 409 it; the button says so either way.
 */
const resumeAbandonsLoop = computed(
  () => !!failedRow.value && failedRow.value.row.parent_aid != null,
);

const elapsedMs = computed(() => {
  if (!s.startedAt) return 0;
  const t0 = Date.parse(s.startedAt);
  if (Number.isNaN(t0)) return 0;
  if (s.finished) return s.finished.durationMs || Math.max(0, s.nowMs - t0);
  return Math.max(0, s.nowMs - t0);
});

/** The iterations a loop has reported, newest last. Also the trend source for a solve row. */
export function loopIterationsOf(aid: number): LoopIterationEvent[] {
  return s.loopIterations[aid] ?? [];
}

/**
 * The previous `vision.solve_offset` magnitude in the same loop — the trend arrow's only
 * honest source. There is no "trend" field on `OffsetOutputs`; it is computed here from the
 * two most recent solves under the same `parent_aid`.
 */
export function previousMagnitude(aid: number): number | null {
  const rows = chain.value;
  const at = rows.findIndex((item) => item.aid === aid);
  if (at < 0) return null;
  const parent = rows[at].row.parent_aid;
  for (let i = at - 1; i >= 0; i--) {
    const item = rows[i];
    if (item.row.kind !== "vision.solve_offset" || item.row.parent_aid !== parent) continue;
    const outputs = item.result?.outputs;
    if (outputs?.kind === "offset" && typeof outputs.magnitude_mm === "number") {
      return outputs.magnitude_mm;
    }
  }
  return null;
}

export function useEngine() {
  return {
    state: s,
    chain,
    counts,
    reality,
    allWarnings,
    failedRow,
    resumeAbandonsLoop,
    elapsedMs,
    commands: engineCommands,
    refresh,
    artifactUrl,
  };
}
