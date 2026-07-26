<script setup lang="ts">
// One RealSense feed: MJPEG frame with an SVG detection overlay on top.
//
// The overlay is SVG rather than pixels burnt into the frame: it stays crisp at
// any size, it can be restyled per verdict without re-encoding video, and the
// detector's rate is free to differ from the video's.
//
// An offline camera does NOT get an <img> pointed at the stream: that endpoint
// opens the device, so a disconnected RealSense would be re-probed on every
// browser retry. Offline shows what is configured and why it failed instead.
import { computed, ref, watch } from "vue";
import type { CameraFrameState, CameraSummary, Detection } from "../../api/cameras";
import { connectCamera, snapshotUrl, stopCamera, streamUrl } from "../../api/cameras";

const props = withDefaults(
  defineProps<{
    camera: CameraSummary;
    state?: CameraFrameState;
    /** Draw detection outlines at all. Off gives a clean feed for screenshots. */
    overlay?: boolean;
    /** Draw the id/entity/range text. Off keeps the outlines but unclutters the frame. */
    labels?: boolean;
  }>(),
  { overlay: true, labels: true },
);
const emit = defineEmits<{
  (e: "select", detection: Detection): void;
  (e: "changed"): void;
}>();

const wantLive = ref(true);
const nonce = ref(Date.now());
const imgError = ref(false);
const connecting = ref(false);
const connectError = ref("");
const selected = ref<string | null>(null);

const online = computed(() => props.camera.connected);
/**
 * Ask for the stream unless a previous attempt failed.
 *
 * NOT gated on `camera.connected`: a camera only reports connected while a hub
 * worker holds it, and the worker only starts when someone requests the stream —
 * gating on it deadlocks, and every camera sits at "offline" forever. The stream
 * endpoint opens the device on demand, so requesting it *is* how a camera comes
 * online. `imgError` latches after one failure so an absent camera is probed
 * once per page load rather than in a retry loop.
 */
const showVideo = computed(() => wantLive.value && !imgError.value);
const src = computed(() =>
  wantLive.value ? streamUrl(props.camera.id, nonce.value) : snapshotUrl(props.camera.id),
);

// viewBox is in *frame pixels*, so label sizes are reasoned about in image terms;
// normalized detections scale into it.
const width = computed(() => props.state?.w || props.camera.width || props.camera.configured.width || 1280);
const height = computed(() => props.state?.h || props.camera.height || props.camera.configured.height || 720);
const detections = computed(() => props.state?.detections ?? []);
const labelSize = computed(() => Math.max(10, height.value / 26));

const intrinsics = computed(() => props.state?.intrinsics ?? props.camera.intrinsics);
const configured = computed(() => {
  const c = props.camera.configured;
  return c.width ? `${c.width}×${c.height}@${c.fps}` : "—";
});

const key = (d: Detection, i: number) => `${d.marker_id ?? d.kind}-${i}`;

const points = (d: Detection) =>
  d.polygon.map(([x, y]) => `${x * width.value},${y * height.value}`).join(" ");

const colour = (d: Detection) => {
  return d.source === "cv" ? "#f59e0b" : "#22c55e";
};

/** Depth beats the tag pose: a 20 mm tag subtends few pixels, so PnP range is noisy. */
const range = (d: Detection): string | null => {
  if (d.depth_m) return `${(d.depth_m * 1000).toFixed(0)} mm`;
  if (d.distance_m) return `~${(d.distance_m * 1000).toFixed(0)} mm`;
  return null;
};

const label = (d: Detection) => {
  const bits = [d.marker_id !== null ? `id ${d.marker_id}` : d.kind];
  const r = range(d);
  if (r) bits.push(r);
  return bits.join("  ·  ");
};

function pick(d: Detection) {
  selected.value = String(d.marker_id ?? d.kind);
  emit("select", d);
}

function reconnect() {
  imgError.value = false;
  nonce.value = Date.now(); // force the browser to drop a dead multipart response
}

async function connect() {
  connecting.value = true;
  connectError.value = "";
  try {
    const res = await connectCamera(props.camera.id);
    if (!res.ok) connectError.value = res.detail;
    else reconnect();
    emit("changed");
  } catch (e) {
    connectError.value = e instanceof Error ? e.message : String(e);
  } finally {
    connecting.value = false;
  }
}

async function toggleLive() {
  wantLive.value = !wantLive.value;
  if (wantLive.value) reconnect();
  else await stopCamera(props.camera.id);
}

watch(() => props.camera.id, reconnect);
</script>

<template>
  <section class="card">
    <div class="mb-2 flex flex-wrap items-center gap-2">
      <span
        class="h-2.5 w-2.5 rounded-full"
        :class="
          state?.error || imgError ? 'bg-red-500' : camera.streaming ? 'bg-emerald-500' : online ? 'bg-amber-400' : 'bg-deck-400'
        "
      />
      <strong class="text-sm">{{ camera.name }}</strong>
      <span v-if="camera.has_depth" class="chip !cursor-default !border-sky-500/50 !text-sky-300">RGB-D</span>
      <span class="num text-xs text-deck-400">
        <template v-if="camera.streaming">
          {{ width }}×{{ height }}
          <template v-if="state?.fps"> · {{ state.fps.toFixed(0) }} fps</template>
          <template v-if="detections.length"> · {{ detections.length }} tags</template>
        </template>
        <template v-else>{{ configured }} · {{ camera.state }}</template>
      </span>
      <div class="ml-auto flex gap-1">
        <button v-if="imgError" class="btn btn-sm btn-primary" :disabled="connecting" @click="connect">
          {{ connecting ? "Connecting…" : "Retry" }}
        </button>
        <template v-else>
          <button class="btn btn-sm" @click="toggleLive">{{ wantLive ? "Pause" : "Go live" }}</button>
          <button class="btn btn-sm btn-ghost" title="Reconnect the stream" @click="reconnect">↻</button>
        </template>
      </div>
    </div>

    <div class="relative overflow-hidden rounded-lg bg-black">
      <img
        v-if="showVideo"
        :src="src"
        :alt="`${camera.name} feed`"
        class="block w-full"
        @error="imgError = true"
        @load="imgError = false"
      />

      <!-- offline: describe what is configured rather than probing a dead device -->
      <div
        v-else
        class="flex flex-col items-center justify-center gap-1 py-14 text-center"
        :style="{ aspectRatio: `${width} / ${height}` }"
      >
        <span class="text-sm font-semibold text-deck-200">
          {{ imgError ? "camera offline" : "paused" }}
        </span>
        <span class="num text-xs text-deck-400">
          serial {{ camera.serial || "not pinned" }} · {{ configured }}
        </span>
        <span v-if="connectError || camera.error" class="mt-1 max-w-md px-4 text-xs text-red-400">
          {{ connectError || camera.error }}
        </span>
      </div>

      <svg
        v-if="showVideo && overlay"
        class="pointer-events-none absolute inset-0 h-full w-full"
        :viewBox="`0 0 ${width} ${height}`"
        preserveAspectRatio="none"
      >
        <g v-for="(d, i) in detections" :key="key(d, i)">
          <polygon
            :points="points(d)"
            :stroke="colour(d)"
            :fill="colour(d)"
            fill-opacity="0.12"
            stroke-width="2"
            vector-effect="non-scaling-stroke"
            class="pointer-events-auto cursor-pointer"
            @click="pick(d)"
          />
          <text
            v-if="labels"
            :x="d.center[0] * width"
            :y="d.center[1] * height - labelSize * 0.9"
            :font-size="labelSize"
            :fill="colour(d)"
            text-anchor="middle"
            paint-order="stroke"
            stroke="#000"
            stroke-width="3"
            vector-effect="non-scaling-stroke"
            class="font-mono"
          >
            {{ label(d) }}
          </text>
        </g>
      </svg>

      <p
        v-if="showVideo && (state?.error || imgError)"
        class="absolute inset-x-0 bottom-0 bg-red-600/80 px-3 py-1.5 text-xs text-white"
      >
        {{ state?.error || "stream not loading — the device may have been unplugged" }}
      </p>
    </div>

    <div class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-deck-400">
      <span v-if="intrinsics" class="num">
        K fx {{ intrinsics.fx.toFixed(1) }} · fy {{ intrinsics.fy.toFixed(1) }} · cx
        {{ intrinsics.cx.toFixed(1) }} · cy {{ intrinsics.cy.toFixed(1) }}
      </span>
      <span v-else-if="camera.has_depth">Factory intrinsics load on connect.</span>
      <span v-if="selected" class="num text-deck-200">selected {{ selected }}</span>
      <span v-if="camera.streaming && !detections.length">
        No <code class="text-deck-200">tag36h11</code> markers in view.
      </span>
    </div>
  </section>
</template>
