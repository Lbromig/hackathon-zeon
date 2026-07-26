<script setup lang="ts">
// The logs view (R-UI-12 / R-LOG-4): the newest page, then cursor-polled follow.
//
// Polling, not a websocket — that is the endpoint's design. `reset` in a response is not an
// error: the file rotated under us, so records between the old cursor and this page are gone,
// and saying "log rotated" is the only honest rendering of a gap.
import { computed, onMounted, onUnmounted, ref } from "vue";
import { LOG_LEVELS, fetchLogs, type LogRecord } from "../api/logs";
import { useEngine } from "../stores/engine";

const { state } = useEngine();

const records = ref<LogRecord[]>([]);
const cursor = ref("");
const rotated = ref(false);
const error = ref("");
const path = ref("");
const loading = ref(false);
const follow = ref(true);
const expanded = ref<number | null>(null);

const level = ref("");
const contains = ref("");
const logger = ref("");
const device = ref("");
const eventFilter = ref("");
const thisRunOnly = ref(false);
const limit = ref(300);

// A hard cap on what the browser holds: a 12-iteration run writes thousands of records and a
// follow that never forgets is a tab that dies during the run it was opened to watch.
const MAX_HELD = 4000;
const POLL_MS = 2000;

const query = computed(() => ({
  limit: limit.value,
  level: level.value || undefined,
  contains: contains.value || undefined,
  logger: logger.value || undefined,
  device: device.value || undefined,
  event: eventFilter.value || undefined,
  run_id: thisRunOnly.value && state.runId ? state.runId : undefined,
}));

async function reload() {
  loading.value = true;
  error.value = "";
  try {
    const page = await fetchLogs(query.value);
    records.value = page.records;
    cursor.value = page.cursor;
    rotated.value = false;
    path.value = page.path;
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    loading.value = false;
  }
}

async function poll() {
  if (!follow.value || loading.value || !cursor.value) return;
  try {
    const page = await fetchLogs({ ...query.value, cursor: cursor.value });
    cursor.value = page.cursor;
    if (page.reset) rotated.value = true;
    if (page.records.length) {
      records.value = records.value.concat(page.records).slice(-MAX_HELD);
    }
    path.value = page.path || path.value;
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  }
}

let timer: number | null = null;
onMounted(() => {
  void reload();
  timer = window.setInterval(poll, POLL_MS);
});
onUnmounted(() => {
  if (timer !== null) window.clearInterval(timer);
});

const tone = (record: LogRecord) => {
  const l = String(record.level ?? "").toUpperCase();
  if (l === "ERROR" || l === "CRITICAL") return "text-red-400";
  if (l === "WARNING") return "text-amber-300";
  if (l === "DEBUG") return "text-deck-400";
  return "text-deck-100";
};

const shortTs = (ts: unknown) => String(ts ?? "").replace("T", " ").slice(0, 23);
const raw = (record: LogRecord) => JSON.stringify(record, null, 2);
</script>

<template>
  <section class="card">
    <div class="flex flex-wrap items-end gap-2">
      <label class="text-xs text-deck-400">
        min level
        <select v-model="level" class="field mt-1 w-32" @change="reload">
          <option value="">all</option>
          <option v-for="l in LOG_LEVELS" :key="l" :value="l">{{ l }}</option>
        </select>
      </label>
      <label class="text-xs text-deck-400">
        contains
        <input
          v-model="contains"
          class="field mt-1 w-48"
          placeholder="substring over the record"
          @keyup.enter="reload"
        />
      </label>
      <label class="text-xs text-deck-400">
        logger prefix
        <input v-model="logger" class="field mt-1 w-40" placeholder="engine.handlers" @keyup.enter="reload" />
      </label>
      <label class="text-xs text-deck-400">
        device
        <input v-model="device" class="field mt-1 w-28" @keyup.enter="reload" />
      </label>
      <label class="text-xs text-deck-400">
        event
        <input v-model="eventFilter" class="field mt-1 w-32" placeholder="action_output" @keyup.enter="reload" />
      </label>
      <label class="text-xs text-deck-400">
        limit
        <input v-model.number="limit" type="number" min="1" max="2000" class="field mt-1 w-20" @change="reload" />
      </label>
      <label class="flex items-center gap-1.5 text-xs text-deck-200">
        <input v-model="thisRunOnly" type="checkbox" :disabled="!state.runId" @change="reload" />
        this run only
      </label>
      <label class="flex items-center gap-1.5 text-xs text-deck-200">
        <input v-model="follow" type="checkbox" />
        follow
      </label>
      <button class="btn btn-sm" :disabled="loading" @click="reload">
        {{ loading ? "Loading…" : "Reload" }}
      </button>
    </div>

    <p v-if="error" class="mt-3 rounded-lg border border-red-500/60 bg-red-950/40 px-3 py-2 text-xs text-red-300">
      {{ error }}
    </p>
    <p
      v-if="rotated"
      class="mt-3 rounded-lg border border-amber-500/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-200"
    >
      ⚠ log rotated — records between the previous page and this one are gone. Reload for the
      newest page.
    </p>

    <div class="mt-3 max-h-[70vh] overflow-auto rounded-lg border border-deck-600">
      <table class="w-full border-collapse text-left text-xs">
        <thead class="sticky top-0 bg-deck-900 text-deck-400">
          <tr>
            <th class="px-2 py-1.5 font-semibold">time</th>
            <th class="px-2 py-1.5 font-semibold">level</th>
            <th class="px-2 py-1.5 font-semibold">logger</th>
            <th class="px-2 py-1.5 font-semibold">aid</th>
            <th class="px-2 py-1.5 font-semibold">message</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="(r, i) in records" :key="i">
            <tr
              class="cursor-pointer border-t border-deck-700/60 align-top hover:bg-deck-700/40"
              tabindex="0"
              @click="expanded = expanded === i ? null : i"
              @keyup.enter="expanded = expanded === i ? null : i"
            >
              <td class="num whitespace-nowrap px-2 py-1 text-deck-400">{{ shortTs(r.ts) }}</td>
              <td class="px-2 py-1 font-semibold" :class="tone(r)">{{ r.level ?? "" }}</td>
              <td class="px-2 py-1 text-deck-400">{{ r.logger ?? "" }}</td>
              <td class="num px-2 py-1 text-deck-400">{{ r.aid ?? "" }}</td>
              <td class="px-2 py-1" :class="tone(r)">{{ r.msg ?? "" }}</td>
            </tr>
            <tr v-if="expanded === i" class="border-t border-deck-700/60 bg-deck-900">
              <td colspan="5" class="px-2 py-2">
                <pre class="num overflow-x-auto text-[11px] text-deck-200">{{ raw(r) }}</pre>
              </td>
            </tr>
          </template>
          <tr v-if="!records.length && !loading">
            <td colspan="5" class="px-2 py-6 text-center text-deck-400">
              No records match. The log file is <span class="num">{{ path || "unknown" }}</span>.
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <p class="mt-2 text-xs text-deck-400">
      {{ records.length }} records held · file <span class="num">{{ path || "—" }}</span>
    </p>
  </section>
</template>
