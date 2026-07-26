<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useFleet } from "./composables/useFleet";
import { connectAll } from "./api/client";
import FleetControl from "./components/FleetControl.vue";
import TeachPanel from "./components/teach/TeachPanel.vue";
import CameraTab from "./components/cameras/CameraTab.vue";
import WorldMapTab from "./components/worldmap/WorldMapTab.vue";
import DemoBrandTab from "./components/DemoBrandTab.vue";

const TAB_ITEMS = [
  { id: "fleet", label: "Fleet", title: "Wet Lab Command" },
  { id: "teach", label: "Teach", title: "Motion Teach" },
  { id: "cameras", label: "Vision", title: "Vision Control" },
  { id: "world", label: "World", title: "World Model" },
  { id: "demo", label: "Demo", title: "Demo Reel" },
] as const;

type Tab = (typeof TAB_ITEMS)[number]["id"];

const { instruments, cameras, connected } = useFleet();
const storedTab = localStorage.getItem("tab");
const storedTabIsValid = TAB_ITEMS.some((item) => item.id === storedTab);
const tab = ref<Tab>(storedTabIsValid ? (storedTab as Tab) : "fleet");
const connecting = ref(false);
const connectMessage = ref("");
const localTime = ref("");
let clock: number | undefined;

const faultCount = computed(
  () =>
    instruments.value.filter((device) => {
      if (device.state === "error") return true;
      const errorCode = device.status.error_code;
      return typeof errorCode === "number" && errorCode !== 0;
    }).length,
);
const offlineCount = computed(
  () => instruments.value.filter((device) => device.state === "disconnected").length,
);

const tabTitle = computed(
  () => TAB_ITEMS.find((item) => item.id === tab.value)?.title ?? "Wet Lab Command",
);

function select(next: Tab) {
  tab.value = next;
  localStorage.setItem("tab", next);
}

async function connectFleet() {
  connecting.value = true;
  connectMessage.value = "";
  try {
    const results = await connectAll();
    const failures = Object.entries(results).filter(([, result]) => result !== "connected");
    connectMessage.value = failures.length
      ? failures.map(([id, result]) => id + ": " + result).join(" · ")
      : "Fleet connection commands completed.";
  } catch (error) {
    connectMessage.value = error instanceof Error ? error.message : String(error);
  } finally {
    connecting.value = false;
  }
}

function updateClock() {
  localTime.value = new Date().toLocaleTimeString([], { hour12: false });
}

onMounted(() => {
  updateClock();
  clock = window.setInterval(updateClock, 1000);
});

onUnmounted(() => window.clearInterval(clock));
</script>

<template>
  <div class="zeon-shell">
    <aside class="zeon-sidebar" aria-label="Primary navigation">
      <div class="zeon-mark" aria-label="Zeon">Z</div>

      <nav class="zeon-nav">
        <button
          v-for="(item, index) in TAB_ITEMS"
          :key="item.id"
          class="zeon-nav-item"
          :class="{ active: tab === item.id }"
          :aria-pressed="tab === item.id"
          @click="select(item.id)"
        >
          <span>0{{ index + 1 }}</span>
          <strong>{{ item.label }}</strong>
        </button>
      </nav>

      <div class="zeon-local">
        <span class="signal-bars" aria-hidden="true"><i /><i /><i /></span>
        <span>LOCAL</span>
      </div>
    </aside>

    <div class="zeon-workspace">
      <header class="zeon-topbar">
        <div class="zeon-title">
          <p>HACKATHON-ZEON · CONTROL</p>
          <h1>{{ tabTitle }}</h1>
        </div>

        <div class="zeon-top-actions">
          <template v-if="tab !== 'demo'">
            <div class="connection-chip" :class="{ live: connected }">
              <span />
              {{ connected ? "State stream live" : "Offline session" }}
            </div>
            <div class="alert-chip">
              <span>{{ faultCount }}</span>
              Faults · {{ offlineCount }} offline
            </div>
            <button
              class="connect-button"
              :disabled="connecting"
              title="Actively connects hardware, clears arm faults, enables servos, and enters position mode."
              @click="connectFleet"
            >
              {{ connecting ? "Connecting…" : "Connect all" }}
            </button>
          </template>
          <div class="local-clock">
            <span>LOCAL</span>
            <strong>{{ localTime }}</strong>
          </div>
        </div>
      </header>

      <p v-if="connectMessage && tab !== 'demo'" class="connection-message">
        {{ connectMessage }}
      </p>

      <main class="zeon-main">
        <FleetControl
          v-if="tab === 'fleet'"
          :instruments="instruments"
          :connected="connected"
          @open-tab="select"
        />
        <!-- Keep inactive surfaces unmounted so browser subscribers, Teach polling,
             and keyboard handlers stop; backend camera workers may stay warm. -->
        <TeachPanel v-else-if="tab === 'teach'" />
        <CameraTab v-else-if="tab === 'cameras'" :cameras="cameras" />
        <WorldMapTab v-else-if="tab === 'world'" />
        <DemoBrandTab v-else-if="tab === 'demo'" @open-tab="select" />
      </main>
    </div>
  </div>
</template>
