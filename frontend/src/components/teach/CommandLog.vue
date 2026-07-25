<script setup lang="ts">
// Every command, its result and how long it took. During bring-up the latency
// column is the quickest tell that an arm is on a bad network path.
import { useTeach } from "../../composables/useTeach";

const { log } = useTeach();
</script>

<template>
  <section class="card">
    <div class="flex items-baseline justify-between">
      <h2 class="card-title mb-0">Command log</h2>
      <button v-if="log.length" class="btn btn-sm btn-ghost" @click="log.length = 0">clear</button>
    </div>

    <ul class="mt-2 max-h-72 overflow-y-auto text-xs">
      <li
        v-for="e in log"
        :key="e.id"
        class="flex items-baseline gap-2 border-b border-deck-700 py-1.5 last:border-0"
      >
        <span class="num shrink-0 text-deck-600">{{ e.at }}</span>
        <span class="shrink-0" :class="e.ok ? 'text-emerald-400' : 'text-red-400'">
          {{ e.ok ? "✓" : "✕" }}
        </span>
        <span class="shrink-0 font-semibold text-deck-100">{{ e.label }}</span>
        <span class="min-w-0 flex-1 truncate" :class="e.ok ? 'text-deck-400' : 'text-red-300'">
          {{ e.detail }}
        </span>
        <span class="num shrink-0 text-deck-600">{{ e.ms }} ms</span>
      </li>
    </ul>
    <p v-if="!log.length" class="mt-2 text-xs text-deck-400">No commands yet.</p>
  </section>
</template>
