<script setup lang="ts">
// PLACEHOLDER — written only to keep `npm run dev` compiling while the Workflow tab is
// still being built. router.ts imports this path, so its absence breaks the whole dev
// server, not just this tab. The agent building the real view owns this file and will
// replace it; if you are reading this in a finished tree, the replacement did not happen.
import RealityBanner from "../components/workflow/RealityBanner.vue";
</script>

<template>
  <div class="space-y-4">
    <RealityBanner />
    <div class="rounded border border-amber-500/40 bg-amber-500/5 p-4 text-sm">
      <p class="font-medium text-amber-300">Workflow tab under construction</p>
      <p class="mt-1 text-slate-400">
        The plan chain, run controls and inject panel are being wired up. The other tabs work
        normally.
      </p>
    </div>
  </div>
</template>
