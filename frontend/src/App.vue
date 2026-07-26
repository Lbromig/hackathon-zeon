<script setup lang="ts">
import { ref } from "vue";
import { useFleet } from "./composables/useFleet";
import { connectAll } from "./api/client";
import TeachPanel from "./components/teach/TeachPanel.vue";
import CameraTab from "./components/cameras/CameraTab.vue";

type Tab = "fleet" | "teach" | "cameras";

const { instruments, cameras, connected } = useFleet();
// Remembered across reloads — during bring-up you live in one tab for hours.
const tab = ref<Tab>((localStorage.getItem("tab") as Tab) ?? "fleet");

function select(next: Tab) {
  tab.value = next;
  localStorage.setItem("tab", next);
}
</script>

<template>
  <div class="mx-auto max-w-[1400px] p-6">
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

    <nav class="mt-5 flex gap-1 border-b border-deck-600">
      <button
        v-for="t in (['fleet', 'teach', 'cameras'] as Tab[])"
        :key="t"
        class="-mb-px border-b-2 px-4 py-2 text-sm font-semibold capitalize transition-colors"
        :class="
          tab === t
            ? 'border-blue-500 text-white'
            : 'border-transparent text-deck-400 hover:text-deck-100'
        "
        @click="select(t)"
      >
        {{ t }}
      </button>
    </nav>

    <main class="mt-5">
      <!-- Placeholder. S6 replaces this shell with a router + the workflow tab
           (R-UI-1); the device cards' raw-status-JSON view and the "Run uncap ->
           aspirate" button both went with the executor they drove. -->
      <div v-if="tab === 'fleet'" class="card">
        <h2 class="card-title">Fleet</h2>
        <ul class="mt-2 space-y-1 text-sm text-deck-100">
          <li v-for="d in instruments" :key="d.id" class="num">
            {{ d.id }} · {{ d.kind }} · {{ d.state }}
          </li>
        </ul>
      </div>

      <!-- v-if, not v-show: unmounting stops the teach poller when you leave the tab,
           and drops the MJPEG connections so the backend can release the cameras -->
      <TeachPanel v-else-if="tab === 'teach'" />
      <CameraTab v-else-if="tab === 'cameras'" :cameras="cameras" />
    </main>
  </div>
</template>
