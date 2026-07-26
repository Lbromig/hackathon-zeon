<script setup lang="ts">
// The Workflow tab (R-UI-1): what is happening now, and the plan ahead.
//
// Layout is §3.1's: readiness and the selected action down the left, controls and the plan chain
// down the right. The reality banner is **not** repeated here — `App.vue` renders it above the tab
// strip on every tab, which is the stronger version of the same rule: a banner that only exists on
// this tab is a banner the operator can navigate away from (R-SIM-8/D29). If it is ever removed
// from the shell, it has to come back here.
//
// The store is connected once, in `App.vue`, and keeps streaming while the operator is on the
// Cameras tab — so this view mounts and unmounts freely without dropping events or forcing a
// resync.
import { computed, ref } from "vue";
import { useEngine } from "../stores/engine";
import ActionCard from "../components/workflow/ActionCard.vue";
import InjectChat from "../components/workflow/InjectChat.vue";
import PlanChain from "../components/workflow/PlanChain.vue";
import ReadinessPanel from "../components/workflow/ReadinessPanel.vue";
import RunControls from "../components/workflow/RunControls.vue";

const { state, chain, counts, failedRow, cursorRow } = useEngine();

const selectedAid = ref<number | null>(null);
const injectOpen = ref(false);

/**
 * What the left column shows.
 *
 * With nothing selected it follows the run: the failed row first — that is what an operator is
 * looking for the moment anything goes wrong — then the row the cursor is on. An explicit
 * selection wins and stays, because reading a completed action's outputs while the run carries on
 * is the other half of what this panel is for.
 */
const detailItem = computed(() => {
  if (selectedAid.value !== null) {
    const found = chain.value.find((item) => item.aid === selectedAid.value);
    if (found) return found;
  }
  return failedRow.value ?? cursorRow.value ?? null;
});

const following = computed(() => selectedAid.value === null);

const finishedText = computed(() => {
  const done = state.finished;
  if (!done) return "";
  const seconds = done.durationMs ? `${(done.durationMs / 1000).toFixed(1)} s` : "unknown";
  return `${done.status} · ${done.completed} completed · ${done.failed} failed · ${seconds}`;
});
</script>

<template>
  <div class="space-y-4">
    <!-- Degradation is rendered, not logged. The engine routes are a parallel slice, and a tab
         that answers a missing API with a blank column teaches the operator to distrust it. -->
    <div
      v-if="state.api === 'absent'"
      class="rounded-xl border-2 border-red-500 bg-red-950/40 px-4 py-3"
      role="alert"
    >
      <p class="text-sm font-bold text-red-200">
        <span aria-hidden="true">✗</span> engine API unavailable
      </p>
      <p class="mt-1 text-xs text-red-100">
        <span class="num">/api/engine/*</span> is not answering, so there is no run to show — which
        is a different thing from a run with nothing in it. Nothing on this tab is stale data: it is
        no data. The other three tabs are unaffected.
      </p>
      <p v-if="state.apiError" class="num mt-1 text-[11px] text-red-300">{{ state.apiError }}</p>
    </div>

    <p
      v-if="finishedText"
      class="rounded-lg border border-deck-600 bg-deck-800 px-3 py-2 text-xs text-deck-200"
    >
      run finished — <span class="num">{{ finishedText }}</span>
      <span v-if="counts.failed" class="text-red-300">
        · the failed rows keep their outputs and their logs; expand them in the chain
      </span>
    </p>

    <div class="grid gap-4 xl:grid-cols-[minmax(22rem,28rem)_1fr]">
      <!-- left: readiness, then the action under the microscope -->
      <div class="space-y-4">
        <ReadinessPanel />

        <section class="card">
          <div class="mb-2 flex flex-wrap items-center gap-2">
            <h2 class="card-title mb-0">Selected action</h2>
            <span
              v-if="following"
              class="text-[11px] text-deck-400"
              title="with nothing selected this follows the run: the failed row if there is one, otherwise the row the cursor is on"
            >
              following the run
            </span>
            <button v-else class="btn btn-sm btn-ghost ml-auto" @click="selectedAid = null">
              follow the run again
            </button>
          </div>

          <ActionCard v-if="detailItem" :key="detailItem.aid" :item="detailItem" detail selected />
          <p v-else class="text-xs text-deck-400">
            Nothing to show yet. Click any row in the plan to inspect its outputs, its artifacts and
            the log records it produced.
          </p>
        </section>
      </div>

      <!-- right: controls, the inject panel, the chain -->
      <div class="space-y-4">
        <RunControls :inject-open="injectOpen" @inject="injectOpen = !injectOpen" />
        <InjectChat v-if="injectOpen" @close="injectOpen = false" />
        <PlanChain :selected-aid="selectedAid" @select="selectedAid = $event" />
      </div>
    </div>
  </div>
</template>
