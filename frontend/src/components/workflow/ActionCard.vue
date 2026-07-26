<script setup lang="ts">
// One row of the plan chain, expandable (R-UI-2/3/4/5/8).
//
// Collapsed it answers: what state is this in, where is it in the plan, what kind is it, the
// one fact that matters for that kind — and **who put it here**. `origin` is a badge rather
// than a detail, because "was this in the plan, did the loop materialize it, or did somebody
// inject it?" is the first question when a run does something unexpected, and the three cases
// look identical otherwise.
//
// Expanded it answers: the full typed outputs, the artifacts, and the log records this action
// produced — fetched by `aid` from `GET /api/logs`, not by index. `aid` is the stable identity;
// an injection renumbers every index after it, and a fetch keyed on the display number would
// quietly show a different action's records after one.
import { computed, ref, watch } from "vue";
import { fetchActionLogs, type LogRecord } from "../../api/logs";
import { useEngine, type ChainRow } from "../../stores/engine";
import ActionOutputs from "./ActionOutputs.vue";
import ActionSummary from "./ActionSummary.vue";
import StateGlyph from "./StateGlyph.vue";

const props = defineProps<{
  item: ChainRow;
  /** Controlled by the parent, so only one place owns which rows are open. */
  expanded?: boolean;
  /** The left column's "selected action" rendering: always open, no toggle chrome. */
  detail?: boolean;
  selected?: boolean;
}>();

const emit = defineEmits<{ (e: "toggle"): void }>();

const { state } = useEngine();

const open = computed(() => props.detail || !!props.expanded);

const ORIGIN: Record<string, { glyph: string; word: string; cls: string; title: string }> = {
  plan: {
    glyph: "▪", word: "plan", cls: "border-deck-600 text-deck-400",
    title: "authored in the plan",
  },
  inject: {
    glyph: "⤵", word: "inject", cls: "border-amber-500 text-amber-300",
    title: "injected into this run after it started — an operator or agent addition, not part "
      + "of the authored plan",
  },
  expand: {
    glyph: "⟳", word: "expand", cls: "border-blue-500 text-blue-300",
    title: "materialized from a loop body, so this iteration's frames, overlays and solved "
      + "offset are each inspectable on their own row",
  },
};

const origin = computed(() => ORIGIN[props.item.row.origin ?? "plan"] ?? {
  glyph: "?", word: props.item.row.origin ?? "unknown",
  cls: "border-red-500 text-red-300",
  title: "an origin this build does not know — shown rather than hidden",
});

const result = computed(() => props.item.result);

const duration = computed(() => {
  const ms = result.value?.duration_ms;
  if (typeof ms !== "number" || !Number.isFinite(ms)) return "";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
});

/** The `action_log` lines streamed on the socket — what is visible before the file is read. */
const liveLines = computed(() => state.liveLogs[props.item.aid] ?? []);

const records = ref<LogRecord[]>([]);
const logError = ref("");
const logLoading = ref(false);
const fetched = ref(false);

async function loadLogs(force = false): Promise<void> {
  if (logLoading.value) return;
  if (fetched.value && !force) return;
  // `aid` alone would match the same action of a *previous* run of the same plan, because aids
  // restart per run. Without a run id there is nothing honest to fetch, so the socket's own lines
  // are all this row shows.
  if (!state.runId) return;
  logLoading.value = true;
  logError.value = "";
  try {
    const page = await fetchActionLogs(state.runId, props.item.aid);
    records.value = page.records;
    fetched.value = true;
    if (page.reset) {
      logError.value = "the log rotated — records before this page are gone, not missing";
    }
  } catch (e) {
    logError.value = e instanceof Error ? e.message : String(e);
  } finally {
    logLoading.value = false;
  }
}

// Fetch on first open, and again when the action finishes while it is open: the interesting
// records are the ones written between "running" and the result.
watch(open, (isOpen) => {
  if (isOpen) void loadLogs();
}, { immediate: true });
watch(() => props.item.state, (next) => {
  if (open.value && (next === "complete" || next === "failed" || next === "aborted")) {
    void loadLogs(true);
  }
});
// A different action in the same slot, or a different run: both invalidate what was fetched.
watch(() => [props.item.aid, state.runId], () => {
  records.value = [];
  fetched.value = false;
  if (open.value) void loadLogs(true);
});

const logCount = computed(() => (fetched.value ? records.value.length : liveLines.value.length));

const levelTone = (level: unknown) => {
  const l = String(level ?? "").toUpperCase();
  if (l === "ERROR" || l === "CRITICAL") return "text-red-400";
  if (l === "WARNING") return "text-amber-300";
  if (l === "DEBUG") return "text-deck-400";
  return "text-deck-200";
};

const shortTs = (ts: unknown) => String(ts ?? "").replace("T", " ").slice(11, 23);
const rawRecord = (r: LogRecord) => JSON.stringify(r, null, 2);
const inputsJson = computed(() => JSON.stringify(result.value?.inputs ?? {}, null, 2));
const paramsJson = computed(() => JSON.stringify(props.item.row.params ?? {}, null, 2));
</script>

<template>
  <div
    class="rounded-lg border transition-colors"
    :class="[
      item.state === 'failed' ? 'border-red-500/70 bg-red-950/20'
      : item.state === 'running' ? 'border-blue-500/70 bg-blue-950/20'
      : selected ? 'border-blue-400/60 bg-deck-700/40'
      : 'border-deck-700 bg-deck-800/60',
      item.notReached ? 'opacity-60' : '',
    ]"
  >
    <!-- One control for the whole header: keyboard-reachable, and the accessible name carries
         the index, the state word and the kind rather than only a glyph. -->
    <component
      :is="detail ? 'div' : 'button'"
      :type="detail ? undefined : 'button'"
      class="flex w-full flex-wrap items-center gap-x-2 gap-y-1 px-2 py-1.5 text-left"
      :class="detail ? '' : 'hover:bg-deck-700/50'"
      :aria-expanded="detail ? undefined : open"
      @click="detail ? undefined : emit('toggle')"
    >
      <StateGlyph :state="item.state" :not-reached="item.notReached" />

      <span
        class="num w-16 shrink-0 text-xs"
        :class="item.depth ? 'text-deck-400' : 'text-deck-200'"
        :title="item.depth
          ? `loop #${item.row.parent_aid != null ? item.row.parent_aid : '?'} · iteration ${item.row.iteration} · row ${item.display.split('.').pop()} — flat index #${item.row.index}, aid ${item.aid}`
          : `plan index #${item.row.index}, aid ${item.aid}`"
      >
        {{ item.display }}
      </span>

      <!-- The cursor is where the run *is*. After a failure inside the loop that is the failed
           row itself, so this never says "next". -->
      <span
        v-if="item.isCursor"
        class="shrink-0 rounded bg-blue-600/30 px-1 text-[10px] font-bold uppercase text-blue-200"
        title="the run is here — Resume continues from this row, and an injection after it becomes the next step"
      >
        <span aria-hidden="true">▸</span> here
      </span>

      <span class="min-w-0 shrink-0 text-xs font-semibold text-deck-100">
        {{ item.row.label || item.row.kind }}
      </span>
      <span v-if="item.row.device" class="num shrink-0 text-[11px] text-deck-400">
        {{ item.row.device }}
      </span>

      <ActionSummary :item="item" class="min-w-0 flex-1" />

      <span
        class="shrink-0 rounded border px-1 text-[10px] font-semibold uppercase"
        :class="origin.cls"
        :title="origin.title"
      >
        <span aria-hidden="true">{{ origin.glyph }}</span> {{ origin.word }}
      </span>
      <span v-if="duration" class="num shrink-0 text-[11px] text-deck-400">{{ duration }}</span>
      <span v-if="!detail" class="shrink-0 text-xs text-deck-400" aria-hidden="true">
        {{ open ? "▾" : "▸" }}
      </span>
    </component>

    <div v-if="open" class="space-y-3 border-t border-deck-700 px-3 py-2">
      <div class="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-deck-400">
        <span class="num">aid {{ item.aid }}</span>
        <span class="num">kind {{ item.row.kind }}</span>
        <span v-if="item.row.speed" class="num">speed {{ item.row.speed }}</span>
        <span v-if="item.row.iteration != null" class="num">iteration {{ item.row.iteration }}</span>
        <span v-if="result?.attempt && result.attempt > 1" class="text-amber-300">
          attempt {{ result.attempt }} — each retry is recorded on its own, never replacing the first
        </span>
        <span v-if="result?.simulated === true" class="text-violet-300">simulated</span>
        <span v-else-if="result?.simulated === false" class="text-emerald-300">real hardware</span>
        <span v-if="result?.started_at" class="num">started {{ shortTs(result.started_at) }}</span>
        <span v-if="result?.finished_at" class="num">finished {{ shortTs(result.finished_at) }}</span>
      </div>

      <div
        v-if="result?.error"
        class="rounded-lg border border-red-500/60 bg-red-950/40 px-2 py-1.5 text-xs"
      >
        <p class="font-semibold text-red-300">
          <span aria-hidden="true">✗</span> {{ result.error.type }}
          <span v-if="result.error.retriable" class="text-amber-300">· retriable</span>
          <span v-if="result.error.device" class="num text-deck-300">· {{ result.error.device }}</span>
        </p>
        <p class="mt-0.5 whitespace-pre-wrap text-red-100">{{ result.error.message }}</p>
      </div>

      <ul v-if="result?.warnings?.length" class="space-y-0.5">
        <li v-for="(w, i) in result.warnings" :key="i" class="text-xs text-amber-200">
          <span aria-hidden="true">⚠</span>
          <span class="num rounded bg-deck-900 px-1">{{ w.code }}</span>
          {{ w.message }}
          <span v-if="w.device" class="text-deck-400">· {{ w.device }}</span>
        </li>
      </ul>

      <ActionOutputs
        :outputs="result?.outputs ?? null"
        :action-kind="item.row.kind"
        :artifacts="result?.artifacts ?? []"
        :run-id="state.runId"
      />

      <details class="rounded-lg border border-deck-700">
        <summary class="cursor-pointer px-2 py-1 text-[11px] text-deck-400">
          inputs as resolved (slots already read) and the authored params
        </summary>
        <div class="space-y-2 px-2 pb-2">
          <pre class="num overflow-x-auto text-[11px] text-deck-200">{{ inputsJson }}</pre>
          <pre class="num overflow-x-auto text-[11px] text-deck-400">{{ paramsJson }}</pre>
        </div>
      </details>

      <!-- The records this action produced. Attributed by `aid` (R-ENG-7/R-UI-8), so a handler
           cannot emit an unattributable line and this list cannot show a neighbour's. -->
      <section>
        <div class="flex items-center gap-2">
          <h4 class="text-[11px] font-semibold uppercase tracking-wide text-deck-400">
            logs ({{ logCount }})
          </h4>
          <button
            class="btn btn-sm btn-ghost"
            :disabled="logLoading"
            title="re-read GET /api/logs?run_id=&aid= for this action"
            @click="loadLogs(true)"
          >
            {{ logLoading ? "…" : "↻" }}
          </button>
          <span v-if="!state.runId" class="text-[11px] text-deck-400">
            no run id yet — records are scoped to a run so a re-run does not show the last one's
          </span>
        </div>

        <p v-if="logError" class="mt-1 text-[11px] text-amber-300">{{ logError }}</p>

        <div
          v-if="records.length"
          class="mt-1 max-h-60 overflow-auto rounded-lg border border-deck-700 bg-deck-900"
        >
          <table class="w-full border-collapse text-left text-[11px]">
            <tbody>
              <tr v-for="(r, i) in records" :key="i" class="border-t border-deck-700/50 align-top">
                <td class="num whitespace-nowrap px-1.5 py-0.5 text-deck-400">{{ shortTs(r.ts) }}</td>
                <td class="px-1.5 py-0.5 font-semibold" :class="levelTone(r.level)">
                  {{ r.level ?? "" }}
                </td>
                <td class="px-1.5 py-0.5" :class="levelTone(r.level)" :title="rawRecord(r)">
                  {{ r.msg ?? r.event ?? "" }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- The socket's own lines, kept as the fallback: they are all there is while the
             action is still running, and all there is at all if the log file is unreadable. -->
        <ul v-else-if="liveLines.length" class="mt-1 space-y-0.5">
          <li v-for="line in liveLines" :key="line.seq" class="num text-[11px]" :class="levelTone(line.level)">
            {{ line.level }} · {{ line.msg }}
          </li>
        </ul>
        <p v-else-if="!logLoading" class="mt-1 text-[11px] text-deck-400">
          no records for this action
          <span v-if="!fetched">— the log file has not been read yet</span>
        </p>
      </section>
    </div>
  </div>
</template>
