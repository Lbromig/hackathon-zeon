// The per-kind inline summary — §3.2's table, one row of the plan chain at a time.
//
// The rule this file follows: **every fact shown has a field behind it.** Where the design
// promised something the frozen `Outputs` models do not carry, this either derives it from
// fields that do exist (and says so in the tooltip) or shows it as unknown — never a plausible
// blank. Three cases, all annotated below:
//
//   * `arm.waypoint` **distance** — no field. Derived as the straight-line distance between
//     `pose_before` and `pose_after`, which is the *travelled* distance, not the commanded one.
//   * `vision.solve_offset` **trend arrow** — no field. Compared against the previous solve in
//     the same loop region.
//
// Review B7's three unwritten fields have since landed in `arm.py` and are read here as fields:
// `MoveOutputs.path` (verified `joint_replay` on a live run), and
// `DecapOutputs.wrist_rewound_before_decap` / `rewind_deg`. The absent-renders-as-unknown paths
// below stay, because an older backend or a handler that stops writing one must still read as
// unknown rather than as `cartesian` or as "no rewind happened".
//
// And two rules from §3.5 that are enforced here rather than by the reader:
//   * a loop is never labelled "aborted" because a step inside it failed (non-blocking 1);
//   * `view_disagreement_mm` never appears inline, where it would read as a gate.
import type { ChainRow } from "../../stores/engine";
import type { LoopIterationEvent, Outputs, RunState } from "../../api/engine";

export type Tone = "muted" | "good" | "warn" | "bad" | "info";

export interface Bit {
  text: string;
  tone?: Tone;
  /** Shown on hover, and as the accessible description: where the number came from. */
  title?: string;
}

export interface SummaryContext {
  runState: RunState;
  previousMagnitude: (aid: number) => number | null;
  loopIterations: (aid: number) => LoopIterationEvent[];
}

const num = (v: unknown): number | null =>
  typeof v === "number" && Number.isFinite(v) ? v : null;

const mm = (v: number, digits = 1) => `${v.toFixed(digits)} mm`;
const deg = (v: number, digits = 0) => `${v.toFixed(digits)}°`;

const str = (v: unknown): string => (typeof v === "string" ? v : "");

/**
 * `{x,y,z}` as `x 1.2 · y -0.4 · z 3.0`, skipping axes that are absent or null.
 *
 * Both spellings are accepted, because the backend uses both: `LHMoveOutputs.requested_mm` is
 * keyed `x/y/z` (from `AXES`) while `MoveOutputs.offsets_mm` is keyed `dx/dy/dz` (from the
 * action's own field names). Reading only one spelling made every arm offset render as blank.
 */
function axes(map: unknown, digits = 1): string {
  if (!map || typeof map !== "object") return "";
  const bag = map as Record<string, unknown>;
  const out: string[] = [];
  for (const axis of ["x", "y", "z"]) {
    const v = num(bag[axis]) ?? num(bag[`d${axis}`]);
    if (v !== null) out.push(`${axis} ${v.toFixed(digits)}`);
  }
  return out.join(" · ");
}

/** Straight-line xyz distance between two recorded poses. */
function poseDistance(before: unknown, after: unknown): number | null {
  if (!Array.isArray(before) || !Array.isArray(after)) return null;
  if (before.length < 3 || after.length < 3) return null;
  let sum = 0;
  for (let i = 0; i < 3; i++) {
    const a = num(before[i]);
    const b = num(after[i]);
    if (a === null || b === null) return null;
    sum += (b - a) ** 2;
  }
  return Math.sqrt(sum);
}

const PATH_LABEL: Record<string, string> = {
  joint_replay: "joint",
  "joint_replay+cartesian_offset": "joint+offset",
  cartesian: "cartesian",
  relative: "relative",
};

/** Only the outputs whose discriminant matches, so a mis-typed result renders as absent. */
function outputsOf<K extends Outputs["kind"]>(
  item: ChainRow, kind: K,
): Extract<Outputs, { kind: K }> | null {
  const outputs = item.result?.outputs;
  if (outputs && outputs.kind === kind) return outputs as Extract<Outputs, { kind: K }>;
  return null;
}

const hasWarning = (item: ChainRow, code: string) =>
  (item.result?.warnings ?? []).some((w) => w.code === code);

export function summarize(item: ChainRow, ctx: SummaryContext): Bit[] {
  const bits: Bit[] = [];
  const params = item.row.params ?? {};
  const push = (text: string, tone?: Tone, title?: string) => {
    if (text) bits.push({ text, tone, title });
  };

  switch (item.row.kind) {
    case "arm.waypoint": {
      const out = outputsOf(item, "move");
      const name = out?.waypoint || str(params.waypoint);
      push(`→ ${name || "?"}`, "info");
      const travelled = poseDistance(out?.pose_before, out?.pose_after);
      if (travelled !== null) {
        push(mm(travelled, 0), "muted",
          "travelled: straight-line xyz distance between pose_before and pose_after — " +
          "MoveOutputs has no distance field");
      }
      const offsets = axes(out?.offsets_mm ?? { x: params.dx, y: params.dy, z: params.dz });
      if (offsets && !/^x 0\.0 · y 0\.0 · z 0\.0$/.test(offsets)) push(`offset ${offsets}`, "muted");
      const path = str(out?.path);
      if (item.result) {
        push(path ? PATH_LABEL[path] ?? path : "path ?", path ? "muted" : "warn",
          path ? "MoveOutputs.path — which motion path the move actually took"
            : "MoveOutputs.path is empty. The handler does write it, so an empty value means " +
              "this row came from an older backend. Not shown as 'cartesian': that would be a " +
              "guess about the first question worth asking after an unexpected trajectory");
      }
      break;
    }

    case "arm.move_relative": {
      const out = outputsOf(item, "move");
      const delta = axes(out?.offsets_mm ?? { x: params.dx, y: params.dy, z: params.dz });
      push(delta ? `Δ ${delta}` : "Δ from slot", "info");
      if (params.from_slot) push(`from ${str(params.from_slot)}`, "muted");
      const travelled = poseDistance(out?.pose_before, out?.pose_after);
      if (travelled !== null) push(mm(travelled, 1), "muted", "travelled, from the recorded poses");
      break;
    }

    case "arm.gripper": {
      const out = outputsOf(item, "gripper");
      const state = out?.state ?? str(params.state) ?? "";
      const width = num(out?.width_after) ?? num(params.width);
      push(state === "close" ? (width !== null ? `close ${width.toFixed(0)}` : "close full")
        : state || "gripper", "info");
      const before = num(out?.width_before);
      if (before !== null && width !== null) {
        push(`${before.toFixed(0)} → ${width.toFixed(0)}`, "muted", "gripper counts");
      }
      break;
    }

    case "arm.decap": {
      const out = outputsOf(item, "decap");
      const step = num(out?.step_deg) ?? num(params.step_deg) ?? 90;
      const bites = num(out?.bites)
        ?? (num(params.turns) !== null ? Math.round((num(params.turns) as number) * 360 / step) : null);
      push(`${bites ?? "?"} × ${deg(step)}`, "info");
      const net = num(out?.net_wrist_travel_deg);
      if (net !== null) {
        push(`net wrist ${net.toFixed(1)}°`, Math.abs(net) < 0.5 ? "good" : "warn",
          "DecapOutputs.net_wrist_travel_deg — must be ~0; a ratchet that drifted shows here");
      }
      const rewound = out?.wrist_rewound_before_decap === true;
      const rewindDeg = num(out?.rewind_deg);
      if (rewound) {
        push(rewindDeg ? `rewound ${deg(Math.abs(rewindDeg))}` : "rewound", "warn",
          "the wrist had to be unwound before the ratchet could start");
      } else if (hasWarning(item, "wrist_rewound_before_decap")) {
        // Both halves exist now: the field and the `ctx.warn`. This branch catches the case
        // where only the warning arrived — a decap that needed a rewind must never render
        // identically to one that did not.
        push("rewound (from warning)", "warn",
          "the wrist_rewound_before_decap warning fired but the field is empty — an older " +
          "backend, or a rewind the outputs did not record");
      }
      if (out && out.preflight_ok === false) {
        push("ratchet not pre-flighted", "bad", "DecapOutputs.preflight_ok is false");
      }
      break;
    }

    case "arm.traverse": {
      const out = outputsOf(item, "traverse");
      const names = (out?.reached?.length ? out.reached : params.waypoints) as unknown;
      const list = Array.isArray(names) ? names.map(String) : [];
      push(list.length ? `→ ${list.join(" → ")}` : "traverse", "info");
      if (out) {
        const blend = num(out.blend_deg);
        push(blend !== null ? `blend ${deg(blend, 1)}` : "point-to-point", "muted",
          "TraverseOutputs.blend_deg — the radius actually used after the shortest-segment clamp");
      }
      break;
    }

    case "lh.move_relative": {
      const out = outputsOf(item, "lh_move");
      const requested = out?.requested_mm ?? { x: params.dx, y: params.dy, z: params.dz };
      const req = axes(requested);
      const app = axes(out?.applied_mm);
      if (params.from_slot && !out) push(`from ${str(params.from_slot)}`, "muted");
      if (req) push(`req ${req}`, "info", "LHMoveOutputs.requested_mm");
      if (app) push(`applied ${app}`, "info", "LHMoveOutputs.applied_mm");
      if (req && app && req !== app) {
        // Not labelled "clamped": `clamp_mm` is a *refusal*, not a clamp (R-LH-3), so a
        // difference here means an axis was unobservable or the driver limited the move.
        push("applied ≠ requested", "warn",
          "the driver did not apply what was asked — clamp_mm refuses rather than clamps, " +
          "so this is an unobservable axis or a driver limit, not a clamp");
      }
      if (out?.provenance) {
        push(out.provenance === "measured" ? "measured" : "dead reckoned",
          out.provenance === "measured" ? "good" : "warn",
          "R-LH-4: a dead-reckoned position must not read as a measurement");
      }
      break;
    }

    case "camera.snapshot": {
      const out = outputsOf(item, "snapshot");
      push(item.row.device ?? "camera", "info");
      if (out) {
        if (out.achieved_mode) {
          push(out.achieved_mode, "muted",
            "SnapshotOutputs.achieved_mode — read back off the device, not the request");
        }
        const stream = out.stream ?? "unknown";
        push(stream === "unknown" ? "stream ?" : stream, stream === "ir" ? "warn" : "muted",
          "SnapshotOutputs.stream — detected, not assumed");
        if (out.stale === true) {
          push("STALE frame", "bad",
            "the capture predates the motion before it — a servo iteration solving against " +
            "this would never converge");
        } else if (out.stale === false) {
          push("fresh", "good", "SnapshotOutputs.stale is false");
        } else {
          push("freshness ?", "warn",
            "SnapshotOutputs.stale is absent — treated as unknown, never as fresh");
        }
      }
      break;
    }

    case "camera.search_code": {
      const out = outputsOf(item, "search_code");
      const id = num(out?.marker_id) ?? num(params.marker_id);
      push(`id ${id ?? "?"}`, "info");
      if (out) {
        push(out.found ? "found" : "not found", out.found ? "good" : "bad");
        const elapsed = num(out.elapsed_s);
        if (elapsed !== null) push(`${elapsed.toFixed(1)} s`, "muted");
      }
      break;
    }

    case "vision.identify": {
      const out = outputsOf(item, "identify");
      push(out?.target ?? str(params.target) ?? "identify", "info");
      if (out) {
        push(out.found ? "found" : "not found", out.found ? "good" : "bad",
          "IdentifyOutputs.found — absence is reported as absence (R-VIS-1/2)");
        const method = out.method ?? "none";
        push(method, method === "tag_anchored" ? "muted" : method === "classical" ? "warn" : "bad",
          "IdentifyOutputs.method — the classical path is the fallback (D27)");
        const score = num(out.score);
        if (score !== null) push(`score ${score.toFixed(2)}`, "muted");
      }
      break;
    }

    case "vision.solve_offset": {
      const out = outputsOf(item, "offset");
      if (!out) {
        push("solve offset", "info");
        break;
      }
      if (out.method === "refused") {
        push(`REFUSED: ${out.refusal || "no reason given"}`, "bad",
          "OffsetOutputs.refusal — a refusal is an outcome, not an error (D17/D18)");
        break;
      }
      const magnitude = num(out.magnitude_mm);
      if (magnitude !== null) {
        const previous = ctx.previousMagnitude(item.aid);
        const arrow = previous === null ? ""
          : magnitude < previous - 0.05 ? " ↓" : magnitude > previous + 0.05 ? " ↑" : " →";
        push(`${mm(magnitude)}${arrow}`,
          previous !== null && magnitude > previous ? "warn" : "info",
          previous === null
            ? "OffsetOutputs.magnitude_mm — the remaining offset"
            : `remaining offset; previous solve in this loop was ${mm(previous)} ` +
              "(the trend is computed here — there is no trend field)");
      } else {
        push("magnitude unknown", "warn",
          "OffsetOutputs.magnitude_mm is null — it is null unless every axis the move can " +
          "act on was observed (review B9). Not a convergence claim");
      }
      push(out.method ?? "?", out.method === "tag_3d" ? "muted" : "warn",
        "OffsetOutputs.method — tag_3d is the primary path, the jacobian is degraded");
      const observed = out.observed_axes ?? [];
      if (observed.length && observed.length < 3) {
        push(`axes ${observed.join("")}`, "warn",
          "OffsetOutputs.observed_axes — an axis that was not observed is not zero");
      }
      break;
    }

    case "control.loop": {
      const out = outputsOf(item, "loop");
      const max = num(params.max_iterations);
      const iterations = ctx.loopIterations(item.aid);
      const current = num(out?.iterations)
        ?? (iterations.length ? iterations[iterations.length - 1].iteration : null);
      push(`iter ${current ?? 0}/${max ?? "?"}`, "info");
      const last = iterations[iterations.length - 1];
      if (last && !out) {
        const magnitude = num(last.magnitude_mm);
        if (magnitude !== null) {
          push(`${mm(magnitude)}${last.improving === false ? " ↑" : " ↓"}`,
            last.improving === false ? "warn" : "muted",
            "LoopIteration.magnitude_mm / improving");
        }
      }
      if (out) {
        // Non-blocking 1: `outcome` is "aborted" when a *child failed*, and the Literal has no
        // "failed" member. Status and the error message are the authority.
        const status = item.result?.status;
        if (out.outcome === "aborted" && ctx.runState !== "aborted") {
          push(status === "failed" ? "stopped: a step inside the loop failed" : "stopped", "bad",
            item.result?.error?.message
            ?? "LoopOutputs.outcome reads 'aborted' for a child failure too — the status is " +
               "the authority (review non-blocking 1)");
        } else {
          push(out.outcome ?? "?",
            out.outcome === "converged" ? "good" : out.outcome === "exhausted" ? "warn" : "bad",
            "LoopOutputs.outcome — converged / stalled / exhausted are three different things");
        }
        const final = num(out.final_magnitude_mm);
        const threshold = num(out.threshold_mm);
        if (final !== null) {
          push(`final ${mm(final)}${threshold !== null ? ` (≤ ${mm(threshold)})` : ""}`, "muted");
        }
      }
      break;
    }

    case "lifecycle.initialize": {
      const out = outputsOf(item, "initialize");
      if (!out) {
        push(str(params.device) ? `device ${str(params.device)}` : "every device", "info");
        break;
      }
      const devices = out.devices ?? [];
      const connected = devices.filter((d) => d.connected).length;
      push(`${connected}/${devices.length} connected`,
        connected === devices.length ? "good" : "warn", "InitializeOutputs.devices");
      if (out.homed?.length) push(`homed ${out.homed.join(" ")}`, "muted");
      if (out.home_missing?.length) {
        push(`no taught HOME: ${out.home_missing.join(" ")}`, "warn",
          "InitializeOutputs.home_missing — skipped, never a guessed home move (R-INIT-4)");
      }
      break;
    }

    case "lifecycle.reconnect": {
      const out = outputsOf(item, "reconnect");
      push(out?.scope ?? str(params.scope) ?? "engage", "info");
      if (out) {
        push(out.connected ? "connected" : "not connected", out.connected ? "good" : "bad");
        if (out.scope !== "connect") {
          push(out.enabled ? "enabled" : "not enabled", out.enabled ? "good" : "bad");
        }
        if (out.scope === "engage") {
          push(out.verified ? "verified" : "unverified", out.verified ? "good" : "warn",
            "verified with a zero-distance move — 'enabled' is the weaker claim (Q5)");
        }
        if (out.cleared_errors?.length) push(`cleared ${out.cleared_errors.length}`, "muted");
      }
      break;
    }

    case "control.checkpoint": {
      const out = outputsOf(item, "checkpoint");
      const message = out?.message || str(params.message);
      push(message || "checkpoint", "info");
      if (out) push(out.acknowledged ? "acknowledged" : "waiting", out.acknowledged ? "good" : "warn");
      break;
    }

    default:
      // A kind this build does not know: name it rather than render an empty row.
      push(item.row.kind, "warn", "no renderer for this kind in this build");
  }

  // Run-wide facts, appended to every kind.
  const attempt = num(item.result?.attempt);
  if (attempt !== null && attempt > 1) {
    push(`attempt ${attempt}`, "warn", "each retry is recorded separately (R-ENG-15)");
  }
  if (item.result?.error) {
    push(item.result.error.type || "error", "bad", item.result.error.message);
  }
  const warnings = item.result?.warnings ?? [];
  if (warnings.length) {
    push(`⚠ ${warnings.length}`, "warn", warnings.map((w) => `${w.code}: ${w.message}`).join("\n"));
  }
  return bits;
}

export const TONE_CLASS: Record<Tone, string> = {
  muted: "text-deck-400",
  info: "text-deck-100",
  good: "text-emerald-400",
  warn: "text-amber-300",
  bad: "text-red-400",
};
