<script setup lang="ts">
// Per-joint jogging with a live angle readout, and a position rail when the
// arm's joint limits are configured (backend leaves them unset by default
// rather than publishing guessed ranges).
import { computed } from "vue";
import { useTeach } from "../../composables/useTeach";

const { arm, state, settings, canMove, jogJoint } = useTeach();

const axisCount = computed(() => arm.value?.axis_count ?? 6);
const limits = computed(() => arm.value?.limits.joints ?? null);

const angle = (i: number) => state.value?.joints?.[i] ?? null;

/** Position of the joint within its range, 0..1, for the rail. */
function fraction(i: number): number | null {
  const a = angle(i);
  const range = limits.value?.[i];
  if (a === null || !range) return null;
  const [lo, hi] = range;
  if (hi <= lo) return null;
  return Math.min(1, Math.max(0, (a - lo) / (hi - lo)));
}
</script>

<template>
  <section class="card">
    <div class="flex items-baseline justify-between">
      <h2 class="card-title mb-0">Joint jog</h2>
      <span class="text-xs text-deck-400">
        step <span class="num text-deck-100">{{ settings.angular }}</span>°
      </span>
    </div>

    <div class="mt-3 space-y-1.5">
      <div
        v-for="i in axisCount"
        :key="i"
        class="grid grid-cols-[2.5rem_1fr_auto] items-center gap-2 rounded-lg px-1 py-0.5 hover:bg-deck-700/40"
      >
        <span class="text-sm font-semibold text-deck-200">J{{ i }}</span>
        <div>
          <span class="num text-sm text-deck-100">
            {{ angle(i - 1) !== null ? angle(i - 1)!.toFixed(2) : "—" }}
            <span class="text-xs text-deck-400">°</span>
          </span>
          <div v-if="fraction(i - 1) !== null" class="mt-1 h-1 rounded-full bg-deck-900">
            <div
              class="h-1 rounded-full bg-blue-500/70"
              :style="{ width: `${(fraction(i - 1) ?? 0) * 100}%` }"
            />
          </div>
        </div>
        <span class="flex gap-1">
          <button class="btn w-11" :disabled="!canMove" @click="jogJoint(i - 1, -1)">−</button>
          <button class="btn w-11" :disabled="!canMove" @click="jogJoint(i - 1, 1)">+</button>
        </span>
      </div>
    </div>

    <p v-if="!limits" class="mt-3 text-xs text-deck-400">
      No joint limits configured — set <code class="text-deck-200">limits.joints</code> in the fleet
      config to show range rails. The controller enforces its own limits regardless.
    </p>
  </section>
</template>
