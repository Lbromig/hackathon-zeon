<script setup lang="ts">
// The twelve poses the hero workflow needs, grouped by the step that visits them.
//
// The list comes from the backend, derived from `uncap_aspirate.CHOREOGRAPHY`, so it
// cannot drift from what the workflow actually does: add a waypoint there and it
// appears here as untaught. "Ready" uses the *same* preflight() the orchestrator runs
// before it moves anything, so a green banner here means green there.
import { computed, onMounted } from "vue";
import { useTeach } from "../../composables/useTeach";
import type { RequiredPose } from "../../api/teach";

const {
  required,
  preflight,
  selectedId,
  arms,
  canMove,
  connected,
  refreshReadiness,
  savePose,
  gotoPose,
  select,
} = useTeach();

onMounted(refreshReadiness);

interface Group {
  step: string;
  poses: RequiredPose[];
}

/** Preserve choreography order — the sequence is the information here. */
const groups = computed<Group[]>(() => {
  const out: Group[] = [];
  for (const p of required.value) {
    let g = out.find((x) => x.step === p.step);
    if (!g) out.push((g = { step: p.step, poses: [] }));
    g.poses.push(p);
  }
  return out;
});

const taughtCount = computed(() => required.value.filter((p) => p.taught).length);
const armName = (id: string) => arms.value.find((a) => a.id === id)?.name ?? id;
const isSelected = (p: RequiredPose) => p.device === selectedId.value;

async function capture(p: RequiredPose) {
  // Saves wherever the arm is right now, under the name the workflow expects.
  await savePose(p.name, p.note);
}
</script>

<template>
  <section class="card">
    <div class="flex items-baseline justify-between gap-2">
      <h2 class="card-title">Workflow poses</h2>
      <span class="num text-xs text-deck-400">{{ taughtCount }} / {{ required.length }} taught</span>
    </div>

    <p
      v-if="preflight"
      class="mt-2 border-l-2 px-2 py-1.5 text-xs"
      :class="preflight.ok
        ? 'border-emerald-500 bg-emerald-500/10 text-emerald-300'
        : 'border-amber-500 bg-amber-500/10 text-amber-300'"
    >
      <template v-if="preflight.ok">
        Pre-flight passes — the workflow can run.
      </template>
      <template v-else>
        Not ready: {{ preflight.problems.length }} problem<span v-if="preflight.problems.length !== 1">s</span>.
        <span class="text-deck-400">{{ preflight.problems[0] }}</span>
      </template>
    </p>

    <div v-for="g in groups" :key="g.step" class="mt-3">
      <div class="mb-1 font-mono text-[0.65rem] uppercase tracking-widest text-deck-400">
        {{ g.step }}
      </div>
      <ul class="divide-y divide-deck-700">
        <li v-for="p in g.poses" :key="p.device + p.name" class="flex items-center gap-2 py-1.5">
          <span
            class="w-1.5 shrink-0 self-stretch rounded"
            :class="p.taught ? 'bg-emerald-500' : 'bg-amber-500'"
            :title="p.taught ? 'taught' : 'not taught yet'"
          />
          <div class="min-w-0 flex-1">
            <div class="truncate text-sm text-deck-100">
              {{ p.name }}
              <span class="ml-1 text-xs text-deck-500">{{ armName(p.device) }}</span>
            </div>
            <div v-if="p.note" class="truncate text-xs text-deck-400">{{ p.note }}</div>
          </div>

          <template v-if="isSelected(p)">
            <button
              class="btn btn-sm"
              :disabled="!canMove || !p.taught"
              :title="p.taught ? 'replay this pose' : 'teach it first'"
              @click="gotoPose(p.name)"
            >
              Go
            </button>
            <button
              class="btn btn-sm"
              :class="p.taught ? 'btn-ghost' : 'btn-primary'"
              :disabled="!connected"
              title="save the arm's current position under this name"
              @click="capture(p)"
            >
              {{ p.taught ? "Re-teach" : "Teach here" }}
            </button>
          </template>
          <button v-else class="btn btn-sm btn-ghost" @click="select(p.device)">
            Select {{ armName(p.device) }}
          </button>
        </li>
      </ul>
    </div>

    <p v-if="!required.length" class="mt-3 text-xs text-deck-400">
      No workflow poses required — or the backend has no choreography loaded.
    </p>
    <p v-else class="mt-3 text-xs text-deck-400">
      Hand-guide the arm to each position, then press <em>Teach here</em>. Poses replay from
      saved joint angles, so the arm returns exactly where it physically was.
    </p>
  </section>
</template>
