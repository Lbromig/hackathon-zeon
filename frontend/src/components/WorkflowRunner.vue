<script setup lang="ts">
import { useWorkflow } from "../composables/useWorkflow";

const { events, running, run } = useWorkflow();

const color = (phase: string) =>
  phase === "passed" ? "#22c55e"
  : phase === "failed" ? "#ef4444"
  : phase === "retrying" ? "#f59e0b"
  : "#38bdf8";
</script>

<template>
  <div class="runner">
    <button :disabled="running" @click="run">
      {{ running ? "Running…" : "Run uncap → aspirate" }}
    </button>
    <ul>
      <li v-for="(e, i) in events" :key="i">
        <span class="phase" :style="{ color: color(e.phase) }">{{ e.phase }}</span>
        <span v-if="e.step" class="step">{{ e.step }}</span>
        <span v-if="e.attempt" class="attempt">#{{ e.attempt }}</span>
        <span v-if="e.verification" class="detail">{{ e.verification.detail }}</span>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.runner { background: #14203a; border: 1px solid #24365c; border-radius: 12px; padding: 16px; color: #cad6ec; }
button { background: #2e6bff; color: #fff; border: 0; border-radius: 8px; padding: 10px 16px; font-weight: 600; cursor: pointer; }
button:disabled { opacity: 0.6; cursor: default; }
ul { list-style: none; padding: 0; margin: 12px 0 0; font-size: 13px; }
li { display: flex; gap: 8px; padding: 4px 0; border-bottom: 1px solid #1e2c4a; }
.phase { font-weight: 700; text-transform: uppercase; min-width: 84px; }
.step { font-weight: 600; }
.attempt { color: #6b7a90; }
.detail { color: #9fb0cc; margin-left: auto; }
</style>
