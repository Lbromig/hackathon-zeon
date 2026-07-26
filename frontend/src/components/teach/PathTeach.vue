<script setup lang="ts">
// Teach a travel route by walking the arm along it.
//
// Press record, hand-guide the arm from where it starts to where it should end up, press
// stop. The backend samples joints at 10 Hz while you move — server-side, because a
// background browser tab gets throttled and would shred the middle of the recording —
// then simplifies the result: a deadband drops the jitter of a resting hand, and
// Ramer-Douglas-Peucker drops points that sit on the straight line between neighbours.
// A long sweep becomes a handful of waypoints that keep the corners.
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useTeach } from "../../composables/useTeach";

const {
  paths, recording, state, canMove, connected,
  refreshPaths, startRecording, stopRecording, deletePath, replayPath,
} = useTeach();

const name = ref("");
const note = ref("");
const confirmDelete = ref("");
let ticker: number | undefined;

const isRecording = computed(() => recording.value?.recording ?? false);
const activeName = computed(() => recording.value?.name ?? "");
const canStart = computed(() => connected.value && !isRecording.value && !!name.value.trim());

onMounted(() => {
  void refreshPaths();
  // While recording, poll a little faster so the sample counter actually moves.
  ticker = window.setInterval(() => {
    if (isRecording.value) void refreshPaths();
  }, 1000);
});
onUnmounted(() => ticker !== undefined && window.clearInterval(ticker));

async function start() {
  if (await startRecording(name.value.trim())) note.value = "";
}

async function stop() {
  if (await stopRecording(activeName.value, note.value.trim())) {
    name.value = "";
    note.value = "";
  }
}

function askDelete(pathName: string) {
  if (confirmDelete.value === pathName) {
    confirmDelete.value = "";
    void deletePath(pathName);
  } else {
    confirmDelete.value = pathName;
    window.setTimeout(() => {
      if (confirmDelete.value === pathName) confirmDelete.value = "";
    }, 4000);
  }
}
</script>

<template>
  <section class="card">
    <h2 class="card-title">Travel paths</h2>

    <div v-if="!isRecording" class="flex gap-2">
      <input
        v-model="name"
        class="field"
        placeholder="name (e.g. tube_to_ot_handover)"
        @keyup.enter="canStart && start()"
      />
      <button class="btn btn-primary shrink-0" :disabled="!canStart" @click="start">
        ● Record
      </button>
    </div>

    <div v-else class="rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2">
      <div class="flex items-center gap-2">
        <span class="h-2.5 w-2.5 animate-pulse rounded-full bg-red-500" />
        <span class="text-sm font-semibold text-red-200">
          Recording “{{ activeName }}”
        </span>
        <span class="num text-xs text-deck-300">
          {{ recording?.samples ?? 0 }} samples · {{ (recording?.duration_s ?? 0).toFixed(0) }}s
        </span>
        <button class="btn btn-sm ml-auto" @click="stop">■ Stop &amp; save</button>
      </div>
      <p class="mt-1.5 text-xs text-red-200/80">
        Hand-guiding is on — walk the arm along the route it should take, then stop.
        Support the arm: it holds against gravity using the configured payload.
      </p>
      <input v-model="note" class="field mt-2" placeholder="note (optional)" />
    </div>

    <ul v-if="paths.length" class="mt-3 divide-y divide-deck-700">
      <li v-for="p in paths" :key="p.name" class="flex items-center gap-2 py-2">
        <div class="min-w-0 flex-1">
          <div class="truncate text-sm font-semibold text-deck-100">{{ p.name }}</div>
          <div class="truncate text-xs text-deck-400">
            <span v-if="p.note">{{ p.note }} · </span>
            <span class="num">{{ p.waypoints.length }}</span> waypoints ·
            <span class="num">{{ p.length_deg.toFixed(0) }}</span>° travel
            <span v-if="p.raw_samples" class="text-deck-500">
              (from {{ p.raw_samples }} samples)
            </span>
          </div>
        </div>
        <button
          class="btn btn-sm"
          :disabled="!canMove || isRecording"
          title="drive the arm along this route"
          @click="replayPath(p.name)"
        >
          Replay
        </button>
        <button
          class="btn btn-sm btn-ghost"
          :disabled="!canMove || isRecording"
          title="drive it backwards — the return leg"
          @click="replayPath(p.name, true)"
        >
          ↩ Reverse
        </button>
        <button
          class="btn btn-sm"
          :class="confirmDelete === p.name ? 'btn-danger' : 'btn-ghost'"
          @click="askDelete(p.name)"
        >
          {{ confirmDelete === p.name ? "Sure?" : "✕" }}
        </button>
      </li>
    </ul>
    <p v-else-if="!isRecording" class="mt-3 text-xs text-deck-400">
      No paths yet. Name a route, press Record, and physically walk the arm along it —
      for example from the tube approach round to the OT handover approach.
    </p>

    <p v-if="state?.free_drive && !isRecording" class="mt-3 text-xs text-sky-300">
      Hand-guiding is still on — switch it off before replaying a path.
    </p>
  </section>
</template>
