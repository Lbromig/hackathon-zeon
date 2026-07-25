<script setup lang="ts">
// Absolute moves — the most dangerous control here, so cartesian targets take a
// second confirming click that shows the travel first. The backend independently
// rejects anything beyond `max_move_to_jump`.
import { computed, ref, watch } from "vue";
import type { PoseValues } from "../../api/teach";
import { useTeach } from "../../composables/useTeach";

const { arm, state, canMove, moveToPose, moveToJoints } = useTeach();

const mode = ref<"pose" | "joints">("pose");
const pose = ref<PoseValues>({ x: 0, y: 0, z: 0, roll: 0, pitch: 0, yaw: 0 });
const joints = ref<number[]>([0, 0, 0, 0, 0, 0]);
const armed = ref(false);
let disarmTimer: number | undefined;

const AXES: (keyof PoseValues)[] = ["x", "y", "z", "roll", "pitch", "yaw"];

function loadCurrent() {
  if (state.value?.pose) pose.value = { ...state.value.pose };
  if (state.value?.joints) joints.value = [...state.value.joints];
  disarm();
}

/** Straight-line distance the TCP would travel — what makes a typo obvious. */
const travel = computed(() => {
  const p = state.value?.pose;
  if (!p) return null;
  return Math.hypot(pose.value.x - p.x, pose.value.y - p.y, pose.value.z - p.z);
});

const overLimit = computed(
  () => travel.value !== null && arm.value !== null && travel.value > arm.value.limits.max_move_to_jump,
);

function disarm() {
  armed.value = false;
  if (disarmTimer) window.clearTimeout(disarmTimer);
}

function arm_() {
  armed.value = true;
  if (disarmTimer) window.clearTimeout(disarmTimer);
  disarmTimer = window.setTimeout(disarm, 6000); // don't leave a live "Go" lying around
}

async function go() {
  if (mode.value === "joints") {
    await moveToJoints(joints.value.map(Number));
    return;
  }
  if (!armed.value) {
    arm_();
    return;
  }
  disarm();
  await moveToPose({ ...pose.value });
}

watch(mode, disarm);
watch(pose, disarm, { deep: true });
</script>

<template>
  <section class="card">
    <div class="flex items-center justify-between">
      <h2 class="card-title mb-0">Move to</h2>
      <div class="flex gap-1">
        <button class="chip" :class="{ 'chip-on': mode === 'pose' }" @click="mode = 'pose'">pose</button>
        <button class="chip" :class="{ 'chip-on': mode === 'joints' }" @click="mode = 'joints'">
          joints
        </button>
        <button class="btn btn-sm btn-ghost" :disabled="!state?.pose" @click="loadCurrent">
          load current
        </button>
      </div>
    </div>

    <div v-if="mode === 'pose'" class="mt-3 grid grid-cols-3 gap-2">
      <label v-for="a in AXES" :key="a" class="text-xs text-deck-400">
        {{ a }} <span class="text-deck-600">{{ ["x", "y", "z"].includes(a) ? "mm" : "°" }}</span>
        <input v-model.number="pose[a]" type="number" step="0.1" class="field num mt-1" />
      </label>
    </div>

    <div v-else class="mt-3 grid grid-cols-3 gap-2">
      <label v-for="(_, i) in joints" :key="i" class="text-xs text-deck-400">
        J{{ i + 1 }} <span class="text-deck-600">°</span>
        <input v-model.number="joints[i]" type="number" step="0.1" class="field num mt-1" />
      </label>
    </div>

    <div class="mt-3 flex items-center gap-3">
      <button
        class="btn"
        :class="armed && mode === 'pose' ? 'btn-danger' : 'btn-primary'"
        :disabled="!canMove || (mode === 'pose' && overLimit)"
        @click="go"
      >
        {{ mode === "joints" ? "Go (joints)" : armed ? "Confirm move" : "Review move" }}
      </button>
      <span v-if="mode === 'pose' && travel !== null" class="text-xs" :class="overLimit ? 'text-red-400' : 'text-deck-400'">
        travel <span class="num">{{ travel.toFixed(1) }}</span> mm
        <template v-if="overLimit">
          — over the {{ arm?.limits.max_move_to_jump }} mm limit, jog closer first
        </template>
        <template v-else-if="armed">— click again to execute</template>
      </span>
      <span v-else-if="mode === 'joints'" class="text-xs text-deck-400">
        joint targets move each axis directly — check clearance first
      </span>
    </div>
  </section>
</template>
