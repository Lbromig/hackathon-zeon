<script setup lang="ts">
// Cartesian jogging. One click = one increment; there is no press-and-hold
// continuous jog (that needs servo-streaming mode plus a deadman switch).
import { useTeach } from "../../composables/useTeach";

const { state, settings, canMove, jogCartesian } = useTeach();

const AXES = [
  { axis: "x", label: "X", unit: "mm", keys: "← →" },
  { axis: "y", label: "Y", unit: "mm", keys: "↑ ↓" },
  { axis: "z", label: "Z", unit: "mm", keys: "PgDn PgUp" },
  { axis: "roll", label: "Roll", unit: "°", keys: "" },
  { axis: "pitch", label: "Pitch", unit: "°", keys: "" },
  { axis: "yaw", label: "Yaw", unit: "°", keys: "Q E" },
] as const;

const linear = (axis: string) => ["x", "y", "z"].includes(axis);
const value = (axis: string) =>
  state.value?.pose ? (state.value.pose as Record<string, number>)[axis] : null;
const stepFor = (axis: string) => (linear(axis) ? settings.linear : settings.angular);
</script>

<template>
  <section class="card">
    <div class="flex items-baseline justify-between">
      <h2 class="card-title mb-0">Cartesian jog</h2>
      <span class="text-xs text-deck-400">
        step <span class="num text-deck-100">{{ settings.linear }}</span> mm ·
        <span class="num text-deck-100">{{ settings.angular }}</span>°
      </span>
    </div>

    <div class="mt-3 space-y-1.5">
      <div
        v-for="a in AXES"
        :key="a.axis"
        class="grid grid-cols-[2.5rem_1fr_auto] items-center gap-2 rounded-lg px-1 py-0.5 hover:bg-deck-700/40"
      >
        <span class="text-sm font-semibold text-deck-200">{{ a.label }}</span>
        <span class="num text-sm text-deck-100">
          {{ value(a.axis) !== null && value(a.axis) !== undefined ? value(a.axis)!.toFixed(2) : "—" }}
          <span class="text-xs text-deck-400">{{ a.unit }}</span>
          <span v-if="a.keys" class="kbd ml-2">{{ a.keys }}</span>
        </span>
        <span class="flex gap-1">
          <button
            class="btn w-11"
            :disabled="!canMove"
            :title="`${a.label} −${stepFor(a.axis)}${a.unit}`"
            @click="jogCartesian(a.axis, -1)"
          >
            −
          </button>
          <button
            class="btn w-11"
            :disabled="!canMove"
            :title="`${a.label} +${stepFor(a.axis)}${a.unit}`"
            @click="jogCartesian(a.axis, 1)"
          >
            +
          </button>
        </span>
      </div>
    </div>
  </section>
</template>
