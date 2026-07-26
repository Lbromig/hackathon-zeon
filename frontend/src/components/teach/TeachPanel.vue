<script setup lang="ts">
// Teach tab root: arm picker + status, jog settings, safety controls, and the
// keyboard bindings. Children read shared state from useTeach() directly.
import { computed, onMounted, onUnmounted, ref } from "vue";
import {
  ANGULAR_STEPS,
  LINEAR_STEPS,
  SPEED_PRESETS,
  useTeach,
} from "../../composables/useTeach";
import CapTools from "./CapTools.vue";
import CommandLog from "./CommandLog.vue";
import GripperControl from "./GripperControl.vue";
import JogPad from "./JogPad.vue";
import JointJog from "./JointJog.vue";
import MoveTo from "./MoveTo.vue";
import PoseLibrary from "./PoseLibrary.vue";
import WaypointChecklist from "./WaypointChecklist.vue";

const t = useTeach();
const {
  arms,
  arm,
  selectedId,
  state,
  settings,
  sending,
  loadError,
  connected,
  faulted,
  canMove,
} = t;

const copied = ref("");

const statusLabel = computed(() => {
  if (!state.value) return "…";
  if (!state.value.connected) return state.value.state;
  if (faulted.value) return `error ${state.value.error_code}`;
  return sending.value ? "moving" : "ready";
});

const statusColor = computed(() => {
  if (!state.value?.connected) return "bg-deck-400";
  if (faulted.value) return "bg-red-500";
  return sending.value ? "bg-amber-400" : "bg-emerald-500";
});

/** Copy the current pose in a form that can be pasted straight into a workflow step. */
async function copyPose(kind: "json" | "python") {
  const p = state.value?.pose;
  const j = state.value?.joints;
  if (!p) return;
  const round = (v: number) => Number(v.toFixed(2));
  const text =
    kind === "json"
      ? JSON.stringify(
          { pose: Object.fromEntries(Object.entries(p).map(([k, v]) => [k, round(v)])), joints: j?.map(round) },
          null,
          2,
        )
      : `arm.move_to(Pose(x=${round(p.x)}, y=${round(p.y)}, z=${round(p.z)}, ` +
        `roll=${round(p.roll)}, pitch=${round(p.pitch)}, yaw=${round(p.yaw)}))` +
        (j ? `\n# joints: ${JSON.stringify(j.map(round))}` : "");
  try {
    await navigator.clipboard.writeText(text);
    copied.value = kind;
    window.setTimeout(() => (copied.value = ""), 1500);
  } catch {
    copied.value = "";
  }
}

// --- keyboard ---------------------------------------------------------------

const KEY_AXES: Record<string, [string, 1 | -1]> = {
  ArrowRight: ["x", 1],
  ArrowLeft: ["x", -1],
  ArrowUp: ["y", 1],
  ArrowDown: ["y", -1],
  PageUp: ["z", 1],
  PageDown: ["z", -1],
  e: ["yaw", 1],
  q: ["yaw", -1],
};

function onKey(ev: KeyboardEvent) {
  const target = ev.target as HTMLElement | null;
  if (target && ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return;
  if (ev.metaKey || ev.ctrlKey || ev.altKey) return;

  if (ev.key === "Escape") {
    ev.preventDefault();
    void t.estop();
    return;
  }
  if (!canMove.value) return;

  const jogKey = KEY_AXES[ev.key];
  if (jogKey) {
    ev.preventDefault();
    void t.jogCartesian(jogKey[0], jogKey[1]);
    return;
  }
  if (ev.key === "[") {
    ev.preventDefault();
    void t.closeGripper();
  } else if (ev.key === "]") {
    ev.preventDefault();
    void t.openGripper();
  } else if (["1", "2", "3", "4"].includes(ev.key)) {
    ev.preventDefault();
    setLinear(LINEAR_STEPS[Number(ev.key) - 1]);
  }
}

function setLinear(v: number) {
  settings.linear = v;
  t.persistSettings();
}
function setAngular(v: number) {
  settings.angular = v;
  t.persistSettings();
}
function setSpeed(v: number) {
  settings.speed = v;
  t.persistSettings();
}

onMounted(() => {
  t.startPolling();
  window.addEventListener("keydown", onKey);
});
onUnmounted(() => {
  t.stopPolling();
  window.removeEventListener("keydown", onKey);
});
</script>

<template>
  <div class="space-y-4">
    <!-- arm picker + status + safety -->
    <section class="card">
      <div class="flex flex-wrap items-center gap-3">
        <div class="flex gap-1">
          <button
            v-for="a in arms"
            :key="a.id"
            class="chip"
            :class="{ 'chip-on': a.id === selectedId }"
            @click="t.select(a.id)"
          >
            {{ a.name }}
          </button>
          <span v-if="!arms.length" class="text-sm text-deck-400">
            {{ loadError || "No arms in the fleet config." }}
          </span>
        </div>

        <span class="flex items-center gap-2 text-sm">
          <span class="h-2.5 w-2.5 rounded-full" :class="statusColor" />
          <span class="text-deck-200">{{ statusLabel }}</span>
          <span v-if="state?.busy" class="text-xs text-amber-400">busy</span>
        </span>

        <div class="ml-auto flex flex-wrap gap-2">
          <button v-if="!connected" class="btn btn-primary" :disabled="!selectedId || sending" @click="t.connect()">
            Connect
          </button>
          <button class="btn" :disabled="!connected || sending" @click="t.setEnabled(true)">Enable</button>
          <button class="btn" :disabled="!connected || sending" @click="t.setEnabled(false)">Disable</button>
          <button
            class="btn"
            :class="{ 'btn-primary': state?.free_drive }"
            :disabled="!connected"
            :title="state?.free_drive
              ? 'return to position control'
              : 'make the arm back-drivable so you can position it by hand'"
            @click="t.setFreeDrive(!state?.free_drive)"
          >
            {{ state?.free_drive ? "Hand-guiding ✋" : "Hand-guide" }}
          </button>
          <button class="btn" :disabled="!canMove" @click="t.goHome()">Home</button>
          <button class="btn" :class="{ 'btn-primary': faulted }" :disabled="!connected" @click="t.clearErrors()">
            Clear errors
          </button>
          <button class="btn btn-danger" :disabled="!selectedId" title="Esc" @click="t.estop()">
            ■ E-STOP
          </button>
        </div>
      </div>

      <p
        v-if="state?.free_drive"
        class="mt-3 rounded-lg border border-sky-500/40 bg-sky-500/10 px-3 py-2 text-sm text-sky-200"
      >
        <strong>Hand-guiding is on</strong> — the arm is back-drivable. Support it: it holds
        against gravity using the configured payload, so it sinks if that value is too low.
        Commanded moves do not behave normally until you switch this off.
      </p>
      <p v-if="faulted" class="mt-3 rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
        Arm reports error {{ state?.error_code }} — motion is blocked until you clear it.
      </p>
      <p v-else-if="state && !state.connected" class="mt-3 text-sm text-deck-400">
        {{ arm?.name }} is {{ state.state }}. Connect it to jog.
      </p>
      <p v-else-if="state?.detail" class="mt-3 text-sm text-amber-400">{{ state.detail }}</p>
    </section>

    <!-- increments + speed -->
    <section class="card">
      <div class="flex flex-wrap items-center gap-x-6 gap-y-3">
        <div class="flex items-center gap-2">
          <span class="text-xs uppercase tracking-wider text-deck-400">Linear</span>
          <button
            v-for="(s, i) in LINEAR_STEPS"
            :key="s"
            class="chip"
            :class="{ 'chip-on': settings.linear === s }"
            :title="i < 4 ? `key ${i + 1}` : ''"
            @click="setLinear(s)"
          >
            {{ s }} mm
          </button>
        </div>

        <div class="flex items-center gap-2">
          <span class="text-xs uppercase tracking-wider text-deck-400">Angular</span>
          <button
            v-for="s in ANGULAR_STEPS"
            :key="s"
            class="chip"
            :class="{ 'chip-on': settings.angular === s }"
            @click="setAngular(s)"
          >
            {{ s }}°
          </button>
        </div>

        <div class="flex items-center gap-2">
          <span class="text-xs uppercase tracking-wider text-deck-400">Speed</span>
          <button
            v-for="p in SPEED_PRESETS"
            :key="p.value"
            class="chip"
            :class="{ 'chip-on': settings.speed === p.value }"
            @click="setSpeed(p.value)"
          >
            {{ p.label }}
          </button>
          <input
            :value="settings.speed"
            type="number"
            min="1"
            :max="arm?.limits.max_speed_linear ?? 200"
            class="field num w-20"
            @change="setSpeed(Number(($event.target as HTMLInputElement).value))"
          />
          <span class="text-xs text-deck-400">mm/s · °/s</span>
        </div>
      </div>
    </section>

    <div class="grid gap-4 lg:grid-cols-2">
      <!-- First, because on a teach session it is the task: everything below it is a tool
           for getting the arm to the point this list is asking for. -->
      <WaypointChecklist />
      <JogPad />
      <JointJog />
      <MoveTo />
      <GripperControl />
      <CapTools />
      <PoseLibrary />
      <CommandLog />
    </div>

    <!-- readout + shortcuts -->
    <section class="card">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 class="card-title">Current position</h2>
          <p class="num text-sm text-deck-100">
            <template v-if="state?.pose">
              x {{ state.pose.x.toFixed(2) }} · y {{ state.pose.y.toFixed(2) }} · z
              {{ state.pose.z.toFixed(2) }} mm<br />
              roll {{ state.pose.roll.toFixed(2) }} · pitch {{ state.pose.pitch.toFixed(2) }} · yaw
              {{ state.pose.yaw.toFixed(2) }}°
            </template>
            <template v-else>—</template>
          </p>
          <p v-if="state?.joints" class="num mt-1 text-sm text-deck-200">
            joints {{ state.joints.map((j) => j.toFixed(2)).join(" · ") }}°
          </p>
          <div class="mt-2 flex gap-2">
            <button class="btn btn-sm" :disabled="!state?.pose" @click="copyPose('json')">
              {{ copied === "json" ? "copied" : "copy JSON" }}
            </button>
            <button class="btn btn-sm" :disabled="!state?.pose" @click="copyPose('python')">
              {{ copied === "python" ? "copied" : "copy Python" }}
            </button>
          </div>
        </div>

        <div>
          <h2 class="card-title">Shortcuts</h2>
          <ul class="space-y-1 text-xs text-deck-400">
            <li><span class="kbd">←</span> <span class="kbd">→</span> X · <span class="kbd">↑</span> <span class="kbd">↓</span> Y</li>
            <li><span class="kbd">PgUp</span> <span class="kbd">PgDn</span> Z · <span class="kbd">Q</span> <span class="kbd">E</span> yaw</li>
            <li><span class="kbd">[</span> close · <span class="kbd">]</span> open gripper</li>
            <li><span class="kbd">1</span>–<span class="kbd">4</span> linear step · <span class="kbd">Esc</span> E-STOP</li>
          </ul>
        </div>
      </div>
    </section>
  </div>
</template>
