import { ref, onMounted, onUnmounted } from "vue";
import { openSocket, type DeviceSummary } from "../api/client";

// Live fleet state via the /ws/state websocket.
export function useFleet() {
  const instruments = ref<DeviceSummary[]>([]);
  const connected = ref(false);
  let ws: WebSocket | null = null;

  onMounted(() => {
    ws = openSocket<{ instruments: DeviceSummary[] }>("/ws/state", (msg) => {
      instruments.value = msg.instruments;
      connected.value = true;
    });
    ws.onclose = () => (connected.value = false);
  });

  onUnmounted(() => ws?.close());

  return { instruments, connected };
}
