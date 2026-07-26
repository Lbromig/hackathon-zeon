<script setup lang="ts">
// The inline, per-kind summary: the one fact that matters for this row's kind (§3.2).
// The logic — and the note on every value that is derived rather than read — is in
// `summarize.ts`; this only renders the bits.
import { computed } from "vue";
import { TONE_CLASS, summarize } from "./summarize";
import { loopIterationsOf, previousMagnitude, useEngine, type ChainRow } from "../../stores/engine";

const props = defineProps<{ item: ChainRow }>();
const { state } = useEngine();

const bits = computed(() =>
  summarize(props.item, {
    runState: state.runState,
    previousMagnitude,
    loopIterations: loopIterationsOf,
  }));
</script>

<template>
  <span class="flex flex-wrap items-center gap-x-2.5 gap-y-0.5">
    <span
      v-for="(bit, i) in bits"
      :key="i"
      class="num text-xs"
      :class="TONE_CLASS[bit.tone ?? 'muted']"
      :title="bit.title"
    >
      {{ bit.text }}
    </span>
  </span>
</template>
