import { onUnmounted, ref } from "vue";
import { openSocket, type WorkflowEvent } from "../api/client";

// Runs the uncap->aspirate workflow and collects streamed step/verify/retry events.
export function useWorkflow() {
  const events = ref<WorkflowEvent[]>([]);
  const running = ref(false);
  let ws: WebSocket | null = null;

  function run() {
    events.value = [];
    running.value = true;
    ws = openSocket<WorkflowEvent>("/ws/workflow", (e) => {
      events.value.push(e);
      if (["done", "failed", "escalated"].includes(e.phase)) running.value = false;
    });
    ws.onclose = () => (running.value = false);
  }

  function stop() {
    ws?.close();
    ws = null;
    running.value = false;
  }

  onUnmounted(stop);

  return { events, running, run, stop };
}
