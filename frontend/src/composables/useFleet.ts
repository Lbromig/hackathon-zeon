import { ref, onMounted, onUnmounted } from "vue";
import { openSocket, type DeviceSummary } from "../api/client";
import type { CameraFrameState } from "../api/cameras";

interface StateMessage {
  instruments: DeviceSummary[];
  cameras?: Record<string, CameraFrameState>;
}

// Live fleet state via the /ws/state websocket. The same message carries the
// per-camera detections, so the overlay costs no extra connection.
export function useFleet() {
  const instruments = ref<DeviceSummary[]>([]);
  const cameras = ref<Record<string, CameraFrameState>>({});
  const connected = ref(false);
  let ws: WebSocket | null = null;

  onMounted(() => {
    ws = openSocket<StateMessage>("/ws/state", (msg) => {
      instruments.value = msg.instruments;
      cameras.value = msg.cameras ?? {};
      connected.value = true;
    });
    ws.onclose = () => (connected.value = false);
  });

  onUnmounted(() => ws?.close());

  return { instruments, cameras, connected };
}
