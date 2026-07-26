<script setup lang="ts">
import { onMounted, onUnmounted, ref, computed } from "vue";
import {
  getCameraPreflight,
  takeSnapshot,
  listSnapshots,
  type CameraPreflightResult,
  type CameraSnapshot,
} from "../api/client";

const preflight = ref<CameraPreflightResult | null>(null);
const snaps = ref<CameraSnapshot[]>([]);
const busy = ref(false);
const label = ref("");
const err = ref("");
let timer: number | undefined;

// Snapshot is gated on this. Live video is not rendered here on purpose: the
// fleet already streams through InstrumentPanel and api/cameras.py, and a second
// reader on the same device is the one that fails. This panel answers "is the
// sensor trustworthy", which is the question that has no other home.
const live = computed(() => preflight.value?.usable === true);

async function refresh() {
  try {
    preflight.value = await getCameraPreflight();
    err.value = "";
  } catch (e) {
    err.value = String(e);
  }
}

async function snapshot() {
  busy.value = true;
  try {
    await takeSnapshot(label.value);
    snaps.value = await listSnapshots();
    label.value = "";
    err.value = "";
  } catch (e) {
    err.value = String(e);
  } finally {
    busy.value = false;
  }
}

onMounted(async () => {
  await refresh();
  snaps.value = await listSnapshots().catch(() => []);
  // Slow poll. This opens the device to answer, so hammering it would fight
  // with the stream for the single capture handle.
  timer = window.setInterval(refresh, 10000);
});
onUnmounted(() => window.clearInterval(timer));

const tone = (d: string) =>
  d === "ok" ? "ok" : d === "backend_missing" ? "warn" : "bad";
</script>

<template>
  <div class="card">
    <div class="card-title">Camera preflight</div>

    <div v-if="!preflight" class="muted">checking...</div>

    <template v-else>
      <div class="verdict" :class="preflight.usable ? 'ok' : 'bad'">
        {{ preflight.usable ? "capture available" : "no capture path" }}
      </div>
      <p v-if="!preflight.usable" class="muted note">
        Nothing downstream may claim to have observed anything.
      </p>

      <div class="section">devices</div>
      <div v-for="d in preflight.devices" :key="d.name" class="row">
        <span class="dot" :class="{ on: d.is_realsense }" />
        <span class="name">{{ d.model || d.name }}</span>
        <span v-if="d.serial" class="num meta">{{ d.serial }}</span>
        <span v-if="d.link_speed_bps" class="num meta" :class="{ bad: d.is_usb3 === false }">
          {{ (d.link_speed_bps / 1e9).toFixed(0) }}G
        </span>
      </div>

      <div class="section">backends</div>
      <div v-for="b in preflight.backends" :key="b.backend" class="backend">
        <div class="row">
          <span class="name">{{ b.backend }}</span>
          <span class="chip" :class="tone(b.diagnosis)">{{ b.diagnosis }}</span>
        </div>
        <p v-if="b.detail" class="muted detail">{{ b.detail }}</p>
        <p v-if="b.remedy" class="remedy">{{ b.remedy }}</p>
      </div>

      <p v-if="!preflight.usable && preflight.responsible_app" class="muted detail">
        camera grant attaches to <span class="num">{{ preflight.responsible_app }}</span>
      </p>

      <div class="section">snapshots</div>
      <div class="snaprow">
        <input v-model="label" class="field" placeholder="label, e.g. cap-off" />
        <button class="btn btn-primary" :disabled="!live || busy" @click="snapshot">
          Snapshot
        </button>
      </div>
      <p v-if="!live" class="muted detail">
        Snapshots need a working capture path.
      </p>
      <div v-for="s in snaps" :key="s.name" class="row">
        <span class="num meta">{{ s.name }}</span>
        <span class="num meta ml">{{ (s.bytes / 1024).toFixed(0) }}k</span>
      </div>

      <p v-if="err" class="remedy">{{ err }}</p>
    </template>
  </div>
</template>

<style scoped>
.muted { color: var(--zeon-muted); font-size: 12px; margin: 4px 0; }
.note { margin-bottom: 8px; }
.verdict { font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; }
.verdict.ok { color: var(--zeon-lime); }
.verdict.bad { color: var(--zeon-red); }
.section { margin: 12px 0 6px; font-size: 10px; font-weight: 600; text-transform: uppercase;
           letter-spacing: 0.08em; color: var(--zeon-muted); border-top: 1px solid var(--zeon-line); padding-top: 8px; }
.row { display: flex; align-items: center; gap: 8px; padding: 2px 0; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--zeon-muted); flex: none; }
.dot.on { background: var(--zeon-lime); }
.name { font-size: 13px; color: var(--zeon-text); }
.meta { font-size: 11px; color: #b5bcb1; }
.meta.bad { color: var(--zeon-red); }
.ml { margin-left: auto; }
.backend { margin-bottom: 8px; }
.chip { margin-left: auto; font-size: 10px; padding: 1px 6px; border-radius: 4px;
        border: 1px solid var(--zeon-line); color: #b5bcb1; text-transform: uppercase; }
.chip.ok { border-color: var(--zeon-lime); color: var(--zeon-lime); }
.chip.warn { border-color: var(--zeon-orange); color: var(--zeon-orange); }
.chip.bad { border-color: var(--zeon-red); color: var(--zeon-red); }
.detail { font-size: 11px; }
.remedy { font-size: 11px; color: #b5bcb1; background: var(--zeon-ink); border-left: 2px solid var(--zeon-blue);
          padding: 6px 8px; margin: 4px 0; border-radius: 0 4px 4px 0; }
.snaprow { display: flex; gap: 8px; align-items: center; }
.field { flex: 1; border-radius: 2px; border: 1px solid var(--zeon-line); background: var(--zeon-ink);
         color: var(--zeon-text); padding: 6px 8px; font-size: 12px; outline: none; }
.field:focus { border-color: var(--zeon-lime); }
.btn { border: 1px solid var(--zeon-line); background: var(--zeon-surface-2); color: var(--zeon-text); border-radius: 2px;
       padding: 6px 12px; font-size: 12px; font-weight: 600; cursor: pointer; }
.btn-primary { border-color: var(--zeon-lime); background: var(--zeon-lime); color: #0a0c09; }
.btn:disabled { opacity: 0.4; cursor: not-allowed; }
.num { font-family: ui-monospace, monospace; font-variant-numeric: tabular-nums; }
</style>
