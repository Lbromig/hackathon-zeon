<script setup lang="ts">
// The plan chain: every action, past and future, with its index and its state (R-UI-1/2/3/5).
//
// The two things this component is *for*:
//
//  * **The loop reads as a loop.** Materialized rows are indented under their `control.loop`
//    and labelled with the iteration, so the 24 rows of a three-iteration servo loop are three
//    passes of eight rather than 24 anonymous steps. Eight body rows repeated twelve times
//    share eight kinds; without the iteration in the label, "snapshot handover_cam" identifies
//    nothing. Each iteration gets a divider carrying what the loop measured on that pass —
//    which is how a *stalled* loop looks different from a slow one.
//  * **The chain never loses a completed action's results.** Rows are keyed on `aid`, so an
//    injection that renumbers every index after it re-labels rows without remounting them, and
//    an expanded row's outputs and fetched logs survive the renumber (D4).
import { computed, ref, watch } from "vue";
import { loopIterationsOf, useEngine, type ChainRow } from "../../stores/engine";
import ActionCard from "./ActionCard.vue";

const props = defineProps<{ selectedAid?: number | null }>();
const emit = defineEmits<{ (e: "select", aid: number): void }>();

const { state, chain, counts, cursorRow } = useEngine();

const expanded = ref<Set<number>>(new Set());
const follow = ref(true);
const listEl = ref<HTMLElement | null>(null);

function toggle(item: ChainRow): void {
  const next = new Set(expanded.value);
  if (next.has(item.aid)) next.delete(item.aid);
  else next.add(item.aid);
  expanded.value = next;
  emit("select", item.aid);
}

function collapseAll(): void {
  expanded.value = new Set();
}

/** One divider per loop iteration, carrying what that pass measured. */
interface Divider {
  parentDisplay: string;
  iteration: number;
  magnitude: number | null;
  threshold: number | null;
  improving: boolean | undefined;
}

const rows = computed(() => {
  const items = chain.value;
  const displayOfIndex = new Map<number, string>();
  for (const item of items) if (!item.depth) displayOfIndex.set(item.row.index, item.display);

  return items.map((item, at) => {
    const previous = at > 0 ? items[at - 1] : null;
    let divider: Divider | null = null;
    const isNewIteration = item.depth > 0
      && (!previous
        || previous.depth === 0
        || previous.row.parent_aid !== item.row.parent_aid
        || previous.row.iteration !== item.row.iteration);
    if (isNewIteration && item.row.parent_aid != null && item.row.iteration != null) {
      const parentAid = item.row.parent_aid;
      const iteration = item.row.iteration;
      const parent = items.find((r) => r.aid === parentAid);
      const measured = loopIterationsOf(parentAid).find((i) => i.iteration === iteration);
      divider = {
        parentDisplay: parent?.display ?? displayOfIndex.get(parentAid) ?? String(parentAid),
        iteration,
        magnitude: typeof measured?.magnitude_mm === "number" ? measured.magnitude_mm : null,
        threshold: typeof measured?.threshold_mm === "number" ? measured.threshold_mm : null,
        improving: measured?.improving,
      };
    }
    return { item, divider };
  });
});

/** Keep the row the run is on in view, without fighting an operator who scrolled away. */
watch(() => cursorRow.value?.aid, (aid) => {
  if (!follow.value || aid == null) return;
  requestAnimationFrame(() => {
    listEl.value
      ?.querySelector(`[data-aid="${aid}"]`)
      ?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
});

const mm = (v: number) => `${v.toFixed(2)} mm`;
</script>

<template>
  <section class="card">
    <div class="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1">
      <h2 class="card-title mb-0">Plan</h2>
      <span v-if="state.name" class="num text-xs text-deck-300">{{ state.name }}</span>
      <span v-if="counts.total" class="num text-xs text-deck-200">
        {{ counts.complete }}/{{ counts.total }} done
      </span>
      <span v-if="counts.running" class="num text-xs text-blue-300">
        {{ counts.running }} running
      </span>
      <span v-if="counts.failed" class="num text-xs text-red-400">
        {{ counts.failed }} failed / aborted
      </span>
      <span
        v-if="state.revision"
        class="num text-xs text-deck-400"
        title="the plan has been mutated this many times — every loop iteration and every injection bumps it"
      >
        rev {{ state.revision }}
      </span>

      <label class="ml-auto flex items-center gap-1.5 text-xs text-deck-300">
        <input v-model="follow" type="checkbox" />
        follow the run
      </label>
      <button class="btn btn-sm btn-ghost" :disabled="!expanded.size" @click="collapseAll">
        collapse all ({{ expanded.size }})
      </button>
    </div>

    <p
      v-if="state.api === 'absent'"
      class="rounded-lg border border-red-500/60 bg-red-950/30 px-3 py-2 text-xs text-red-200"
    >
      <span aria-hidden="true">✗</span>
      <strong>engine API unavailable.</strong>
      <span class="num">/api/engine/*</span> is not answering, so there is no plan to show — not
      an empty plan. {{ state.apiError }}
    </p>

    <p v-else-if="!rows.length" class="text-xs text-deck-400">
      No plan loaded. Load <span class="num">handover</span> or
      <span class="num">startup</span> above; the chain then shows every action, past and future,
      before anything moves.
    </p>

    <div v-else ref="listEl" class="max-h-[68vh] space-y-1 overflow-auto pr-1">
      <template v-for="entry in rows" :key="entry.item.aid">
        <!-- Iteration divider. `improving` and the measured magnitude are the two facts that
             separate "converging slowly" from "stalled", and the loop's own row only ever shows
             the latest one. -->
        <div
          v-if="entry.divider"
          class="mt-2 flex flex-wrap items-center gap-x-2 border-t border-dashed border-blue-500/40 pt-1.5 pl-6 text-[11px]"
        >
          <span class="font-semibold text-blue-300">
            loop #{{ entry.divider.parentDisplay }} · iteration {{ entry.divider.iteration }}
          </span>
          <span v-if="entry.divider.magnitude !== null" class="num text-deck-200">
            measured {{ mm(entry.divider.magnitude) }}
            <span v-if="entry.divider.threshold !== null" class="text-deck-400">
              (converges at ≤ {{ mm(entry.divider.threshold) }})
            </span>
          </span>
          <span
            v-if="entry.divider.improving === false"
            class="text-amber-300"
            title="this pass did not improve on the last one — no_progress_abort counts these, and three in a row stall the loop"
          >
            <span aria-hidden="true">↑</span> no improvement
          </span>
          <span v-else-if="entry.divider.improving === true" class="text-emerald-400">
            <span aria-hidden="true">↓</span> improving
          </span>
        </div>

        <div :data-aid="entry.item.aid" :class="entry.item.depth ? 'ml-6' : ''">
          <ActionCard
            :item="entry.item"
            :expanded="expanded.has(entry.item.aid)"
            :selected="props.selectedAid === entry.item.aid"
            @toggle="toggle(entry.item)"
          />
        </div>
      </template>
    </div>

    <p v-if="rows.length" class="mt-2 text-[11px] text-deck-400">
      Rows are keyed on <span class="num">aid</span>, not on the index: an injection renumbers
      the indices after it, and a completed action's outputs must not vanish because its number
      changed.
    </p>
  </section>
</template>
