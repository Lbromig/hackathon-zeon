<script setup lang="ts">
// A state as a glyph **and** a word.
//
// Colour is secondary on purpose: a red/green-only encoding is unreadable for a meaningful
// fraction of operators, so the glyph carries the meaning and the colour reinforces it. The
// word is always in the accessible name, and visible next to the glyph wherever there is room.
import { computed } from "vue";
import type { ActionState } from "../../api/engine";

const props = defineProps<{
  state: ActionState;
  /** A `planned` row in a finished run was never reached — a different fact from "pending". */
  notReached?: boolean;
  showText?: boolean;
}>();

const GLYPHS: Record<ActionState, { glyph: string; word: string; class: string }> = {
  planned: { glyph: "○", word: "planned", class: "text-deck-400" },
  running: { glyph: "◐", word: "running", class: "text-blue-300" },
  complete: { glyph: "✓", word: "complete", class: "text-emerald-400" },
  failed: { glyph: "✗", word: "failed", class: "text-red-400" },
  skipped: { glyph: "⊘", word: "skipped", class: "text-deck-400" },
  aborted: { glyph: "⊗", word: "aborted", class: "text-amber-400" },
};

const shown = computed(() => {
  const base = GLYPHS[props.state] ?? GLYPHS.planned;
  if (props.notReached && props.state === "planned") {
    return { ...base, word: "not reached", class: "text-deck-600" };
  }
  return base;
});
</script>

<template>
  <span class="inline-flex items-center gap-1.5" :class="shown.class">
    <span class="text-base leading-none" aria-hidden="true">{{ shown.glyph }}</span>
    <span v-if="showText" class="text-xs">{{ shown.word }}</span>
    <span v-else class="sr-only">{{ shown.word }}</span>
  </span>
</template>

<style scoped>
/* Tailwind 4 ships sr-only, but this component is used in tables where the utility layer
   ordering has bitten us before — keep it local and certain. */
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border-width: 0;
}
</style>
