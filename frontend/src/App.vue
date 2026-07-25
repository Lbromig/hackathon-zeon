<script setup lang="ts">
import { useFleet } from "./composables/useFleet";
import { connectAll } from "./api/client";
import InstrumentPanel from "./components/InstrumentPanel.vue";
import WorkflowRunner from "./components/WorkflowRunner.vue";

const { instruments, connected } = useFleet();
</script>

<template>
  <div class="app">
    <header class="top">
      <h1>hackathon-zeon · control</h1>
      <span class="live" :class="{ on: connected }">{{ connected ? "live" : "offline" }}</span>
      <button @click="connectAll">Connect all</button>
    </header>

    <main>
      <section class="fleet">
        <InstrumentPanel v-for="d in instruments" :key="d.id" :device="d" />
      </section>
      <aside>
        <WorkflowRunner />
      </aside>
    </main>
  </div>
</template>

<style>
body { margin: 0; background: #0b1220; font-family: system-ui, sans-serif; }
.app { max-width: 1200px; margin: 0 auto; padding: 24px; }
.top { display: flex; align-items: center; gap: 12px; color: #fff; }
.top h1 { font-size: 20px; margin: 0; }
.live { font-size: 12px; color: #6b7a90; text-transform: uppercase; }
.live.on { color: #22c55e; }
.top button { margin-left: auto; background: #2e6bff; color: #fff; border: 0; border-radius: 8px; padding: 8px 14px; font-weight: 600; cursor: pointer; }
main { display: grid; grid-template-columns: 2fr 1fr; gap: 20px; margin-top: 20px; align-items: start; }
.fleet { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 16px; }
</style>
