<script setup lang="ts">
// Camera tab: the three RealSense viewpoints, with live AprilTag highlighting and
// a discovery panel for pinning each physical unit to a fleet id in .env.
import { computed, onMounted, ref } from "vue";
import type {
  CameraDevices,
  CameraFrameState,
  CameraSummary,
  Detection,
} from "../../api/cameras";
import { listCameras, listDevices } from "../../api/cameras";
import CameraView from "./CameraView.vue";

defineProps<{ cameras: Record<string, CameraFrameState> }>();

const devices = ref<CameraSummary[]>([]);
const attached = ref<CameraDevices | null>(null);
const error = ref("");
const lastPick = ref<Detection | null>(null);
const scanning = ref(false);

// Overlay visibility, shared across every feed and remembered: turning it off is
// how you get a clean frame for a screenshot or a slide, and having to redo that
// on each reload (or per camera) defeats the point.
const OVERLAY_KEY = "cameras.overlay";
const saved = (() => {
  try {
    return JSON.parse(localStorage.getItem(OVERLAY_KEY) ?? "{}");
  } catch {
    return {};
  }
})();
const showOverlay = ref<boolean>(saved.overlay ?? true);
const showLabels = ref<boolean>(saved.labels ?? true);

function persistOverlay() {
  localStorage.setItem(
    OVERLAY_KEY,
    JSON.stringify({ overlay: showOverlay.value, labels: showLabels.value }),
  );
}
function toggleOverlay() {
  showOverlay.value = !showOverlay.value;
  persistOverlay();
}
function toggleLabels() {
  showLabels.value = !showLabels.value;
  persistOverlay();
}

const online = computed(() => devices.value.filter((d) => d.connected).length);

/** Which .env var pins a given fleet id — the whole point of the discovery table. */
const ENV_VAR: Record<string, string> = {
  gripper_cam: "CAM_GRIPPER",
  gripper_left_cam: "CAM_GRIPPER_LEFT",
  overview_cam: "CAM_OVERVIEW",
  handover_cam: "CAM_HANDOVER",
};

async function refresh() {
  try {
    devices.value = await listCameras();
    error.value = "";
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  }
}

async function scan() {
  scanning.value = true;
  try {
    attached.value = await listDevices();
  } catch (e) {
    attached.value = { devices: [], error: e instanceof Error ? e.message : String(e) };
  } finally {
    scanning.value = false;
  }
}

onMounted(refresh);
</script>

<template>
  <div class="space-y-4">
    <section class="card flex flex-wrap items-center gap-3">
      <h2 class="card-title mb-0">Cameras</h2>
      <span class="num text-xs text-deck-400">{{ online }}/{{ devices.length }} online</span>
      <span class="text-xs text-deck-400">
        AprilTag <code class="text-deck-200">tag36h11</code> detection runs at ~5 Hz on the
        backend. On RGB-D units the tag centre also carries measured depth.
      </span>
      <div class="ml-auto flex items-center gap-2">
        <button
          class="chip"
          :class="{ 'chip-on': showOverlay }"
          title="Draw detection outlines on the feeds"
          @click="toggleOverlay"
        >
          Overlay
        </button>
        <button
          class="chip"
          :class="{ 'chip-on': showLabels }"
          :disabled="!showOverlay"
          title="Draw the id / range text inside the outlines"
          @click="toggleLabels"
        >
          Labels
        </button>
        <button class="btn btn-sm" :disabled="scanning" @click="scan">
          {{ scanning ? "Scanning…" : "Scan for devices" }}
        </button>
        <button class="btn btn-sm" @click="refresh">Refresh</button>
      </div>
    </section>

    <!-- discovery: serial -> .env var -->
    <section v-if="attached" class="card">
      <h2 class="card-title">Attached RealSense units</h2>
      <p v-if="attached.error" class="text-sm text-amber-400">
        {{ attached.error }}
      </p>
      <p v-else-if="!attached.devices.length" class="text-sm text-deck-400">
        No RealSense devices detected — check USB and power, then scan again.
      </p>
      <table v-else class="w-full text-left text-sm">
        <thead class="text-xs uppercase tracking-wider text-deck-400">
          <tr>
            <th class="py-1">serial</th><th>model</th><th>firmware</th><th>assigned to</th>
          </tr>
        </thead>
        <tbody class="num">
          <tr v-for="d in attached.devices" :key="d.serial" class="border-t border-deck-700">
            <td class="py-1.5">{{ d.serial }}</td>
            <td>{{ d.name }}</td>
            <td class="text-deck-400">{{ d.firmware }}</td>
            <td>
              <span v-if="d.assigned_to" class="text-emerald-400">{{ d.assigned_to }}</span>
              <span v-else class="text-amber-400">unassigned</span>
            </td>
          </tr>
        </tbody>
      </table>
      <p class="mt-3 text-xs text-deck-400">
        Pin each unit in <code class="text-deck-200">.env</code> —
        <code class="text-deck-200">CAM_GRIPPER</code>,
        <code class="text-deck-200">CAM_OVERVIEW</code>,
        <code class="text-deck-200">CAM_HANDOVER</code> — then restart the backend.
        Enumeration order is not stable across replugs, so leaving them blank makes
        "which camera is which" a coin flip once more than one is attached.
      </p>
    </section>

    <p v-if="error" class="card text-sm text-red-400">{{ error }}</p>
    <p v-else-if="!devices.length" class="card text-sm text-deck-400">
      No cameras in the fleet. The three RealSense units are defined in
      <code class="text-deck-200">core/config.py</code> and configured through
      <code class="text-deck-200">.env</code>; or run with
      <code class="text-deck-200">HZ_FLEET_FILE=fleet.mock.json</code> for synthetic tag feeds.
    </p>

    <div class="grid gap-4 xl:grid-cols-2">
      <CameraView
        v-for="cam in devices"
        :key="cam.id"
        :camera="cam"
        :state="cameras[cam.id]"
        :overlay="showOverlay"
        :labels="showLabels"
        @select="lastPick = $event"
        @changed="refresh"
      />
    </div>

    <section v-if="devices.length" class="card">
      <h2 class="card-title">Fleet ids → .env</h2>
      <ul class="space-y-1 text-xs text-deck-400">
        <li v-for="cam in devices" :key="cam.id" class="num">
          <span class="text-deck-100">{{ cam.id }}</span>
          → <code class="text-deck-200">{{ ENV_VAR[cam.id] ?? "—" }}</code>
          = {{ cam.serial || "(unset: binds by enumeration order)" }}
        </li>
      </ul>
    </section>

    <section v-if="lastPick" class="card">
      <h2 class="card-title">Selected detection</h2>
      <dl class="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-5">
        <div><dt class="text-xs text-deck-400">marker</dt><dd class="num">{{ lastPick.marker_id ?? "—" }}</dd></div>
        <div>
          <dt class="text-xs text-deck-400">depth (measured)</dt>
          <dd class="num">{{ lastPick.depth_m !== null ? `${(lastPick.depth_m * 1000).toFixed(0)} mm` : "—" }}</dd>
        </div>
        <div>
          <dt class="text-xs text-deck-400">range (tag pose)</dt>
          <dd class="num">{{ lastPick.distance_m !== null ? `${(lastPick.distance_m * 1000).toFixed(0)} mm` : "—" }}</dd>
        </div>
        <div>
          <dt class="text-xs text-deck-400">camera frame (m)</dt>
          <dd class="num">
            {{ lastPick.camera_xyz ? lastPick.camera_xyz.map((v) => v.toFixed(3)).join(", ") : "—" }}
          </dd>
        </div>
      </dl>
      <p class="mt-3 text-xs text-deck-400">
        These are <em>camera-frame</em> metres. There is no shared world frame any more —
        the offset the workflow needs is computed between two features in
        <em>one</em> camera's frame (R-VIS-12), which is why the calibration pipeline
        went away rather than being finished.
      </p>
    </section>
  </div>
</template>
