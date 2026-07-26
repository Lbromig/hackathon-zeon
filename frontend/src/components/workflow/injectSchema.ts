// The 14 action kinds as a form schema, transcribed from `backend/app/engine/actions.py`.
//
// R-UI-14 makes the manual path the required one: the inject flow must work with no LLM
// available. So this is not a convenience layer over a chat box — it is the primary way an
// operator authors an action, and it has one job beyond drawing inputs: **produce a payload the
// backend will accept, or say why it will not, before anything is sent.**
//
// Two properties of `ActionBase` shape everything here:
//
//  * `model_config = ConfigDict(extra="forbid")`. A misspelled field is a 422, not a silently
//    ignored one — which is the right behaviour and the reason this file lists fields explicitly
//    rather than posting a free-form object. An ignored `dz` is a move to the wrong place that
//    reports success.
//  * `allow_inf_nan=False`. A NaN offset must never become a commanded move, so every numeric
//    field is checked for finiteness here too, not only server-side.
//
// The bounds below are the pydantic `Field(...)` constraints, copied field for field. Where they
// disagree with the backend the backend wins — this only exists to fail earlier and in language
// an operator can act on.
import type { ActionKind, SlotName } from "../../api/engine";

export const SLOTS: readonly SlotName[] = ["frame", "tip", "tube", "offset", "selected_offset"];
export const SPEED_TIERS = ["slow", "medium", "fast"] as const;
export const FAILURE_POLICIES = ["halt", "continue", "retry"] as const;

export type FieldType =
  | "string" | "text" | "number" | "int" | "bool" | "enum" | "slot" | "device" | "waypoint"
  | "waypoints";

/** What role a `device` field plays, so the picker can offer the right ids (R-WP-3). */
export type DeviceRole = "arm" | "camera" | "lh" | "any";

export interface FieldSpec {
  name: string;
  type: FieldType;
  label: string;
  help: string;
  /** Required by the model: no default, so omitting it is a 422. */
  required?: boolean;
  default?: string | number | boolean | null;
  /** `ge` / `le`, inclusive. */
  min?: number;
  max?: number;
  /** `gt`, exclusive. */
  exclusiveMin?: number;
  maxLength?: number;
  options?: readonly string[];
  /** The model accepts `null`, and an empty input means null rather than 0 or "". */
  nullable?: boolean;
  role?: DeviceRole;
}

export interface KindSpec {
  kind: ActionKind;
  title: string;
  summary: string;
  fields: FieldSpec[];
  /** Set when a form cannot express this kind honestly — the JSON editor is the only path. */
  jsonOnly?: string;
  /**
   * Real model fields no form input can express, carried through the JSON path verbatim.
   *
   * A loop's `body` is the case that matters: it is required (`min_length=1`), it is structural,
   * and a JSON path that dropped it would post a loop the backend rejects for the one field the
   * operator actually wrote.
   */
  passthrough?: readonly string[];
}

const DEVICE = (role: DeviceRole, help: string, required = true): FieldSpec => ({
  name: "device", type: "device", label: "device", help, required, role,
  nullable: !required,
});

const OFFSET_FIELDS: FieldSpec[] = [
  { name: "dx", type: "number", label: "dx (mm)", help: "TCP frame", default: 0 },
  { name: "dy", type: "number", label: "dy (mm)", help: "TCP frame", default: 0 },
  { name: "dz", type: "number", label: "dz (mm)", help: "TCP frame", default: 0 },
];

const FROM_SLOT = (help: string): FieldSpec => ({
  name: "from_slot", type: "slot", label: "from_slot", help, nullable: true, default: null,
});

export const KIND_SPECS: KindSpec[] = [
  {
    kind: "lifecycle.initialize",
    title: "initialize",
    summary: "connect every device (or one), clear faults, enable, capture a frame, home the arms",
    fields: [
      DEVICE("any", "empty = every configured device; one failing never stops the rest", false),
      {
        name: "home_after", type: "bool", label: "home_after", default: true,
        help: "arms move to their taught HOME slowly afterwards. An arm with no taught HOME is "
          + "warned about and skipped — never sent to a guessed pose (R-INIT-4)",
      },
    ],
  },
  {
    kind: "lifecycle.reconnect",
    title: "reconnect",
    summary: "recovery: re-connect, re-enable, or re-engage one device",
    fields: [
      DEVICE("any", "the device to recover"),
      {
        name: "scope", type: "enum", label: "scope", options: ["connect", "enable", "engage"],
        default: "engage", required: true,
        help: "`engage` is clear_errors → enable → verify with a zero-distance move. 'enabled' "
          + "alone is the weaker claim (Q5)",
      },
    ],
  },
  {
    kind: "arm.waypoint",
    title: "move to waypoint",
    summary: "replay a taught waypoint, optionally offset — this is also the home move",
    fields: [
      DEVICE("arm", "the arm that will move"),
      {
        name: "waypoint", type: "waypoint", label: "waypoint", required: true, maxLength: 64,
        help: "a taught name **owned by this arm**. A move naming another arm's waypoint is "
          + "refused at pre-flight, never silently resolved to the same name elsewhere (R-WP-2)",
      },
      ...OFFSET_FIELDS,
    ],
  },
  {
    kind: "arm.move_relative",
    title: "move relative",
    summary: "move the arm by an offset, or by whatever a blackboard slot holds",
    fields: [
      DEVICE("arm", "the arm that will move"),
      ...OFFSET_FIELDS,
      { name: "droll", type: "number", label: "droll (°)", help: "", default: 0 },
      { name: "dpitch", type: "number", label: "dpitch (°)", help: "", default: 0 },
      { name: "dyaw", type: "number", label: "dyaw (°)", help: "", default: 0 },
      FROM_SLOT("when set, dx/dy/dz come from that slot instead of the fields above"),
    ],
  },
  {
    kind: "arm.gripper",
    title: "gripper",
    summary: "open, or close to a width in counts",
    fields: [
      DEVICE("arm", "the arm whose gripper moves"),
      {
        name: "state", type: "enum", label: "state", options: ["open", "close"], required: true,
        help: "", default: "open",
      },
      {
        name: "width", type: "number", label: "width (counts)", nullable: true, default: null,
        help: "empty = fully. The tube and the cap are both held at 298 counts on this bench. "
          + "The driver refuses an out-of-range width rather than clamping it",
      },
    ],
  },
  {
    kind: "arm.decap",
    title: "decap",
    summary: "unscrew a cap in bites, rewinding the wrist between them so net travel is zero",
    fields: [
      DEVICE("arm", "the arm holding the cap"),
      {
        name: "step_deg", type: "number", label: "step_deg", default: 90, exclusiveMin: 0, max: 180,
        help: "degrees per bite. Smaller bites make the pre-flight easier: peak wrist excursion "
          + "halves from 180° to 90°",
      },
      {
        name: "turns", type: "number", label: "turns", default: 1, exclusiveMin: 0, max: 4,
        help: "full turns of the cap",
      },
      {
        name: "grip_counts", type: "number", label: "grip_counts", nullable: true, default: null,
        help: "the width the jaws re-grip to between bites; empty = the driver's default",
      },
    ],
  },
  {
    kind: "arm.traverse",
    title: "traverse",
    summary: "move through an ordered set of waypoints as one blended action",
    fields: [
      DEVICE("arm", "the arm that will move"),
      {
        name: "waypoints", type: "waypoints", label: "waypoints", required: true,
        help: "at least two, in order, comma-separated. Every one is limit-checked before the "
          + "first move: stopping halfway along a travel path leaves the arm somewhere nobody chose",
      },
      {
        name: "blend_deg", type: "number", label: "blend_deg", default: 5, min: 0, max: 45,
        nullable: true,
        help: "corner radius in degrees, clamped server-side to the shortest segment. Empty = "
          + "point-to-point",
      },
    ],
  },
  {
    kind: "lh.move_relative",
    title: "nudge the liquid handler",
    summary: "move the pipette head by x/y/z in mm — what the servo loop drives",
    fields: [
      DEVICE("lh", "the liquid handler"),
      ...OFFSET_FIELDS,
      FROM_SLOT("normally `selected_offset`: the move the solve computed"),
      {
        name: "clamp_mm", type: "number", label: "clamp_mm", default: 15, exclusiveMin: 0,
        help: "a vision-derived step larger than this is **refused, not clamped** — a big step is "
          + "a detection failure wearing a command's clothes (R-LH-3/R-VIS-7)",
      },
    ],
  },
  {
    kind: "camera.snapshot",
    title: "snapshot",
    summary: "capture one frame into a slot, and store it as an artifact",
    fields: [
      DEVICE("camera", "the camera to capture from"),
      {
        name: "fresh", type: "bool", label: "fresh", default: true,
        help: "drain the capture buffer and require a capture timestamp after the last move. A "
          + "cached pre-move frame is exactly what makes a servo loop never converge (R-CAM-2)",
      },
      {
        name: "store", type: "bool", label: "store", default: true,
        help: "write it under the run's artifact directory and emit an Artifact",
      },
      {
        name: "into_slot", type: "slot", label: "into_slot", default: "frame", required: true,
        help: "which blackboard slot receives the frame",
      },
    ],
  },
  {
    kind: "camera.search_code",
    title: "search for a marker",
    summary: "watch a camera until a marker id appears, or time out",
    fields: [
      DEVICE("camera", "the camera to watch"),
      {
        name: "marker_id", type: "int", label: "marker_id", required: true, min: 0,
        help: "the AprilTag / ArUco id to wait for",
      },
      {
        name: "timeout_s", type: "number", label: "timeout_s", default: 15, exclusiveMin: 0,
        max: 120, help: "give up after this long, and report not-found rather than hanging",
      },
    ],
  },
  {
    kind: "vision.identify",
    title: "identify tip or tube",
    summary: "locate the tip bottom or the tube top in one camera's frame",
    fields: [
      DEVICE("camera", "the camera whose frame is read"),
      {
        name: "target", type: "enum", label: "target", options: ["tip", "tube"], required: true,
        default: "tip",
        help: "one kind with a target rather than two detectors: both report **absence** as "
          + "absence rather than a low-confidence guess (R-VIS-1/2)",
      },
      {
        name: "from_slot", type: "slot", label: "from_slot", default: "frame", required: true,
        help: "the frame to read",
      },
      {
        name: "into_slot", type: "slot", label: "into_slot", nullable: true, default: null,
        help: "empty = the slot matching the target (`tip` or `tube`)",
      },
    ],
  },
  {
    kind: "vision.solve_offset",
    title: "solve the offset",
    summary: "tip → tube offset across the servo cameras, with the overlay",
    fields: [
      DEVICE("camera", "empty = fuse every configured servo camera; one id restricts it to that "
        + "view, which is the degraded path", false),
      { name: "tip_slot", type: "slot", label: "tip_slot", default: "tip", required: true, help: "" },
      { name: "tube_slot", type: "slot", label: "tube_slot", default: "tube", required: true, help: "" },
      {
        name: "into_slot", type: "slot", label: "into_slot", default: "selected_offset",
        required: true, help: "the slot the loop watches for convergence",
      },
      {
        name: "render_overlay", type: "bool", label: "render_overlay", default: true,
        help: "folded into the solve rather than a separate action: a converged loop with no "
          + "visual record of why it moved is the failure that separate action would allow",
      },
    ],
  },
  {
    kind: "control.loop",
    title: "servo loop",
    summary: "a bounded loop whose every iteration materializes into the plan",
    jsonOnly:
      "A loop carries a `body` of at least one action, and authoring a body of nested actions in "
      + "a flat form would be a worse editor than the JSON. Note also that the engine refuses a "
      + "loop injected into an existing loop region — the one nested-loop route no validator sees.",
    // `body` is required and structural; `until` is a named predicate with one member; and
    // `corrected_axes` declares which axes the loop must have OBSERVED before it may claim
    // convergence — none of the three belongs in a flat form, and all three are real fields.
    passthrough: ["body", "until", "corrected_axes"],
    fields: [
      {
        name: "threshold_mm", type: "number", label: "threshold_mm", default: 1.5,
        exclusiveMin: 0,
        help: "the remaining offset at which the loop is converged. Tested against "
          + "`magnitude_mm`; inter-view disagreement is ignored entirely (Q4/D16)",
      },
      {
        name: "max_iterations", type: "int", label: "max_iterations", default: 12, min: 1, max: 40,
        help: "\"needs longer\" — exhausted",
      },
      {
        name: "no_progress_abort", type: "int", label: "no_progress_abort", default: 3, min: 1,
        help: "consecutive iterations without improvement before the loop stops as `stalled`: "
          + "the calibration is stale or wrong-signed and more iterations will not help",
      },
      {
        name: "watch_slot", type: "slot", label: "watch_slot", default: "selected_offset",
        required: true, help: "",
      },
    ],
  },
  {
    kind: "control.checkpoint",
    title: "checkpoint",
    summary: "an explicit pause point authored into the plan",
    fields: [
      {
        name: "message", type: "text", label: "message", default: "",
        help: "shown to the operator at the stop",
      },
    ],
  },
];

/** Fields every action carries (`ActionBase`), offered separately: rarely changed, never absent. */
export const COMMON_FIELDS: FieldSpec[] = [
  { name: "label", type: "string", label: "label", default: "", maxLength: 120,
    help: "the plan row's text. Empty = auto-filled from the kind" },
  { name: "speed", type: "enum", label: "speed", options: SPEED_TIERS, default: "medium",
    help: "the tier the driver resolves; `arm.decap` defaults to slow" },
  { name: "on_failure", type: "enum", label: "on_failure", options: FAILURE_POLICIES,
    default: "halt",
    help: "`halt` stops the run. `continue` is only honest when the next action does not depend "
      + "on this one" },
  { name: "max_attempts", type: "int", label: "max_attempts", default: 1, min: 1, max: 5,
    help: "each attempt is recorded and emits its own start/finish pair — attempt 2 is shown, "
      + "never substituted for attempt 1" },
  { name: "note", type: "text", label: "note", default: "",
    help: "free text, carried on the action" },
];

export const specFor = (kind: ActionKind): KindSpec | undefined =>
  KIND_SPECS.find((s) => s.kind === kind);

export type FormValues = Record<string, string | number | boolean | null>;

/** The defaults a fresh form starts from — the model's own, so an untouched form is valid. */
export function defaultsFor(kind: ActionKind): FormValues {
  const values: FormValues = {};
  const spec = specFor(kind);
  for (const field of [...(spec?.fields ?? []), ...COMMON_FIELDS]) {
    values[field.name] = field.default ?? (field.nullable ? null : field.type === "bool" ? false : "");
  }
  if (kind === "arm.decap") values.speed = "slow";
  return values;
}

export interface Built {
  errors: Record<string, string>;
  action: Record<string, unknown> | null;
}

function fieldError(field: FieldSpec, raw: unknown): string {
  const empty = raw === null || raw === undefined || raw === "";
  if (empty) {
    if (field.required && field.type !== "bool") return "required — the model has no default";
    return "";
  }
  if (field.type === "number" || field.type === "int") {
    const value = typeof raw === "number" ? raw : Number(String(raw).trim());
    if (!Number.isFinite(value)) {
      return "must be a finite number — the contract rejects NaN and Infinity outright, "
        + "because a NaN offset must never become a commanded move";
    }
    if (field.type === "int" && !Number.isInteger(value)) return "must be a whole number";
    if (field.exclusiveMin !== undefined && value <= field.exclusiveMin) {
      return `must be greater than ${field.exclusiveMin}`;
    }
    if (field.min !== undefined && value < field.min) return `must be at least ${field.min}`;
    if (field.max !== undefined && value > field.max) return `must be at most ${field.max}`;
    return "";
  }
  if (field.type === "waypoints") {
    const names = String(raw).split(",").map((s) => s.trim()).filter(Boolean);
    if (names.length < 2) return "at least two waypoints, comma-separated";
    return "";
  }
  if (field.maxLength && String(raw).length > field.maxLength) {
    return `at most ${field.maxLength} characters`;
  }
  if (field.options && !field.options.includes(String(raw))) {
    return `must be one of ${field.options.join(", ")}`;
  }
  return "";
}

/**
 * Validate the form and build the payload.
 *
 * Only fields with a value are sent, and **an empty nullable field is sent as `null`** rather than
 * omitted: for `blend_deg` that is the difference between point-to-point and the 5° default, and
 * for `width` between "close fully" and the driver's default. Neither is ever coerced to `0` or
 * `""` — one blank box must not be able to mean two different commands. A non-nullable field left
 * empty is omitted, so the model's own default applies.
 */
export function buildAction(kind: ActionKind, values: FormValues): Built {
  const spec = specFor(kind);
  const errors: Record<string, string> = {};
  if (!spec) {
    return { errors: { kind: `this build does not know the kind ${kind}` }, action: null };
  }
  const action: Record<string, unknown> = { kind };

  for (const field of [...spec.fields, ...COMMON_FIELDS]) {
    const raw = values[field.name];
    const problem = fieldError(field, raw);
    if (problem) {
      errors[field.name] = problem;
      continue;
    }
    const empty = raw === null || raw === undefined || raw === "";
    if (empty) {
      if (field.nullable) action[field.name] = null;
      continue;
    }
    switch (field.type) {
      case "number":
      case "int":
        action[field.name] = typeof raw === "number" ? raw : Number(String(raw).trim());
        break;
      case "bool":
        action[field.name] = raw === true || raw === "true";
        break;
      case "waypoints":
        action[field.name] = String(raw).split(",").map((s) => s.trim()).filter(Boolean);
        break;
      default:
        action[field.name] = String(raw);
    }
  }
  // `bool` fields are never "empty": false is a value, and the loop above skips `false` for a
  // field whose default is `true`. Send them explicitly.
  for (const field of [...spec.fields, ...COMMON_FIELDS]) {
    if (field.type === "bool") action[field.name] = values[field.name] === true;
  }
  return { errors, action: Object.keys(errors).length ? null : action };
}

/**
 * Read a pasted action back into the form (the chat tab's "propose" path).
 *
 * Unknown fields are reported rather than dropped: `extra="forbid"` means the backend would 422
 * them, and a form that silently discarded them would send something other than what was read.
 */
export interface Parsed {
  kind: ActionKind | null;
  values: FormValues;
  /** Structural fields the form does not own (`body`), carried through untouched. */
  extra: Record<string, unknown>;
  problems: string[];
}

export function parseAction(text: string): Parsed {
  const problems: string[] = [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (e) {
    return {
      kind: null, values: {}, extra: {},
      problems: [`not JSON: ${e instanceof Error ? e.message : String(e)}`],
    };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { kind: null, values: {}, extra: {}, problems: ["expected a JSON object with a `kind`"] };
  }
  const bag = parsed as Record<string, unknown>;
  const kind = typeof bag.kind === "string" ? bag.kind as ActionKind : null;
  const spec = kind ? specFor(kind) : undefined;
  if (!spec) {
    return {
      kind: null, values: {}, extra: {},
      problems: [`unknown or missing kind: ${String(bag.kind)}`],
    };
  }
  const values = defaultsFor(spec.kind);
  const extra: Record<string, unknown> = {};
  const known = new Set([...spec.fields, ...COMMON_FIELDS].map((f) => f.name));
  const carried = new Set(spec.passthrough ?? []);
  for (const [key, value] of Object.entries(bag)) {
    if (key === "kind") continue;
    if (carried.has(key)) {
      extra[key] = value;
      continue;
    }
    if (!known.has(key)) {
      problems.push(`field \`${key}\` is not on ${spec.kind} — the backend forbids extra fields, `
        + "so this would be rejected rather than ignored");
      continue;
    }
    values[key] = Array.isArray(value) ? value.map(String).join(", ")
      : value === null ? null
        : typeof value === "boolean" || typeof value === "number" ? value
          : String(value);
  }
  if (spec.kind === "control.loop" && !Array.isArray(extra.body)) {
    problems.push("a `control.loop` needs a `body` of at least one action — the engine refuses an "
      + "empty one, and there is nothing for the loop to iterate over");
  }
  return { kind: spec.kind, values, extra, problems };
}
