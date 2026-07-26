<script setup lang="ts">
// Cap manipulation: grab, ungrab, and the ratchet unscrew.
//
// Unscrew is not a single 360° spin — the tool cabling cannot take it. The wrist turns
// 180° with the cap held, opens, unwinds 180° with the cap free, re-grips, and turns
// again. Two bites therefore back the cap off a full turn while the wrist never travels
// more than 180° at once. The backend pre-flights every wrist angle before it moves, so
// a request that would run J6 past its soft limit is refused with nothing having moved.
import { computed, ref } from "vue";
import { useTeach } from "../../composables/useTeach";

const { state, arm, canMove, grabCap, ungrabCap, unscrewCap } = useTeach();

const halfTurns = ref(2);
const useWidth = ref(false);
const width = ref(0);
const armed = ref(false);

const supportsWidth = computed(() => arm.value?.gripper.supports_width ?? false);
const maxWidth = computed(() => arm.value?.gripper.max_width ?? 850);
const gripWidth = computed(() => (useWidth.value && supportsWidth.value ? width.value : undefined));
const capDegrees = computed(() => halfTurns.value * 180);

async function unscrew() {
  // Two-step confirm: this is the longest unattended motion in the teach tab, and it
  // runs the gripper open/closed mid-sequence.
  if (!armed.value) {
    armed.value = true;
    window.setTimeout(() => (armed.value = false), 5000);
    return;
  }
  armed.value = false;
  await unscrewCap(halfTurns.value, gripWidth.value);
}
</script>

<template>
  <section class="card">
    <h2 class="card-title">Cap</h2>

    <div class="flex flex-wrap gap-2">
      <button class="btn" :disabled="!canMove" @click="grabCap(gripWidth)">Grab cap</button>
      <button class="btn" :disabled="!canMove" @click="ungrabCap()">Ungrab cap</button>
    </div>

    <div v-if="supportsWidth" class="mt-3 flex items-center gap-2 text-xs">
      <label class="flex items-center gap-1.5 text-deck-300">
        <input v-model="useWidth" type="checkbox" />
        grip to width
      </label>
      <input
        v-model.number="width"
        type="number"
        class="field w-24 num"
        :disabled="!useWidth"
        :min="0"
        :max="maxWidth"
      />
      <span class="text-deck-500">
        {{ arm?.gripper.units }} · 0 = closed, {{ maxWidth }} = open
      </span>
    </div>
    <p v-else class="mt-2 text-xs text-deck-400">
      This gripper has no commandable width — grab closes fully.
    </p>

    <hr class="my-3 border-deck-700" />

    <div class="flex flex-wrap items-center gap-2">
      <span class="text-xs uppercase tracking-wider text-deck-400">Unscrew</span>
      <button
        v-for="n in [1, 2, 3, 4]"
        :key="n"
        class="chip"
        :class="{ 'chip-on': halfTurns === n }"
        :title="`${n} x 180° = ${n * 180}° of cap rotation`"
        @click="halfTurns = n; armed = false"
      >
        {{ n }}×180°
      </button>
      <button
        class="btn ml-auto"
        :class="armed ? 'btn-danger' : 'btn-primary'"
        :disabled="!canMove"
        @click="unscrew"
      >
        {{ armed ? "Confirm unscrew" : "Unscrew" }}
      </button>
    </div>

    <p class="mt-2 text-xs text-deck-400">
      Turns the cap <span class="num">{{ capDegrees }}</span>° in
      <span class="num">{{ halfTurns }}</span> bite<span v-if="halfTurns !== 1">s</span>:
      turn&nbsp;+180° gripped → open → turn&nbsp;−180° free → re-grip → turn again.
      Ends with the jaws <strong>open</strong> and the wrist back where it started, so the
      cap is left loose on the tube and the routine can be run again without walking J6
      toward its limit. Lifting the cap away is a separate action.
    </p>
    <p v-if="armed" class="mt-2 text-xs text-amber-300">
      Cap must already be gripped and the tube held. Click again to run.
    </p>
    <p v-if="state?.free_drive" class="mt-2 text-xs text-sky-300">
      Hand-guiding is on — switch it off before running the ratchet.
    </p>
  </section>
</template>
