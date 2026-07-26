<script setup lang="ts">
import { computed, ref } from "vue";
import { useFleet } from "./composables/useFleet";
import { connectAll } from "./api/client";
import InstrumentPanel from "./components/InstrumentPanel.vue";
import WorkflowRunner from "./components/WorkflowRunner.vue";
import OpentronsJog from "./components/OpentronsJog.vue";
import TeachPanel from "./components/teach/TeachPanel.vue";
import CameraPreflight from "./components/CameraPreflight.vue";
import CameraTab from "./components/cameras/CameraTab.vue";
import WorldMapTab from "./components/worldmap/WorldMapTab.vue";

type Tab = "fleet" | "teach" | "cameras" | "world";

const { instruments, cameras, connected } = useFleet();
// Remembered across reloads — during bring-up you live in one tab for hours.
const tab = ref<Tab>((localStorage.getItem("tab") as Tab) ?? "fleet");

function select(next: Tab) {
  tab.value = next;
  localStorage.setItem("tab", next);
}

// Jog pads for every liquid handler in the fleet. Driven off the fleet list
// rather than a hard-coded id so it appears for whatever is actually connected.
const liquidHandlers = computed(() =>
  instruments.value.filter((d) => d.kind === "liquid_handler"),
);
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
        v-for="t in (['fleet', 'teach', 'cameras', 'world'] as Tab[])"
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
      <div v-if="tab === 'fleet'" class="grid items-start gap-5 lg:grid-cols-[2fr_1fr]">
        <section class="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          <InstrumentPanel v-for="d in instruments" :key="d.id" :device="d" />
        </section>
        <aside class="grid gap-4">
          <OpentronsJog v-for="d in liquidHandlers" :key="d.id" :device-id="d.id" />
          <CameraPreflight />
          <WorkflowRunner />
        </aside>
      </div>

      <!-- v-if, not v-show: unmounting stops the teach poller when you leave the tab,
           and drops the MJPEG connections so the backend can release the cameras -->
      <TeachPanel v-else-if="tab === 'teach'" />
      <CameraTab v-else-if="tab === 'cameras'" :cameras="cameras" />
      <WorldMapTab v-else-if="tab === 'world'" />
    </main>
  </div>
</template>
