<script setup lang="ts">
// Teach points: drive the arm somewhere by hand, name it, come back to it later.
// Go-to replays the saved joint angles (the backend's choice) — the arm physically
// reached them, so there is no IK branch to guess at.
import { ref } from "vue";
import { useTeach } from "../../composables/useTeach";

const { poses, connected, canMove, savePose, deletePose, gotoPose } = useTeach();

const name = ref("");
const note = ref("");
const confirmDelete = ref("");

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
    <h2 class="card-title">Taught poses</h2>

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

    <ul v-if="poses.length" class="mt-3 divide-y divide-deck-700">
      <li v-for="p in poses" :key="p.name" class="flex items-center gap-2 py-2">
        <div class="min-w-0 flex-1">
          <div class="truncate text-sm font-semibold text-deck-100">{{ p.name }}</div>
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
