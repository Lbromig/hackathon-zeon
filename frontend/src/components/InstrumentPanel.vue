<script setup lang="ts">
import type { DeviceSummary } from "../api/client";
import { cameraFrameUrl } from "../api/client";

defineProps<{ device: DeviceSummary }>();

const dot = (state: string) =>
  state === "connected" ? "#22c55e" : state === "error" ? "#ef4444" : "#6b7a90";
</script>

<template>
  <div class="panel">
    <header>
      <span class="dot" :style="{ background: dot(device.state) }" />
      <strong>{{ device.name }}</strong>
      <span class="kind">{{ device.kind }}</span>
    </header>
    <img
      v-if="device.kind === 'camera' && device.state === 'connected'"
      :src="cameraFrameUrl(device.id)"
      class="feed"
      alt="camera feed"
    />
    <pre class="status">{{ JSON.stringify(device.status, null, 1) }}</pre>
  </div>
</template>

<style scoped>
.panel { background: #14203a; border: 1px solid #24365c; border-radius: 12px; padding: 12px; color: #cad6ec; }
header { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.dot { width: 10px; height: 10px; border-radius: 50%; }
.kind { margin-left: auto; font-size: 12px; color: #6b7a90; text-transform: uppercase; }
.feed { width: 100%; border-radius: 8px; margin-bottom: 8px; }
.status { font-size: 11px; color: #9fb0cc; margin: 0; white-space: pre-wrap; }
</style>
