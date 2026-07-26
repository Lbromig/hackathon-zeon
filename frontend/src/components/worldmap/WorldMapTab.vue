<script setup lang="ts">
// Top-down world map: every camera and tracked object in the shared world frame.
//
// This is the shared-frame proof — both fixed cameras were placed by solving the
// same board, so if they land at their true relative positions, the calibration is
// right. That is the whole point of the picture, so the panel names the origin and
// reports camera positions numerically alongside it.
//
// The map itself is the server's SVG, inlined (not <img>) so it inherits the page
// theme and scales with the layout. Re-rendering the JSON here would mean a second
// renderer to keep in step with core/viz/scene.py.
import { computed, onMounted, onUnmounted, ref } from "vue";
import type { Scene } from "../../api/worldmodel";
import { getScene, getSceneSvg, NotCalibrated, SCENE_SVG_URL } from "../../api/worldmodel";

const REFRESH_MS = 1000; // 1 Hz — the twin moves at human speed

const svg = ref("");
const scene = ref<Scene | null>(null);
const notCalibrated = ref("");
const error = ref("");
const live = ref(true);
const updatedAt = ref("");

let timer: number | undefined;
let inFlight = false;

const cameras = computed(() => scene.value?.cameras ?? []);
/** Everything that isn't a camera, grouped by kind for a readable panel. */
const byKind = computed(() => {
  const groups: Record<string, Scene["entities"]> = {};
  for (const e of scene.value?.entities ?? []) (groups[e.kind] ??= []).push(e);
  return Object.entries(groups).sort(([a], [b]) => a.localeCompare(b));
});

const mm = (v: number) => `${(v * 1000).toFixed(0)}`;
const deg = (rad: number) => `${((rad * 180) / Math.PI).toFixed(0)}°`;

async function refresh() {
  if (inFlight || document.hidden) return;
  inFlight = true;
  try {
    // Fetch both: the SVG is what you look at, the JSON is what you read numbers
    // off. They come from one twin snapshot each, a few ms apart — fine at 1 Hz.
    const [markup, data] = await Promise.all([getSceneSvg(), getScene()]);
    svg.value = markup;
    scene.value = data;
    notCalibrated.value = "";
    error.value = "";
    updatedAt.value = new Date().toLocaleTimeString();
  } catch (e) {
    if (e instanceof NotCalibrated) {
      notCalibrated.value = e.message;
      svg.value = "";
      scene.value = null;
    } else {
      error.value = e instanceof Error ? e.message : String(e);
    }
  } finally {
    inFlight = false;
  }
}

function setLive(on: boolean) {
  live.value = on;
  if (on) {
    void refresh();
    timer ??= window.setInterval(refresh, REFRESH_MS);
  } else if (timer !== undefined) {
    window.clearInterval(timer);
    timer = undefined;
  }
}

onMounted(() => setLive(true));
onUnmounted(() => setLive(false));
</script>

<template>
  <div class="space-y-4">
    <section class="card flex flex-wrap items-center gap-3">
      <h2 class="card-title mb-0">World map</h2>
      <span class="text-xs text-deck-400">
        Top-down, world frame, +X right / +Y up. Both fixed cameras were placed by
        solving the same board, so their relative positions here are the calibration.
      </span>
      <div class="ml-auto flex items-center gap-2">
        <span v-if="updatedAt" class="num text-xs text-deck-400">{{ updatedAt }}</span>
        <button class="btn btn-sm" @click="setLive(!live)">{{ live ? "Pause" : "Go live" }}</button>
        <button class="btn btn-sm btn-ghost" title="Refresh now" @click="refresh">↻</button>
        <a class="btn btn-sm btn-ghost" :href="SCENE_SVG_URL" target="_blank" rel="noopener">
          Open SVG
        </a>
      </div>
    </section>

    <p v-if="error" class="card text-sm text-red-400">{{ error }}</p>

    <section v-if="notCalibrated" class="card">
      <h2 class="card-title">Not calibrated yet</h2>
      <p class="text-sm text-deck-200">{{ notCalibrated }}</p>
      <p class="mt-2 text-xs text-deck-400">
        The map is built from the twin, and the twin has no world frame until calibration
        solves the fixed board. Run the calibration pass (<code class="text-deck-200">/ws/calibrate</code>),
        then this fills in — cameras first, then every tracked object.
      </p>
    </section>

    <div v-else class="grid gap-4 xl:grid-cols-[2fr_1fr]">
      <!-- The server's own rendering, inlined so it scales and themes with the page. -->
      <section class="card">
        <div v-if="svg" class="worldmap overflow-x-auto" v-html="svg" />
        <p v-else class="text-sm text-deck-400">Loading the scene…</p>
      </section>

      <div class="space-y-4">
        <section class="card">
          <h2 class="card-title">Cameras</h2>
          <p v-if="!cameras.length" class="text-xs text-deck-400">
            No cameras placed in the world frame yet.
          </p>
          <ul v-else class="space-y-2 text-sm">
            <li v-for="c in cameras" :key="c.id" class="flex items-baseline gap-2">
              <span class="h-2 w-2 shrink-0 rounded-full bg-sky-400" />
              <span class="font-semibold text-deck-100">{{ c.id }}</span>
              <span class="num ml-auto text-xs text-deck-400">
                {{ mm(c.x) }}, {{ mm(c.y) }}, {{ mm(c.z) }} mm · {{ deg(c.heading) }}
              </span>
            </li>
          </ul>
        </section>

        <section class="card">
          <div class="flex items-baseline justify-between">
            <h2 class="card-title mb-0">Tracked objects</h2>
            <span class="num text-xs text-deck-400">{{ scene?.entities.length ?? 0 }}</span>
          </div>
          <div v-for="[kind, items] in byKind" :key="kind" class="mt-3">
            <div class="text-xs uppercase tracking-wider text-deck-400">{{ kind }}</div>
            <ul class="mt-1 space-y-1 text-sm">
              <li v-for="e in items" :key="e.id" class="flex items-baseline gap-2">
                <span class="truncate text-deck-100">{{ e.id }}</span>
                <span class="num ml-auto shrink-0 text-xs text-deck-400">
                  {{ mm(e.x) }}, {{ mm(e.y) }}, {{ mm(e.z) }} mm
                </span>
              </li>
            </ul>
          </div>
          <p v-if="!byKind.length" class="mt-2 text-xs text-deck-400">
            Nothing tracked yet — objects appear as detections resolve into the twin.
          </p>
        </section>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* The server SVG carries its own sizing; make it fluid inside the card. */
.worldmap :deep(svg) {
  width: 100%;
  height: auto;
  max-width: 100%;
}
</style>
