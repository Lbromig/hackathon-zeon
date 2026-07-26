<script setup lang="ts">
// Start / Pause / Resume / Abort / Inject (R-UI-6, §3.4).
//
// The two things this component exists to get right:
//
//  * **Pause says what it does before the click.** Pause is cooperative: the action in flight
//    runs to completion and the run stops at the next boundary. The button is labelled with
//    that, and while the runner reports `pausing` the label is `pausing…` — never a fake
//    instant stop, and never the word "paused" (§3.5).
//  * **Start refuses honestly.** At readiness `failed` it is disabled and every pre-flight
//    problem is on screen. At `degraded` it opens a confirmation naming each degradation, and
//    only then sends `allow_degraded` (R-ENG-2).
import { computed, ref } from "vue";
import { useEngine } from "../../stores/engine";
import type { PlanName } from "../../api/engine";

const emit = defineEmits<{ (e: "inject"): void }>();
const props = defineProps<{ injectOpen?: boolean }>();

const { state, commands, allWarnings, failedRow, cursorRow, resumeReentersLoop, refresh } =
  useEngine();

const confirmDegraded = ref(false);
const confirmAbort = ref(false);

const apiAbsent = computed(() => state.api === "absent");
const active = computed(() =>
  state.runState === "running" || state.runState === "pausing"
  || state.runState === "paused" || state.runState === "preflight");

const blocking = computed(() =>
  (state.preflight?.problems ?? []).filter((p) => p.blocking !== false));
const degradations = computed(() => {
  const advisory = (state.preflight?.problems ?? [])
    .filter((p) => p.blocking === false)
    .map((p) => `${p.code}: ${p.message}${p.device ? ` (${p.device})` : ""}`);
  const warned = allWarnings.value.map(
    (w) => `${w.code}: ${w.message}${w.device ? ` (${w.device})` : ""}`);
  return Array.from(new Set([...advisory, ...warned]));
});

const startDisabledReason = computed(() => {
  if (apiAbsent.value) return "the engine API is not available";
  if (active.value) return "a run is already in progress";
  if (!state.rows.length) return "no plan is loaded — load one first";
  if (state.readiness === "failed") {
    return `pre-flight failed: ${blocking.value.length} blocking problem(s) — see the readiness panel`;
  }
  return "";
});

async function start() {
  // `degraded` is the one path that needs the operator to say yes to something specific.
  if (state.readiness === "degraded" && !confirmDegraded.value) {
    confirmDegraded.value = true;
    return;
  }
  confirmDegraded.value = false;
  await commands.start(state.readiness === "degraded");
}

async function abort() {
  if (!confirmAbort.value) {
    confirmAbort.value = true;
    return;
  }
  confirmAbort.value = false;
  await commands.abort();
}

const busy = (name: string) => state.busy === name;
</script>

<template>
  <section class="card">
    <div class="flex flex-wrap items-center gap-2">
      <!-- Plan load. Refused with a 409 while a run is live (review B8), so it is disabled
           rather than offered and rejected. -->
      <span class="text-xs text-deck-400">plan</span>
      <button
        v-for="name in (['handover', 'startup'] as PlanName[])"
        :key="name"
        class="chip"
        :class="state.name === name ? 'chip-on' : ''"
        :disabled="apiAbsent || active || !!state.busy"
        :title="active ? 'a run is live — abort it before loading another plan' : `load the ${name} plan`"
        @click="commands.load(name)"
      >
        {{ name }}
      </button>
      <button class="btn btn-sm btn-ghost" :disabled="apiAbsent" title="re-read the snapshot" @click="refresh">
        ↻
      </button>

      <span class="mx-1 h-6 w-px bg-deck-600" aria-hidden="true" />

      <button
        class="btn btn-primary"
        :disabled="!!startDisabledReason || !!state.busy"
        :title="startDisabledReason || 'pre-flight the whole plan, then run it'"
        @click="start"
      >
        <span aria-hidden="true">▶</span> {{ busy("start") ? "Starting…" : "Start" }}
      </button>

      <button
        class="btn"
        :disabled="apiAbsent || state.runState !== 'running' || !!state.busy"
        title="Pause is cooperative: the action in flight finishes, then the run stops at the next boundary"
        @click="commands.pause"
      >
        <span aria-hidden="true">⏸</span>
        {{ state.runState === "pausing" ? "pausing…" : "Pause (finishes current step)" }}
      </button>

      <button
        class="btn"
        :disabled="apiAbsent || (state.runState !== 'paused' && state.runState !== 'failed') || !!state.busy"
        :title="resumeReentersLoop
          ? 'the failed step is inside the servo loop, and the cursor is on it — resuming re-enters the loop and continues that iteration from the failed row'
          : 'continue from the row the cursor is on'"
        @click="commands.resume"
      >
        <span aria-hidden="true">⏵</span>
        {{ resumeReentersLoop ? "Resume (re-enters the servo loop)" : "Resume" }}
      </button>

      <button
        class="btn btn-danger"
        :disabled="apiAbsent || !active || !!state.busy"
        title="Abort stops the run now: a parked handler wakes and raises, and actions never reached stay planned"
        @click="abort"
      >
        <span aria-hidden="true">⏹</span> {{ confirmAbort ? "Abort — click again to confirm" : "Abort" }}
      </button>

      <button
        class="btn ml-auto"
        :class="props.injectOpen ? 'chip-on' : ''"
        :disabled="apiAbsent"
        title="Insert an action after a chosen one"
        @click="emit('inject')"
      >
        <span aria-hidden="true">⤵</span> Inject
      </button>
    </div>

    <p v-if="startDisabledReason && !active" class="mt-2 text-xs text-amber-300">
      Start unavailable — {{ startDisabledReason }}
    </p>

    <p v-if="confirmAbort" class="mt-2 text-xs text-red-300">
      Abort will stop the run at the next checkpoint a handler reaches, and leave devices where
      they are. Click Abort again to confirm, or
      <button class="underline" @click="confirmAbort = false">cancel</button>.
    </p>

    <!-- The degraded confirmation names each degradation. "Are you sure?" is not consent. -->
    <div
      v-if="confirmDegraded"
      class="mt-3 rounded-lg border border-amber-500 bg-amber-950/40 p-3"
      role="alertdialog"
      aria-label="Confirm a degraded start"
    >
      <p class="text-sm font-semibold text-amber-200">
        Readiness is <span class="uppercase">degraded</span>. Starting accepts these:
      </p>
      <ul class="mt-2 space-y-1">
        <li v-for="(d, i) in degradations" :key="i" class="text-xs text-amber-100">· {{ d }}</li>
        <li v-if="!degradations.length" class="text-xs text-amber-100">
          · the engine reported `degraded` without naming a problem — pre-flight
          (<span class="num">GET /api/engine/preflight</span>) returned nothing to show
        </li>
      </ul>
      <div class="mt-3 flex gap-2">
        <button class="btn btn-primary" :disabled="!!state.busy" @click="start">
          Start anyway (allow_degraded)
        </button>
        <button class="btn" @click="confirmDegraded = false">Cancel</button>
      </div>
    </div>

    <!-- The backend's reason, verbatim. Several refusals are deliberate and the operator needs
         the actual text, not our paraphrase. -->
    <p
      v-if="state.commandError"
      class="mt-3 whitespace-pre-wrap rounded-lg border border-red-500/60 bg-red-950/40 px-3 py-2 text-xs text-red-200"
    >
      {{ state.commandError }}
    </p>

    <p v-if="failedRow" class="mt-2 text-xs text-red-300">
      <span aria-hidden="true">✗</span>
      step {{ failedRow.display }} ({{ failedRow.row.kind }}) failed:
      {{ failedRow.result?.error?.message ?? "no message" }}
      <span v-if="resumeReentersLoop" class="text-amber-200">
        — it is inside the servo loop and the run is still on it. Resume re-enters the loop and
        continues from this row; injecting a fix immediately after it is accepted.
      </span>
    </p>

    <!-- The cursor is "where the run is", not "what is next": after a mid-loop failure it sits
         on the failed row itself, and saying "next" there would be a lie about a red row. -->
    <p v-if="cursorRow" class="mt-2 text-xs text-deck-300">
      the run is at <span class="num">#{{ cursorRow.display }}</span>
      {{ cursorRow.row.label || cursorRow.row.kind }}
      <span class="text-deck-400">({{ cursorRow.state }})</span>
    </p>

    <p v-if="state.pausePointer" class="mt-2 text-xs text-deck-300">
      last stop: {{ state.pausePointer.reason }}
      <span v-if="state.pausePointer.aid != null">· at aid {{ state.pausePointer.aid }}</span>
      <span v-if="state.pausePointer.reason === 'action_failed'" class="text-deck-400">
        (a halt, not an operator pause — the run state above is the authority)
      </span>
    </p>
  </section>
</template>
