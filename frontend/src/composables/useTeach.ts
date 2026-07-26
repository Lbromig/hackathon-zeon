// Shared state for the teach tab: which arm, its live state, taught poses,
// jog settings and the command log.
//
// This is a module singleton on purpose — the panel and its children all act on
// one arm, and keeping the poller in one place means switching tabs doesn't
// spawn a second one.

import { computed, reactive, ref } from "vue";
import * as api from "../api/teach";
import type {
  ActionResult,
  ArmState,
  ArmSummary,
  ArmWaypoints,
  SpeedTier,
  TaughtPose,
  WaypointProblem,
  WaypointReport,
  WaypointRow,
} from "../api/teach";

const POLL_MS = 500; // 2 Hz — matches the backend's gripper-width cache

export interface LogEntry {
  id: number;
  label: string;
  ok: boolean;
  detail: string;
  ms: number;
  at: string;
}

export interface JogSettings {
  linear: number; // mm per jog
  angular: number; // deg per jog
  speed: number; // mm/s for cartesian, deg/s for joint moves
}

export const LINEAR_STEPS = [0.1, 1, 5, 10, 50];
export const ANGULAR_STEPS = [0.1, 1, 5, 15];
export const SPEED_PRESETS = [
  { label: "slow", value: 20 },
  { label: "medium", value: 60 },
  { label: "fast", value: 150 },
];

const STORAGE_KEY = "teach.settings";

function loadSettings(): JogSettings {
  const fallback: JogSettings = { linear: 1, angular: 1, speed: 60 };
  try {
    return { ...fallback, ...JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}") };
  } catch {
    return fallback;
  }
}

const arms = ref<ArmSummary[]>([]);
const selectedId = ref<string>(localStorage.getItem("teach.arm") ?? "");
const state = ref<ArmState | null>(null);
const poses = ref<TaughtPose[]>([]);
const log = ref<LogEntry[]>([]);
const sending = ref(false);
const loadError = ref("");
const settings = reactive<JogSettings>(loadSettings());
const waypointReport = ref<WaypointReport | null>(null);
const waypointError = ref("");

let poller: number | undefined;
let polling = false; // single-flight: skip a tick if the last one is still out
let logSeq = 0;

const arm = computed(() => arms.value.find((a) => a.id === selectedId.value) ?? null);
const connected = computed(() => state.value?.connected ?? false);
const faulted = computed(() => Boolean(state.value?.error_code));
/** Motion controls are live only when the arm can actually move. */
const canMove = computed(() => connected.value && !faulted.value && !sending.value);

/**
 * The selected arm's workflow waypoints — and *only* that arm's (R-WP-3).
 *
 * Derived by matching on `device`, never by index or by merging the two arms' lists: a
 * picker that can express `right` + a left-arm waypoint is a defect, and the cheapest way
 * to guarantee it cannot is for the UI to never hold a combined list at all.
 * `null` when the selected arm owns no workflow waypoints, which the panel says out loud
 * rather than showing an empty checklist that reads as "nothing to teach".
 */
const waypoints = computed<ArmWaypoints | null>(
  () => waypointReport.value?.devices.find((d) => d.device === selectedId.value) ?? null,
);

/** Problems that concern the selected arm. Blocking ones first. */
const waypointProblems = computed<WaypointProblem[]>(() =>
  (waypointReport.value?.problems ?? [])
    .filter((p) => p.device === selectedId.value)
    .sort((a, b) => Number(b.blocking) - Number(a.blocking)),
);

/** Untaught rows, in workflow order — the operator's remaining work. */
const untaughtWaypoints = computed<WaypointRow[]>(
  () => waypoints.value?.waypoints.filter((w) => !w.taught) ?? [],
);

/** The next one to teach, so the panel can point at it without the operator scanning. */
const nextWaypoint = computed<WaypointRow | null>(() => untaughtWaypoints.value[0] ?? null);

function persistSettings() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
}

function pushLog(label: string, ok: boolean, detail: string, startedAt: number) {
  log.value.unshift({
    id: ++logSeq,
    label,
    ok,
    detail,
    ms: Math.round(performance.now() - startedAt),
    at: new Date().toLocaleTimeString(),
  });
  if (log.value.length > 60) log.value.length = 60;
}

/**
 * Run one command, record it, and adopt the state it returns.
 * `force` is for the e-stop: it must never queue behind an in-flight move,
 * exactly like the backend's lock bypass.
 */
async function send(
  label: string,
  fn: () => Promise<ActionResult>,
  opts: { force?: boolean } = {},
): Promise<boolean> {
  if (sending.value && !opts.force) return false;
  const startedAt = performance.now();
  if (!opts.force) sending.value = true;
  try {
    const res = await fn();
    pushLog(label, res.ok, res.detail, startedAt);
    if (res.state) state.value = res.state;
    return res.ok;
  } catch (e) {
    pushLog(label, false, e instanceof Error ? e.message : String(e), startedAt);
    return false;
  } finally {
    if (!opts.force) sending.value = false;
  }
}

async function refreshArms() {
  try {
    arms.value = await api.listArms();
    loadError.value = "";
    if (!arms.value.some((a) => a.id === selectedId.value)) {
      await select(arms.value[0]?.id ?? "");
      return;
    }
    // The arm remembered in localStorage is still there, so `select` does not run — but
    // its poses and checklist still have to be loaded, or a reload leaves the panel
    // showing an empty library and "0 of 10 taught" for a fully taught arm.
    await Promise.all([refreshPoses(), refreshWaypoints()]);
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e);
  }
}

async function select(id: string) {
  selectedId.value = id;
  state.value = null;
  poses.value = [];
  if (!id) return;
  localStorage.setItem("teach.arm", id);
  await Promise.all([tick(), refreshPoses(), refreshWaypoints()]);
}

async function refreshPoses() {
  if (!selectedId.value) return;
  try {
    poses.value = await api.listPoses(selectedId.value);
  } catch {
    /* the pose library is a convenience; don't break the panel over it */
  }
}

/**
 * Re-read the checklist. Unlike the pose library this failing is *not* cosmetic: a stale
 * checklist tells the operator a waypoint is taught when it is not, so the error is
 * surfaced rather than swallowed.
 */
async function refreshWaypoints() {
  try {
    waypointReport.value = await api.getWaypointReport();
    waypointError.value = "";
  } catch (e) {
    waypointError.value = e instanceof Error ? e.message : String(e);
  }
}

async function tick() {
  if (polling || !selectedId.value || document.hidden) return;
  polling = true;
  try {
    state.value = await api.getArmState(selectedId.value);
  } catch {
    /* transient — the next tick retries */
  } finally {
    polling = false;
  }
}

function startPolling() {
  if (poller !== undefined) return;
  poller = window.setInterval(tick, POLL_MS);
  void refreshArms();
}

function stopPolling() {
  if (poller === undefined) return;
  window.clearInterval(poller);
  poller = undefined;
}

// --- commands ---------------------------------------------------------------

const step = (axis: string) => (["x", "y", "z"].includes(axis) ? settings.linear : settings.angular);
const unit = (axis: string) => (["x", "y", "z"].includes(axis) ? "mm" : "°");

function jogCartesian(axis: string, sign: 1 | -1) {
  const delta = step(axis) * sign;
  return send(`jog ${axis} ${delta > 0 ? "+" : ""}${delta}${unit(axis)}`, () =>
    api.jog(selectedId.value, "cartesian", axis, delta, settings.speed),
  );
}

function jogJoint(index: number, sign: 1 | -1) {
  const delta = settings.angular * sign;
  return send(`jog J${index + 1} ${delta > 0 ? "+" : ""}${delta}°`, () =>
    api.jog(selectedId.value, "joint", `j${index + 1}`, delta, settings.speed),
  );
}

const moveToPose = (pose: api.PoseValues) =>
  send("move to pose", () => api.moveToPose(selectedId.value, pose, settings.speed));

const moveToJoints = (joints: number[]) =>
  send("move to joints", () => api.moveToJoints(selectedId.value, joints, settings.speed));

const openGripper = () => send("gripper open", () => api.setGripper(selectedId.value, "open"));
const closeGripper = () => send("gripper close", () => api.setGripper(selectedId.value, "close"));
const setGripperWidth = (width: number) =>
  send(`gripper -> ${width}`, () => api.setGripper(selectedId.value, "set", width));

const goHome = () => send("home", () => api.home(selectedId.value));
const setEnabled = (on: boolean) => send(on ? "enable" : "disable", () => api.enableArm(selectedId.value, on));
const clearErrors = () => send("clear errors", () => api.clearErrors(selectedId.value));
const connect = () => send("connect", () => api.connectArm(selectedId.value));

/** Always allowed, never queued. */
const estop = () => send("E-STOP", () => api.stopArm(selectedId.value, true), { force: true });

async function savePose(name: string, note = "") {
  const startedAt = performance.now();
  try {
    poses.value = await api.savePose(selectedId.value, name, note);
    pushLog(`save pose "${name}"`, true, "", startedAt);
    await refreshWaypoints();
    return true;
  } catch (e) {
    pushLog(`save pose "${name}"`, false, e instanceof Error ? e.message : String(e), startedAt);
    return false;
  }
}

async function deletePose(name: string) {
  const startedAt = performance.now();
  try {
    poses.value = await api.deletePose(selectedId.value, name);
    pushLog(`delete pose "${name}"`, true, "", startedAt);
    await refreshWaypoints();
  } catch (e) {
    pushLog(`delete pose "${name}"`, false, e instanceof Error ? e.message : String(e), startedAt);
  }
}

const gotoPose = (name: string) =>
  send(`goto "${name}"`, () => api.gotoPose(selectedId.value, name, { speed: settings.speed }));

/**
 * Teach the selected arm's waypoint: snapshot where it is now under that exact name.
 *
 * The name comes from the checklist, so it is always one this arm owns; the backend
 * re-checks and refuses otherwise, and that refusal lands in the command log.
 */
const teachWaypoint = (name: string, note = "") => savePose(name, note);

/**
 * Drive to a waypoint at its *intended* tier rather than at the jog slider's speed —
 * verifying a taught point at the speed the workflow will use is the point of the button.
 */
const gotoWaypoint = (name: string, tier: SpeedTier) =>
  send(`goto waypoint ${name} (${tier})`, () =>
    api.gotoPose(selectedId.value, name, { tier }),
  );

/** Hand-guiding on/off. Turning it off is also how we get back to position control,
 *  so it must stay reachable even when `canMove` is false. */
const setFreeDrive = (on: boolean) =>
  send(on ? "hand-guide ON" : "hand-guide off", () => api.setFreeDrive(selectedId.value, on),
       { force: true });

const grabCap = (width?: number) =>
  send("grab cap", () => api.capAction(selectedId.value, "grab", { width }));
const ungrabCap = () =>
  send("ungrab cap", () => api.capAction(selectedId.value, "ungrab"));
/** Ratchet unscrew — `halfTurns` x 180°. Long-running: the arm turns, opens,
 *  unwinds and re-grips once per bite. */
const unscrewCap = (halfTurns: number, width?: number) =>
  send(`unscrew cap (${halfTurns} x 180°)`, () =>
    api.capAction(selectedId.value, "unscrew", {
      half_turns: halfTurns, width, speed: settings.speed,
    }),
  );

export function useTeach() {
  return {
    // state
    arms,
    arm,
    selectedId,
    state,
    poses,
    log,
    sending,
    loadError,
    settings,
    connected,
    faulted,
    canMove,
    // workflow waypoints (R-WP-3: only ever the selected arm's own)
    waypoints,
    waypointProblems,
    waypointError,
    untaughtWaypoints,
    nextWaypoint,
    // lifecycle
    startPolling,
    stopPolling,
    refreshArms,
    refreshPoses,
    refreshWaypoints,
    select,
    persistSettings,
    // commands
    jogCartesian,
    jogJoint,
    moveToPose,
    moveToJoints,
    openGripper,
    closeGripper,
    setGripperWidth,
    goHome,
    setEnabled,
    clearErrors,
    connect,
    estop,
    savePose,
    deletePose,
    gotoPose,
    teachWaypoint,
    gotoWaypoint,
    setFreeDrive,
    grabCap,
    ungrabCap,
    unscrewCap,
  };
}
