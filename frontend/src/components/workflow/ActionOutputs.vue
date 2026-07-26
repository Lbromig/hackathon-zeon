<script setup lang="ts">
// One `Outputs` union member, rendered in full (R-UI-4).
//
// `summarize.ts` answers "the one fact that matters for this kind" on the row; this answers
// "everything the action actually recorded" when the row is expanded. Three rules:
//
//  * **Absent is not false, and null is not zero.** A field the backend did not send reads
//    `not recorded`; `residual_offset_mm.z === null` reads `unobservable`, which is the whole
//    point of the field (`dict[str, float | None]` in `actions.py`).
//  * **A refusal is an outcome, not an error** (D17/D18). `method === "refused"` gets a banner
//    with `refusal` verbatim — the backend refuses several solves deliberately, and the reason
//    is the operator's answer.
//  * **`view_disagreement_mm` is advisory and labelled as such** (Q4/D16, §3.5). The loop
//    terminates on `magnitude_mm` against `threshold_mm` and ignores disagreement entirely, so
//    presenting it as a gate would send an operator chasing a number nothing reads.
//
// A raw-JSON `<details>` is always available: this renderer knows the frozen fields, and the
// day a field is added it must still be visible rather than silently dropped.
import { computed } from "vue";
import {
  OUTPUTS_KIND_FOR_ACTION, type ActionKind, type Artifact, type Outputs,
} from "../../api/engine";
import { TONE_CLASS, type Tone } from "./summarize";
import ArtifactImage from "./ArtifactImage.vue";

const props = defineProps<{
  outputs?: Outputs | null;
  /** The *action* kind (`arm.waypoint`), not the outputs discriminant (`move`). */
  actionKind: string;
  artifacts?: Artifact[];
  runId?: string;
}>();

interface Field {
  label: string;
  value: string;
  tone?: Tone;
  title?: string;
}

const ABSENT = "not recorded";
const ABSENT_TITLE =
  "the backend did not send this field — rendered as unknown, never as false or 0";

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** Short, honest number: no invented precision, and a non-finite value says so. */
function n(v: unknown, digits = 2): string {
  if (typeof v !== "number") return ABSENT;
  if (!Number.isFinite(v)) return String(v);            // NaN / Infinity, if one ever arrives
  return String(Number(v.toFixed(digits)));
}

function vector(v: unknown, digits = 1): string {
  if (!Array.isArray(v) || !v.length) return "";
  return v.map((x) => n(x, digits)).join("  ");
}

/** `{x: 1, y: null}` as `x 1 · y unobservable`, keeping every key the backend sent. */
function record(v: unknown, digits = 2): string {
  if (!v || typeof v !== "object" || Array.isArray(v)) return "";
  const entries = Object.entries(v as Record<string, unknown>);
  if (!entries.length) return "";
  return entries
    .map(([key, value]) => `${key} ${value === null ? "unobservable" : n(value, digits)}`)
    .join(" · ");
}

const flag = (v: unknown, yes: string, no: string): Field["value"] =>
  v === true ? yes : v === false ? no : ABSENT;

const flagTone = (v: unknown, good: Tone = "good", bad: Tone = "bad"): Tone =>
  v === true ? good : v === false ? bad : "warn";

/** The outputs, only when their discriminant is the one this action kind must produce. */
const expectedKind = computed<Outputs["kind"] | null>(
  () => OUTPUTS_KIND_FOR_ACTION[props.actionKind as ActionKind] ?? null,
);

const mismatch = computed(() => {
  const outputs = props.outputs;
  if (!outputs || !expectedKind.value) return "";
  if (outputs.kind === expectedKind.value) return "";
  return `this ${props.actionKind} recorded ${outputs.kind} outputs, but the contract says `
    + `${expectedKind.value}. The runner fails a mis-typed result with a TypeError, so either `
    + `this row is a bug or this build's contract is stale — the fields below are read as sent.`;
});

/** `vision.solve_offset` refused. Rendered whole, above everything else. */
const refusal = computed(() => {
  const outputs = props.outputs;
  if (!outputs || outputs.kind !== "offset") return "";
  if (outputs.method !== "refused" && !outputs.refusal) return "";
  return outputs.refusal || "the solve was refused, and the backend sent no reason";
});

/** Per-axis residual and σ, side by side, with observability called out (R-VIS-4, B9). */
const axisRows = computed(() => {
  const outputs = props.outputs;
  if (!outputs || outputs.kind !== "offset") return [];
  const residual = outputs.residual_offset_mm ?? {};
  const sigma = outputs.sigma_mm ?? {};
  const observed = outputs.observed_axes ?? [];
  const names = Array.from(new Set([...Object.keys(residual), ...Object.keys(sigma), ...observed]));
  return names.map((axis) => ({
    axis,
    residual: residual[axis] === null ? "unobservable"
      : residual[axis] === undefined ? ABSENT : n(residual[axis], 2),
    sigma: isNum(sigma[axis]) ? n(sigma[axis], 2) : ABSENT,
    observed: observed.includes(axis),
  }));
});

const devices = computed(() => {
  const outputs = props.outputs;
  if (!outputs || outputs.kind !== "initialize") return [];
  return outputs.devices ?? [];
});

const fields = computed<Field[]>(() => {
  const outputs = props.outputs;
  if (!outputs) return [];
  const out: Field[] = [];
  const push = (label: string, value: string, tone?: Tone, title?: string) => {
    if (value === "") return;
    out.push({ label, value, tone, title });
  };

  switch (outputs.kind) {
    case "initialize":
      push("homed", outputs.homed?.length ? outputs.homed.join(", ") : "none", "muted");
      push("no taught HOME",
        outputs.home_missing?.length ? outputs.home_missing.join(", ") : "none",
        outputs.home_missing?.length ? "warn" : "good",
        "R-INIT-4: an arm with no taught HOME is skipped and warned about, never sent to a "
        + "guessed pose");
      break;

    case "reconnect":
      push("scope", outputs.scope, "info");
      push("connected", flag(outputs.connected, "yes", "no"), flagTone(outputs.connected));
      push("enabled", flag(outputs.enabled, "yes", "no"), flagTone(outputs.enabled));
      push("verified", flag(outputs.verified, "yes", "no"), flagTone(outputs.verified, "good", "warn"),
        "`engage` verifies with a zero-distance move — 'enabled' is the weaker claim (Q5)");
      push("cleared errors",
        outputs.cleared_errors?.length ? outputs.cleared_errors.join(", ") : "none", "muted");
      break;

    case "move":
      push("waypoint", outputs.waypoint || ABSENT, outputs.waypoint ? "info" : "muted");
      push("motion path", outputs.path || ABSENT, outputs.path ? "info" : "warn",
        outputs.path
          ? "MoveOutputs.path — which path the move actually took: replayed joints, replayed "
            + "joints plus a cartesian offset, or solved IK"
          : "MoveOutputs.path is empty. Not shown as 'cartesian': that would be a guess about "
            + "the one question worth asking after an unexpected 300 mm trajectory");
      push("offsets mm", record(outputs.offsets_mm) || ABSENT, "muted");
      push("pose before", vector(outputs.pose_before) || ABSENT, "muted", "x y z roll pitch yaw");
      push("pose after", vector(outputs.pose_after) || ABSENT, "muted", "x y z roll pitch yaw");
      push("joints before", vector(outputs.joints_before) || ABSENT, "muted", "degrees, J1…J6");
      push("joints after", vector(outputs.joints_after) || ABSENT, "muted", "degrees, J1…J6");
      push("resolved speed", record(outputs.resolved_speed) || ABSENT, "muted",
        "the tier as the driver resolved it, not as the plan asked");
      break;

    case "gripper":
      push("commanded", outputs.state, "info");
      push("width before", isNum(outputs.width_before) ? n(outputs.width_before, 0) : ABSENT, "muted",
        "gripper counts");
      push("width after", isNum(outputs.width_after) ? n(outputs.width_after, 0) : ABSENT, "muted",
        "gripper counts");
      break;

    case "decap":
      push("bites", isNum(outputs.bites) ? String(outputs.bites) : ABSENT, "info");
      push("step", isNum(outputs.step_deg) ? `${n(outputs.step_deg, 0)}°` : ABSENT, "info");
      push("total rotation",
        isNum(outputs.total_rotation_deg) ? `${n(outputs.total_rotation_deg, 0)}°` : ABSENT, "muted");
      push("net wrist travel",
        isNum(outputs.net_wrist_travel_deg) ? `${n(outputs.net_wrist_travel_deg, 1)}°` : ABSENT,
        isNum(outputs.net_wrist_travel_deg) && Math.abs(outputs.net_wrist_travel_deg) < 0.5
          ? "good" : "warn",
        "must be ~0: a ratchet that walked J6 toward its limit shows up here and nowhere else");
      push("ratchet pre-flighted", flag(outputs.preflight_ok, "yes", "NO"),
        flagTone(outputs.preflight_ok),
        "the entire ratchet — every intermediate angle — is checked before the first move");
      push("wrist rewound first", flag(outputs.wrist_rewound_before_decap, "yes", "no"),
        outputs.wrist_rewound_before_decap === true ? "warn" : flagTone(!outputs.wrist_rewound_before_decap, "good", "warn"),
        "J6 was already wound too far for a fresh turn, so it was unwound before the ratchet");
      push("rewind", isNum(outputs.rewind_deg) ? `${n(outputs.rewind_deg, 1)}°` : ABSENT, "muted");
      break;

    case "traverse":
      push("reached", outputs.reached?.length ? outputs.reached.join(" → ") : ABSENT, "info");
      push("blend",
        outputs.blend_deg === null ? "point-to-point"
          : isNum(outputs.blend_deg) ? `${n(outputs.blend_deg, 1)}°` : ABSENT,
        "muted",
        "TraverseOutputs.blend_deg — the radius actually used after the shortest-segment clamp, "
        + "not the one requested");
      push("resolved speed", record(outputs.resolved_speed) || ABSENT, "muted");
      break;

    case "lh_move":
      push("requested mm", record(outputs.requested_mm) || ABSENT, "info");
      push("applied mm", record(outputs.applied_mm) || ABSENT, "info");
      push("position before", record(outputs.position_before) || ABSENT, "muted");
      push("position after", record(outputs.position_after) || ABSENT, "muted");
      push("provenance", outputs.provenance ?? ABSENT,
        outputs.provenance === "measured" ? "good" : outputs.provenance ? "warn" : "warn",
        "R-LH-4: a dead-reckoned position must not read as a measurement");
      push("drift", isNum(outputs.drift_mm) ? `${n(outputs.drift_mm, 2)} mm`
        : outputs.drift_mm === null ? "not measurable" : ABSENT, "muted");
      break;

    case "snapshot":
      push("size", isNum(outputs.width) && isNum(outputs.height)
        ? `${outputs.width} × ${outputs.height} px` : ABSENT, "muted");
      push("captured at", outputs.captured_at || ABSENT, "muted",
        "the child's own capture timestamp, which is what makes freshness decidable (D20)");
      push("freshness", outputs.stale === true ? "STALE — predates the move before it"
        : outputs.stale === false ? "fresh" : "unknown",
        outputs.stale === true ? "bad" : outputs.stale === false ? "good" : "warn",
        outputs.stale === undefined
          ? "SnapshotOutputs.stale is absent — unknown, never assumed fresh"
          : "a servo iteration solving against a pre-move frame can never converge");
      push("achieved mode", outputs.achieved_mode || ABSENT, "muted",
        "read back off the device, not the mode that was requested");
      push("stream", outputs.stream ?? ABSENT, outputs.stream === "ir" ? "warn" : "muted",
        "detected, not assumed — an IR stream reaching a colour detector is a silent failure");
      break;

    case "search_code":
      push("marker id", String(outputs.marker_id), "info");
      push("found", flag(outputs.found, "yes", "no"), flagTone(outputs.found));
      push("centre px", vector(outputs.center_px, 1)
        || (outputs.center_px === null ? "not seen" : ABSENT), "muted");
      push("elapsed", isNum(outputs.elapsed_s) ? `${n(outputs.elapsed_s, 1)} s` : ABSENT, "muted");
      break;

    case "identify":
      push("target", outputs.target, "info");
      push("found", flag(outputs.found, "yes", "no"), flagTone(outputs.found),
        "absence is reported as absence, never as a low-confidence guess (R-VIS-1/2)");
      push("method", outputs.method ?? ABSENT,
        outputs.method === "tag_anchored" ? "good" : outputs.method === "classical" ? "warn" : "bad",
        "the fiducial path is primary; `classical` is the fallback that carried this one (D27)");
      push("score", isNum(outputs.score) ? n(outputs.score, 3) : ABSENT, "muted");
      push("marker id", isNum(outputs.marker_id) ? String(outputs.marker_id)
        : outputs.marker_id === null ? "no tag" : ABSENT, "muted");
      push("point px", vector(outputs.point_px, 1)
        || (outputs.point_px === null ? "not found" : ABSENT), "muted");
      push("T_cam_feature", Array.isArray(outputs.T_cam_feature)
        ? `${outputs.T_cam_feature.length}×${outputs.T_cam_feature[0]?.length ?? 0} pose`
        : outputs.T_cam_feature === null ? "no 3D pose" : ABSENT, "muted",
        "the feature pose in the camera frame — the primary O1 path's actual input");
      break;

    case "offset":
      push("magnitude", isNum(outputs.magnitude_mm) ? `${n(outputs.magnitude_mm, 2)} mm`
        : outputs.magnitude_mm === null ? "null — not every corrected axis was observed" : ABSENT,
        isNum(outputs.magnitude_mm) ? "info" : "warn",
        "the remaining offset, over the observed axes only. `null` unless every axis the loop "
        + "is responsible for was observed (review B9) — never a convergence claim");
      push("method", outputs.method ?? ABSENT,
        outputs.method === "tag_3d" ? "good" : outputs.method === "refused" ? "bad" : "warn",
        "tag_3d is the primary 3D path; the axis-decoupled jacobian is the degraded one");
      push("view disagreement",
        isNum(outputs.view_disagreement_mm) ? `${n(outputs.view_disagreement_mm, 2)} mm (advisory)`
          : outputs.view_disagreement_mm === null ? "single view" : ABSENT,
        "muted",
        "ADVISORY ONLY. The loop terminates on the magnitude against threshold_mm and ignores "
        + "this entirely (Q4/D16): two views need never agree to a fixed tolerance");
      push("condition number",
        isNum(outputs.condition_number) ? n(outputs.condition_number, 1)
          : outputs.condition_number === null ? "not finite / not computed" : ABSENT,
        "muted", "an ill-conditioned solve is refused rather than reported (O7/D17)");
      push("singular values", vector(outputs.singular_values, 3) || ABSENT, "muted");
      push("contributions", outputs.contributions?.length
        ? `${outputs.contributions.length} view(s) — see raw` : ABSENT, "muted");
      break;

    case "loop":
      push("outcome", outputs.outcome ?? ABSENT,
        outputs.outcome === "converged" ? "good"
          : outputs.outcome === "exhausted" ? "warn" : outputs.outcome ? "bad" : "muted",
        "converged / stalled / exhausted are three different things: 'stalled' means the "
        + "calibration is stale or wrong-signed and more iterations will not help. `aborted` is "
        + "also what a child failure produces — the result status is the authority");
      push("iterations", isNum(outputs.iterations) ? String(outputs.iterations) : ABSENT, "info");
      push("materialized rows",
        isNum(outputs.materialized) ? String(outputs.materialized) : ABSENT, "muted");
      push("final magnitude", isNum(outputs.final_magnitude_mm)
        ? `${n(outputs.final_magnitude_mm, 2)} mm` : outputs.final_magnitude_mm === null
          ? "null — never fully observed" : ABSENT, "info");
      push("threshold", isNum(outputs.threshold_mm) ? `${n(outputs.threshold_mm, 2)} mm` : ABSENT,
        "muted", "the convergence gate the magnitude is tested against");
      break;

    case "checkpoint":
      push("message", outputs.message || "(none)", "info");
      push("acknowledged", flag(outputs.acknowledged, "yes", "waiting"),
        flagTone(outputs.acknowledged, "good", "warn"));
      break;

    default:
      break;
  }
  return out;
});

const raw = computed(() => JSON.stringify(props.outputs ?? null, null, 2));
const imageArtifacts = computed(() => props.artifacts ?? []);
</script>

<template>
  <div class="space-y-3">
    <p
      v-if="mismatch"
      class="rounded-lg border border-red-500/60 bg-red-950/40 px-3 py-2 text-xs text-red-200"
    >
      <span aria-hidden="true">✗</span> {{ mismatch }}
    </p>

    <!-- A refusal is an outcome. Whole, verbatim, above the fields. -->
    <div
      v-if="refusal"
      class="rounded-lg border border-red-500/70 bg-red-950/50 px-3 py-2"
      role="note"
    >
      <p class="text-xs font-bold uppercase tracking-wide text-red-300">
        <span aria-hidden="true">⊘</span> solve refused
      </p>
      <p class="mt-1 whitespace-pre-wrap text-xs text-red-100">{{ refusal }}</p>
      <p class="mt-1 text-[11px] text-red-300/80">
        A refusal is a reported outcome, not a crash: the solver declined to turn an
        under-determined measurement into a commanded move.
      </p>
    </div>

    <p v-if="!outputs" class="text-xs text-deck-400">
      No outputs recorded yet — this action has not finished.
    </p>

    <dl v-if="fields.length" class="grid grid-cols-[minmax(7rem,auto)_1fr] gap-x-3 gap-y-1">
      <template v-for="(f, i) in fields" :key="i">
        <dt class="text-xs text-deck-400">{{ f.label }}</dt>
        <dd
          class="num break-words text-xs"
          :class="TONE_CLASS[f.tone ?? 'muted']"
          :title="f.value === 'not recorded' ? (f.title ? `${f.title}\n\n${ABSENT_TITLE}` : ABSENT_TITLE) : f.title"
        >
          {{ f.value }}
        </dd>
      </template>
    </dl>

    <!-- Per axis, because "1.9 mm" over two observed axes and "1.9 mm" over three are not the
         same measurement, and σ is what says which of them to trust. -->
    <div v-if="axisRows.length" class="overflow-x-auto rounded-lg border border-deck-600">
      <table class="w-full border-collapse text-left text-xs">
        <thead class="bg-deck-900 text-deck-400">
          <tr>
            <th class="px-2 py-1 font-semibold">axis</th>
            <th class="px-2 py-1 font-semibold">residual mm</th>
            <th class="px-2 py-1 font-semibold" title="OffsetOutputs.sigma_mm — the per-axis 1σ from the solve covariance">σ mm</th>
            <th class="px-2 py-1 font-semibold">observed</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in axisRows" :key="a.axis" class="border-t border-deck-700/60">
            <td class="num px-2 py-1 text-deck-200">{{ a.axis }}</td>
            <td class="num px-2 py-1" :class="a.residual === 'unobservable' ? 'text-amber-300' : 'text-deck-100'">
              {{ a.residual }}
            </td>
            <td class="num px-2 py-1 text-deck-300">{{ a.sigma }}</td>
            <td class="px-2 py-1" :class="a.observed ? 'text-emerald-400' : 'text-amber-300'">
              <span aria-hidden="true">{{ a.observed ? "✓" : "✗" }}</span>
              {{ a.observed ? "observed" : "not observed" }}
            </td>
          </tr>
        </tbody>
      </table>
      <p class="px-2 py-1 text-[11px] text-deck-400">
        An axis that was not observed is <strong>not</strong> zero — it is unmeasured, and the
        loop may not claim convergence on it (R-VIS-4).
      </p>
    </div>

    <div v-if="devices.length" class="overflow-x-auto rounded-lg border border-deck-600">
      <table class="w-full border-collapse text-left text-xs">
        <thead class="bg-deck-900 text-deck-400">
          <tr>
            <th class="px-2 py-1 font-semibold">device</th>
            <th class="px-2 py-1 font-semibold">connected</th>
            <th class="px-2 py-1 font-semibold">reality</th>
            <th class="px-2 py-1 font-semibold">detail</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(d, i) in devices" :key="i" class="border-t border-deck-700/60 align-top">
            <td class="num px-2 py-1 text-deck-200">{{ d.device ?? "?" }}</td>
            <td class="px-2 py-1" :class="d.connected ? 'text-emerald-400' : 'text-red-400'">
              <span aria-hidden="true">{{ d.connected ? "✓" : "✗" }}</span>
              {{ d.connected ? "connected" : "not connected" }}
            </td>
            <td class="px-2 py-1" :class="d.simulated ? 'text-violet-300' : 'text-emerald-300'">
              {{ d.simulated === undefined ? "unknown" : d.simulated ? "simulated" : "real" }}
            </td>
            <td class="px-2 py-1 text-deck-300">{{ d.detail ?? "" }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Overlays and frames. Through the run's artifact route, never the recorded path. -->
    <div v-if="imageArtifacts.length" class="grid gap-2 sm:grid-cols-2">
      <ArtifactImage
        v-for="(a, i) in imageArtifacts"
        :key="i"
        :artifact="a"
        :run-id="runId ?? ''"
      />
    </div>

    <details v-if="outputs" class="rounded-lg border border-deck-700">
      <summary class="cursor-pointer px-2 py-1 text-[11px] text-deck-400">
        raw outputs ({{ outputs.kind }})
      </summary>
      <pre class="num overflow-x-auto px-2 pb-2 text-[11px] text-deck-200">{{ raw }}</pre>
    </details>
  </div>
</template>
