<script setup lang="ts">
// Whether what you are watching is real. This is a correctness requirement, not chrome.
//
// Simulation is the default (D29), so the dangerous direction is a simulated run that reads as
// real — and the *other* dangerous direction is a real run that reads as simulated, which is
// why "unknown" is rendered as its own loud state rather than assumed to be simulation.
//
// Two independent witnesses, and a mixed run must be as visible as either pure case:
//   * the per-device `simulated` map from `run_started` / the snapshot (D25);
//   * `ActionResult.simulated` per action — a pure-computation action has no device, but is
//     still simulated when the pixels it read were.
import { computed } from "vue";
import { useEngine } from "../../stores/engine";

const { state, reality, elapsedMs, counts } = useEngine();

const clock = computed(() => {
  const total = Math.floor(elapsedMs.value / 1000);
  if (!total) return "";
  const m = Math.floor(total / 60);
  const sec = String(total % 60).padStart(2, "0");
  return `${m}:${sec}`;
});

const look = computed(() => {
  switch (reality.value.kind) {
    case "simulated":
      return {
        label: "SIMULATED",
        detail: `${reality.value.simulated.length} device(s) simulated · nothing physical will move`,
        box: "border-violet-400 bg-violet-950/60 text-violet-100",
        dot: "bg-violet-400",
      };
    case "real":
      return {
        label: "LIVE HARDWARE",
        detail: `${reality.value.real.length} real device(s) · this moves physical equipment`,
        box: "border-emerald-400 bg-emerald-950/50 text-emerald-100",
        dot: "bg-emerald-400",
      };
    case "mixed":
      return {
        label: "MIXED — PART REAL",
        detail:
          `real: ${reality.value.real.join(", ") || "see per-action flags"} · ` +
          `simulated: ${reality.value.simulated.join(", ") || "see per-action flags"}`,
        box: "border-amber-400 bg-amber-950/60 text-amber-100",
        dot: "bg-amber-400",
      };
    default:
      return {
        label: "REALITY UNKNOWN",
        detail: "no device map yet — load a plan; do not assume simulation",
        box: "border-deck-400 bg-deck-800 text-deck-100",
        dot: "bg-deck-400",
      };
  }
});

/** `pausing` is never rendered as `paused` (§3.5) — the run has not stopped yet. */
const RUN_STATE_TEXT: Record<string, string> = {
  idle: "idle",
  preflight: "pre-flighting",
  running: "running",
  pausing: "pausing… (finishing the current step)",
  paused: "paused",
  complete: "complete",
  failed: "failed",
  aborted: "aborted",
};
</script>

<template>
  <div
    class="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-xl border-2 px-4 py-2.5"
    :class="look.box"
    role="status"
    aria-live="polite"
  >
    <span class="flex items-center gap-2">
      <span class="h-3 w-3 rounded-full" :class="look.dot" aria-hidden="true" />
      <strong class="text-sm font-bold tracking-wide">{{ look.label }}</strong>
    </span>
    <span class="text-xs opacity-90">{{ look.detail }}</span>

    <span class="ml-auto flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
      <span v-if="state.name" class="font-semibold">{{ state.name }}</span>
      <span :class="state.runState === 'pausing' ? 'font-semibold text-amber-300' : ''">
        {{ RUN_STATE_TEXT[state.runState] ?? state.runState }}
      </span>
      <span v-if="counts.total" class="num">{{ counts.complete }}/{{ counts.total }} done</span>
      <span v-if="state.runId" class="num opacity-80">run {{ state.runId.slice(0, 8) }}</span>
      <span v-if="clock" class="num opacity-80">⏱ {{ clock }}</span>
    </span>
  </div>
</template>
