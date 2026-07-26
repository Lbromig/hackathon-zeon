<script setup lang="ts">
// The workflow-waypoint checklist — the thing the operator actually works down at the
// bench. Replaces the `TeachChecklist.vue` Wave 0 deleted, whose list came from the
// choreography that no longer exists; this one comes from `core/waypoints.py`'s spec,
// which is the same list the engine will pre-flight against.
//
// Two rules shape the whole component:
//
//   * It shows **only the selected arm's own waypoints** (R-WP-3). It never holds a
//     combined list of both arms, so there is no code path that could offer one arm the
//     other's waypoint. The backend refuses such a save anyway; this makes it unaskable.
//   * Progress has to be readable at arm's length — the operator is standing at a bench
//     with one hand on the arm, working through a list of ten.
//
// Re-teaching an already-taught waypoint takes a second click. A stray click on "Teach
// here" would otherwise overwrite a good taught point with wherever the arm happens to be.
import { computed, ref } from "vue";
import type { WaypointRow } from "../../api/teach";
import { useTeach } from "../../composables/useTeach";

const {
  arm,
  selectedId,
  waypoints,
  waypointProblems,
  waypointError,
  nextWaypoint,
  connected,
  canMove,
  sending,
  teachWaypoint,
  gotoWaypoint,
  refreshWaypoints,
} = useTeach();

/** Name awaiting a confirming second click before it is overwritten. */
const confirmOverwrite = ref("");
let confirmTimer: number | undefined;

const percent = computed(() => {
  const w = waypoints.value;
  if (!w || !w.total) return 0;
  return Math.round((w.taught / w.total) * 100);
});

const blocking = computed(() => waypointProblems.value.filter((p) => p.blocking));
const advisory = computed(() => waypointProblems.value.filter((p) => !p.blocking));

const TIER_CLASS: Record<string, string> = {
  slow: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  medium: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  fast: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
};

function shortTime(iso: string | null) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function teach(row: WaypointRow) {
  if (!row.taught) {
    void teachWaypoint(row.name);
    return;
  }
  if (confirmOverwrite.value === row.name) {
    confirmOverwrite.value = "";
    void teachWaypoint(row.name);
    return;
  }
  confirmOverwrite.value = row.name;
  if (confirmTimer) window.clearTimeout(confirmTimer);
  confirmTimer = window.setTimeout(() => (confirmOverwrite.value = ""), 4000);
}
</script>

<template>
  <section class="card lg:col-span-2">
    <div class="flex flex-wrap items-baseline justify-between gap-2">
      <h2 class="card-title mb-0">
        Workflow waypoints
        <span class="text-deck-400">— {{ arm?.name ?? selectedId ?? "no arm" }}</span>
      </h2>
      <div class="flex items-center gap-3">
        <span v-if="waypoints" class="text-sm font-semibold text-deck-100">
          {{ waypoints.taught }} of {{ waypoints.total }} taught
        </span>
        <button class="btn btn-sm btn-ghost" @click="refreshWaypoints()">refresh</button>
      </div>
    </div>

    <p v-if="waypointError" class="mt-2 rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
      Could not read the waypoint checklist: {{ waypointError }}. Treat every waypoint below
      as unknown until this clears — do not assume untaught means untaught.
    </p>

    <p v-else-if="!waypoints" class="mt-3 text-sm text-deck-400">
      No workflow waypoints are owned by
      <span class="num">{{ selectedId || "this arm" }}</span
      >. The 15 waypoints belong to <span class="num">left</span> and
      <span class="num">right</span>; use the pose library below for scratch points on
      other devices.
    </p>

    <template v-else>
      <!-- progress -->
      <div class="mt-3 h-2 w-full overflow-hidden rounded-full bg-deck-700">
        <div
          class="h-full rounded-full transition-all"
          :class="waypoints.complete ? 'bg-emerald-500' : 'bg-sky-500'"
          :style="{ width: `${percent}%` }"
        />
      </div>

      <p v-if="waypoints.complete" class="mt-2 text-sm text-emerald-300">
        Every waypoint on this arm is taught. Check each one with <strong>Go</strong> before
        running the workflow.
      </p>
      <p v-else-if="nextWaypoint" class="mt-2 text-sm text-deck-300">
        Next: <span class="num font-semibold text-deck-100">{{ nextWaypoint.name }}</span>
        — {{ nextWaypoint.note }}
      </p>

      <p v-if="!connected" class="mt-2 text-xs text-amber-400">
        {{ arm?.name ?? selectedId }} is not connected — you can read the list, but teaching
        needs a live arm.
      </p>

      <!-- the list -->
      <ul class="mt-3 divide-y divide-deck-700">
        <li
          v-for="row in waypoints.waypoints"
          :key="row.name"
          class="flex flex-wrap items-center gap-x-3 gap-y-1 py-2"
          :class="{ 'opacity-70': row.taught }"
        >
          <span
            class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border text-xs"
            :class="row.taught
              ? 'border-emerald-500/50 bg-emerald-500/15 text-emerald-300'
              : 'border-deck-600 text-deck-400'"
            :title="row.taught ? 'taught' : 'not taught yet'"
          >
            {{ row.taught ? "✓" : row.step }}
          </span>

          <div class="min-w-0 flex-1">
            <div class="flex flex-wrap items-baseline gap-2">
              <span class="num truncate text-sm font-semibold text-deck-100">{{ row.name }}</span>
              <span
                class="rounded border px-1.5 py-0.5 text-[10px] uppercase tracking-wider"
                :class="TIER_CLASS[row.speed]"
                :title="`intended speed tier for this move`"
              >
                {{ row.speed }}
              </span>
              <span class="text-[10px] uppercase tracking-wider text-deck-500">
                step {{ row.step }}
              </span>
            </div>
            <div class="truncate text-xs text-deck-400">{{ row.note }}</div>
            <div v-if="row.taught" class="text-xs text-deck-500">
              taught {{ shortTime(row.saved_at) }}
              <span v-if="row.has_joints">· joints stored</span>
              <span v-else class="text-amber-400">· no joints — cartesian replay only</span>
            </div>
            <div v-else class="text-xs text-deck-500">not taught</div>
          </div>

          <div class="flex shrink-0 gap-2">
            <button
              class="btn btn-sm"
              :class="row.taught
                ? (confirmOverwrite === row.name ? 'btn-danger' : 'btn-ghost')
                : 'btn-primary'"
              :disabled="!connected || sending"
              :title="row.taught
                ? 'overwrite this taught waypoint with the arm\'s current position'
                : 'save the arm\'s current position as this waypoint'"
              @click="teach(row)"
            >
              {{ row.taught ? (confirmOverwrite === row.name ? "Overwrite?" : "Re-teach") : "Teach here" }}
            </button>
            <button
              class="btn btn-sm"
              :disabled="!canMove || !row.taught"
              :title="row.taught
                ? `move there at the ${row.speed} tier`
                : 'nothing taught to go to'"
              @click="gotoWaypoint(row.name, row.speed)"
            >
              Go
            </button>
          </div>
        </li>
      </ul>

      <!-- problems -->
      <div v-if="blocking.length" class="mt-3 space-y-2">
        <p
          v-for="p in blocking"
          :key="`${p.kind}:${p.name}`"
          class="rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300"
        >
          <strong class="num">{{ p.name }}</strong> — {{ p.detail }}
        </p>
      </div>

      <details v-if="advisory.length" class="mt-3">
        <summary class="cursor-pointer text-xs text-deck-400">
          {{ advisory.length }} pose(s) on this arm are not workflow waypoints
        </summary>
        <p
          v-for="p in advisory"
          :key="`${p.kind}:${p.name}`"
          class="mt-2 text-xs"
          :class="p.kind === 'name_mismatch' ? 'text-amber-400' : 'text-deck-400'"
        >
          <span class="num font-semibold">{{ p.name }}</span> — {{ p.detail }}
        </p>
      </details>
    </template>
  </section>
</template>
