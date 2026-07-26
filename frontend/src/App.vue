<script setup lang="ts">
// App shell: the reality banner, the four tabs, and the two long-lived sockets.
//
// The engine socket is opened here rather than in the Workflow view, for two reasons: the
// reality banner has to stay truthful on every tab, and a run must keep streaming while the
// operator is watching a camera — remounting the store on tab change would drop events and
// force a resync each time.
import { onMounted, onUnmounted, provide } from "vue";
import { RouterLink, RouterView } from "vue-router";
import { useFleet } from "./composables/useFleet";
import { connectAll } from "./api/client";
import { CamerasKey } from "./stores/fleet";
import { connectEngine, disconnectEngine } from "./stores/engine";
import RealityBanner from "./components/workflow/RealityBanner.vue";
import { routes } from "./router";

const { cameras, connected } = useFleet();
provide(CamerasKey, cameras);

const tabs = routes.filter((r) => typeof r.meta?.label === "string");

onMounted(connectEngine);
onUnmounted(disconnectEngine);
</script>

<template>
  <div class="mx-auto max-w-[1600px] p-6">
    <header class="flex items-center gap-3">
      <h1 class="text-xl font-semibold text-white">hackathon-zeon · control</h1>
      <span
        class="text-xs uppercase tracking-wider"
        :class="connected ? 'text-emerald-500' : 'text-deck-400'"
      >
        {{ connected ? "live" : "offline" }}
      </span>
      <button class="btn btn-primary ml-auto" @click="connectAll">Connect all</button>
    </header>

    <!-- Persistent, on every tab: with simulation the default (D29/R-SIM-8) a simulated run
         must be impossible to mistake for a real one, and a banner that only exists on the
         workflow tab is a banner the operator can navigate away from. -->
    <RealityBanner class="mt-4" />

    <nav class="mt-4 flex gap-1 border-b border-deck-600">
      <RouterLink
        v-for="t in tabs"
        :key="String(t.name)"
        :to="t.path"
        class="-mb-px border-b-2 px-4 py-2 text-sm font-semibold transition-colors"
        active-class="border-blue-500 text-white"
        exact-active-class="border-blue-500 text-white"
      >
        <span class="border-transparent">{{ t.meta?.label }}</span>
      </RouterLink>
    </nav>

    <main class="mt-5">
      <RouterView />
    </main>
  </div>
</template>

<style scoped>
/* RouterLink's default (inactive) look; `active-class` overrides the border and colour. */
nav a {
  border-color: transparent;
  color: var(--color-deck-400);
}
nav a:hover {
  color: var(--color-deck-100);
}
</style>
