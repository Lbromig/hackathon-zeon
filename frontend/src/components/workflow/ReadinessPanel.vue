<script setup lang="ts">
// The readiness panel (R-UI-13): initialization state, every pre-flight problem, and the
// warnings — including "no taught HOME".
//
// Three sources, because no single one carries all of it:
//   * `RunSnapshot.warnings` / `ReadinessChanged.warnings` — pre-flight problems only;
//   * `ActionResult.warnings` — where `home_not_defined` and R-CAM-15's IR warning actually
//     arrive (review non-blocking 3), unioned by `Warning_.code` in the store;
//   * `GET /api/arms/waypoints` — because pre-flight never checks HOME at all: the handover
//     plan does not reference it, so an untaught HOME is invisible to the plan's pre-flight
//     and would silently skip the home move at initialization (R-INIT-4).
import { computed, onMounted, ref } from "vue";
import { getWaypointReport, type WaypointReport } from "../../api/engine";
import { useEngine } from "../../stores/engine";

const { state, allWarnings } = useEngine();

const waypoints = ref<WaypointReport | null>(null);
const waypointError = ref("");

onMounted(async () => {
  try {
    waypoints.value = await getWaypointReport();
  } catch (e) {
    waypointError.value = e instanceof Error ? e.message : String(e);
  }
});

/** Arms whose `HOME` waypoint is not taught. Named per arm: HOME means a different pose on
 *  each, so "no taught HOME" without the arm is not actionable (R-WP-4). */
const homeMissing = computed(() =>
  (waypoints.value?.devices ?? [])
    .filter((d) => d.waypoints.some((w) => w.name === "HOME" && !w.taught))
    .map((d) => d.device),
);

const untaught = computed(() =>
  (waypoints.value?.devices ?? []).flatMap((d) =>
    d.waypoints.filter((w) => !w.taught).map((w) => `${d.device}/${w.name}`)),
);

const blocking = computed(() => (state.preflight?.problems ?? []).filter((p) => p.blocking !== false));
const advisory = computed(() => (state.preflight?.problems ?? []).filter((p) => p.blocking === false));

const READINESS_LOOK: Record<string, string> = {
  ready: "border-emerald-500 text-emerald-300",
  degraded: "border-amber-500 text-amber-300",
  failed: "border-red-500 text-red-300",
  initializing: "border-deck-600 text-deck-300",
};

const socketLook = computed(() => {
  if (state.api === "absent") return { text: "engine API unavailable", cls: "text-red-400" };
  switch (state.socket) {
    case "live": return { text: "event stream live", cls: "text-emerald-400" };
    case "connecting": return { text: "connecting…", cls: "text-amber-300" };
    case "closed": return { text: "event stream closed — retrying", cls: "text-red-400" };
    default: return { text: "not connected", cls: "text-deck-400" };
  }
});
</script>

<template>
  <section class="card">
    <h2 class="card-title">Readiness</h2>

    <div class="flex flex-wrap items-center gap-2">
      <span
        class="rounded-md border px-2 py-1 text-xs font-bold uppercase tracking-wide"
        :class="READINESS_LOOK[state.readiness] ?? READINESS_LOOK.initializing"
      >
        {{ state.readiness }}
      </span>
      <span class="text-xs" :class="socketLook.cls">{{ socketLook.text }}</span>
      <span v-if="allWarnings.length" class="text-xs text-amber-300">
        {{ allWarnings.length }} warning{{ allWarnings.length === 1 ? "" : "s" }}
      </span>
    </div>

    <!-- Blocking problems. Start is refused at `failed`, and every one of them is rendered:
         "the plan cannot run" without the list is not a diagnosis. -->
    <div v-if="blocking.length" class="mt-3">
      <p class="text-xs font-semibold uppercase tracking-wide text-red-400">
        blocking — start is refused
      </p>
      <ul class="mt-1 space-y-1">
        <li v-for="(p, i) in blocking" :key="`b${i}`" class="text-xs text-red-300">
          <span class="num rounded bg-red-950/60 px-1">{{ p.code }}</span>
          {{ p.message }}
          <span v-if="p.device" class="text-deck-400">· {{ p.device }}</span>
          <span v-if="p.aid != null" class="text-deck-400">· aid {{ p.aid }}</span>
        </li>
      </ul>
    </div>

    <div v-if="advisory.length" class="mt-3">
      <p class="text-xs font-semibold uppercase tracking-wide text-amber-300">
        advisory — start needs confirmation
      </p>
      <ul class="mt-1 space-y-1">
        <li v-for="(p, i) in advisory" :key="`a${i}`" class="text-xs text-amber-200">
          <span class="num rounded bg-amber-950/60 px-1">{{ p.code }}</span>
          {{ p.message }}
          <span v-if="p.device" class="text-deck-400">· {{ p.device }}</span>
        </li>
      </ul>
    </div>

    <div v-if="homeMissing.length" class="mt-3">
      <p class="text-xs text-amber-200">
        <span aria-hidden="true">⚠</span>
        <strong>no taught HOME</strong> on {{ homeMissing.join(", ") }} — initialization skips
        the home move for that arm rather than guessing one (R-INIT-4).
      </p>
    </div>

    <div v-if="allWarnings.length" class="mt-3">
      <p class="text-xs font-semibold uppercase tracking-wide text-deck-400">warnings</p>
      <ul class="mt-1 space-y-1">
        <li v-for="(w, i) in allWarnings" :key="`w${i}`" class="text-xs text-amber-200">
          <span aria-hidden="true">⚠</span>
          <span class="num rounded bg-deck-900 px-1">{{ w.code }}</span>
          {{ w.message }}
          <span v-if="w.device" class="text-deck-400">· {{ w.device }}</span>
          <span v-if="w.fromAid != null" class="text-deck-400">· from aid {{ w.fromAid }}</span>
        </li>
      </ul>
    </div>

    <p v-if="untaught.length" class="mt-3 text-xs text-deck-400">
      {{ untaught.length }} spec waypoint(s) not taught:
      <span class="num">{{ untaught.slice(0, 6).join(", ") }}</span>
      <span v-if="untaught.length > 6"> …</span>
    </p>
    <p v-else-if="waypoints" class="mt-3 text-xs text-emerald-400">
      all {{ waypoints.total }} spec waypoints taught
    </p>
    <p v-if="waypointError" class="mt-2 text-xs text-red-400">
      waypoint library unreadable: {{ waypointError }}
    </p>

    <!-- Transport health. A dropped frame is why a row would sit at "running" forever, so it
         is reported rather than logged to a console nobody has open. -->
    <p class="mt-3 border-t border-deck-700 pt-2 text-[11px] text-deck-400">
      seq {{ state.lastSeq }} · resyncs {{ state.resyncs }} · gaps {{ state.gaps }} ·
      duplicates dropped {{ state.dropped }}
      <span v-if="state.orderingDegraded" class="text-amber-300">
        · ⚠ a gap could not be repaired immediately — this view may be incomplete
      </span>
    </p>
    <ul v-if="state.badFrames.length" class="mt-1 space-y-0.5">
      <li v-for="(f, i) in state.badFrames" :key="`f${i}`" class="num text-[11px] text-red-400">
        unreadable frame · {{ f }}
      </li>
    </ul>
  </section>
</template>
