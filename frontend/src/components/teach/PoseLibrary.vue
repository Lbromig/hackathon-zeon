<script setup lang="ts">
// Teach points: drive the arm somewhere by hand, name it, come back to it later.
// Go-to replays the saved joint angles (the backend's choice) — the arm physically
// reached them, so there is no IK branch to guess at.
//
// This is the *scratch* library. The 15 workflow waypoints live in the checklist above;
// they show up here too (they are ordinary taught poses) but are badged, so "taught six
// scratch points" can never be mistaken for progress against the workflow.
import { computed, ref } from "vue";
import { useTeach } from "../../composables/useTeach";

const { poses, waypoints, connected, canMove, savePose, deletePose, gotoPose } = useTeach();

const name = ref("");
const note = ref("");
const confirmDelete = ref("");

/** The selected arm's own workflow-waypoint names — never the other arm's (R-WP-3). */
const specNames = computed(() => new Set(waypoints.value?.waypoints.map((w) => w.name) ?? []));

/** Typing a name this arm does not own is refused by the backend; say so before the click.
 *  We cannot list the *other* arm's names here without building the very picker R-WP-3
 *  forbids, so the check is "not one of mine, and not free-form either" — which is exactly
 *  what a near-miss of a spec name looks like. */
const looksLikeAnotherArmsWaypoint = computed(() => {
  const n = name.value.trim();
  if (!n || specNames.value.has(n)) return false;
  return /^[A-Z][A-Z0-9_]{3,}$/.test(n);
});

async function save() {
  const trimmed = name.value.trim();
  if (!trimmed) return;
  if (await savePose(trimmed, note.value.trim())) {
    name.value = "";
    note.value = "";
  }
}

function askDelete(poseName: string) {
  if (confirmDelete.value === poseName) {
    confirmDelete.value = "";
    void deletePose(poseName);
  } else {
    confirmDelete.value = poseName;
    window.setTimeout(() => {
      if (confirmDelete.value === poseName) confirmDelete.value = "";
    }, 4000);
  }
}
</script>

<template>
  <section class="card">
    <h2 class="card-title">Taught poses <span class="text-deck-400">— scratch points</span></h2>

    <div class="flex gap-2">
      <input
        v-model="name"
        class="field"
        placeholder="name (e.g. rack_A1_above)"
        @keyup.enter="save"
      />
      <button class="btn btn-primary shrink-0" :disabled="!connected || !name.trim()" @click="save">
        Save here
      </button>
    </div>
    <input v-model="note" class="field mt-2" placeholder="note (optional)" />
    <p v-if="looksLikeAnotherArmsWaypoint" class="mt-2 text-xs text-amber-400">
      <span class="num">{{ name.trim() }}</span> is not a workflow waypoint for this arm. If
      you meant one, teach it from the checklist above — the backend refuses a waypoint this
      arm does not own, and an UPPER_CASE scratch name is easy to mistake for one later.
    </p>

    <ul v-if="poses.length" class="mt-3 divide-y divide-deck-700">
      <li v-for="p in poses" :key="p.name" class="flex items-center gap-2 py-2">
        <div class="min-w-0 flex-1">
          <div class="flex items-baseline gap-2">
            <span class="truncate text-sm font-semibold text-deck-100">{{ p.name }}</span>
            <span
              v-if="specNames.has(p.name)"
              class="shrink-0 rounded border border-sky-500/30 bg-sky-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-sky-300"
              title="a workflow waypoint — manage it from the checklist above"
            >
              waypoint
            </span>
          </div>
          <div class="truncate text-xs text-deck-400">
            <span v-if="p.note">{{ p.note }} · </span>
            <span v-if="p.pose" class="num">
              {{ p.pose.x.toFixed(0) }}, {{ p.pose.y.toFixed(0) }}, {{ p.pose.z.toFixed(0) }} mm
            </span>
            <span v-if="p.joints"> · joints saved</span>
          </div>
        </div>
        <button class="btn btn-sm" :disabled="!canMove" @click="gotoPose(p.name)">Go to</button>
        <button
          class="btn btn-sm"
          :class="confirmDelete === p.name ? 'btn-danger' : 'btn-ghost'"
          @click="askDelete(p.name)"
        >
          {{ confirmDelete === p.name ? "Sure?" : "✕" }}
        </button>
      </li>
    </ul>
    <p v-else class="mt-3 text-xs text-deck-400">
      Nothing taught yet. Jog the arm where you want it, then save it under a name — the library
      survives a backend restart.
    </p>
  </section>
</template>
