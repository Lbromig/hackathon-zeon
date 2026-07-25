<script setup lang="ts">
// Gripper control. Only the parallel gripper has a commandable width; the Lite 6
// pneumatic and BIO grippers are open/close only, so the slider hides itself
// rather than sending a width the hardware can't honour.
import { computed, ref, watch } from "vue";
import { useTeach } from "../../composables/useTeach";

const { state, connected, sending, openGripper, closeGripper, setGripperWidth } = useTeach();

const gripper = computed(() => state.value?.gripper ?? null);
const supportsWidth = computed(() => gripper.value?.supports_width ?? false);
const width = ref(0);
const dragging = ref(false);

// Follow the hardware unless the operator is mid-drag.
watch(
  () => gripper.value?.width,
  (w) => {
    if (!dragging.value && typeof w === "number") width.value = w;
  },
  { immediate: true },
);

const percent = computed(() => {
  const g = gripper.value;
  if (!g || !g.supports_width || g.width === null) return null;
  const span = g.max_width - g.min_width;
  return span > 0 ? ((g.width - g.min_width) / span) * 100 : null;
});

/** Counts mean nothing at the bench — show the physical opening too. */
function millimetres(counts: number): string | null {
  const g = gripper.value;
  if (!g?.stroke_m || g.max_width <= g.min_width) return null;
  const mm = ((counts - g.min_width) / (g.max_width - g.min_width)) * g.stroke_m * 1000;
  return `${mm.toFixed(1)} mm`;
}
</script>

<template>
  <section class="card">
    <div class="flex items-baseline justify-between">
      <h2 class="card-title mb-0">Gripper</h2>
      <span class="text-xs text-deck-400">
        {{ gripper?.kind ?? "—" }}
        <span v-if="gripper && !gripper.supports_width && gripper.kind !== 'unknown'">· open/close only</span>
      </span>
    </div>

    <div class="mt-2 flex gap-2">
      <button class="btn btn-primary flex-1" :disabled="!connected || sending" @click="openGripper">
        Open <span class="kbd ml-1">]</span>
      </button>
      <button class="btn flex-1" :disabled="!connected || sending" @click="closeGripper">
        Close <span class="kbd ml-1">[</span>
      </button>
    </div>

    <div v-if="supportsWidth && gripper" class="mt-4">
      <div class="flex items-baseline justify-between text-xs text-deck-400">
        <span>width <span class="text-deck-600">{{ gripper.units }}</span></span>
        <span class="num text-deck-100">
          {{ width }}
          <span v-if="millimetres(width)" class="text-deck-400">· {{ millimetres(width) }}</span>
        </span>
      </div>
      <input
        v-model.number="width"
        type="range"
        class="mt-1 w-full accent-blue-500"
        :min="gripper.min_width"
        :max="gripper.max_width"
        step="10"
        :disabled="!connected || sending"
        @pointerdown="dragging = true"
        @pointerup="dragging = false; setGripperWidth(width)"
        @keyup.enter="setGripperWidth(width)"
      />
      <div class="flex justify-between text-[10px] text-deck-600">
        <span>{{ gripper.min_width }} closed</span>
        <span>open {{ gripper.max_width }}</span>
      </div>
      <p v-if="percent !== null" class="mt-2 text-xs text-deck-400">
        currently <span class="num text-deck-100">{{ percent.toFixed(0) }}%</span> open
      </p>
    </div>

    <p v-else-if="gripper?.kind === 'unknown'" class="mt-3 text-xs text-deck-400">
      Gripper type resolves on connect (or set <code class="text-deck-200">gripper</code> in the
      fleet config).
    </p>
  </section>
</template>
