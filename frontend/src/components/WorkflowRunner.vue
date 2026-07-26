<script setup lang="ts">
import { computed } from "vue";
import { useWorkflow } from "../composables/useWorkflow";

const props = withDefaults(defineProps<{
  available?: boolean;
  unavailableReason?: string;
}>(), {
  available: true,
  unavailableReason: "Connect the motion fleet before starting.",
});

const { events, running, run } = useWorkflow();
const plan = [
  { key: "uncap", number: "01", label: "Cooperative uncap", verifier: "CAP REMOVED" },
  { key: "transport", number: "02", label: "Transport open tube", verifier: "GRASP SECURE" },
  { key: "present", number: "03", label: "Present under tip", verifier: "TUBE ALIGNED" },
  { key: "aspirate", number: "04", label: "Aspirate 100 μL", verifier: "VOLUME OK" },
];

const latest = computed(() => events.value[events.value.length - 1]);
const failed = computed(() => events.value.some((event) => event.phase === "failed"));
const passedSteps = computed(
  () => plan.filter((step) =>
    events.value.some((event) => event.step === step.key && event.phase === "passed"),
  ).length,
);
const completed = computed(
  () => latest.value?.phase === "done" && !failed.value && passedSteps.value === plan.length,
);
const progress = computed(() =>
  completed.value ? 100 : Math.round((passedSteps.value / plan.length) * 100),
);
const preflight = computed(() =>
  [...events.value].reverse().find((event) => event.step === "preflight"),
);
const headline = computed(() => {
  if (failed.value) return "Workflow needs help";
  if (completed.value) return "Sequence complete";
  if (!latest.value) return "Physical run staged";
  if (latest.value.phase === "verifying") return "Verifying " + latest.value.step;
  if (latest.value.phase === "retrying") return "Retrying " + latest.value.step;
  if (latest.value.step) return "Running " + latest.value.step;
  return "Workflow active";
});
const helper = computed(() =>
  latest.value?.verification?.detail
    ?? latest.value?.detail
    ?? (props.available
      ? "Opening the workflow stream starts real motion after backend preflight."
      : props.unavailableReason),
);
const recentEvents = computed(() => events.value.slice(-4).reverse());

function stepState(step: string) {
  const event = [...events.value].reverse().find((item) => item.step === step);
  if (!event) return "waiting";
  if (event.phase === "passed") return "complete";
  if (event.phase === "failed") return "failed";
  return ["started", "verifying", "retrying"].includes(event.phase) ? "active" : "waiting";
}
</script>

<template>
  <article class="zeon-panel protocol-panel">
    <div class="panel-heading">
      <div>
        <span class="kicker">WORKFLOW · PHYSICAL</span>
        <h2>Uncap <b>→</b> Aspirate</h2>
      </div>
      <span
        class="run-state"
        :class="{ live: running, failed, complete: completed }"
      >
        {{ failed ? "HELP" : completed ? "DONE" : running ? "RUNNING" : "STAGED" }}
      </span>
    </div>

    <div class="protocol-summary">
      <div class="status-orb" :class="{ live: running, failed }" aria-hidden="true">
        <span />
      </div>
      <div>
        <strong>{{ headline }}</strong>
        <p>{{ helper }}</p>
      </div>
    </div>

    <div class="protocol-track" :aria-label="'Workflow progress ' + progress + '%'">
      <span :style="{ width: progress + '%' }" />
    </div>

    <ol class="step-list">
      <li
        v-for="step in plan"
        :key="step.key"
        :class="stepState(step.key)"
      >
        <span>{{ step.number }}</span>
        <span>
          <strong>{{ step.label }}</strong>
          <small>{{ step.verifier }}</small>
        </span>
        <i>
          {{ stepState(step.key) === "complete"
            ? "PASS"
            : stepState(step.key) === "failed"
              ? "FAIL"
              : stepState(step.key) === "active"
                ? "LIVE"
                : "WAIT" }}
        </i>
      </li>
    </ol>

    <div v-if="events.length" class="workflow-events" aria-live="polite">
      <p v-for="(event, index) in recentEvents" :key="index">
        <span>{{ event.phase }}</span>
        <strong>{{ event.step ?? "workflow" }}</strong>
        <small>{{ event.verification?.detail ?? event.detail ?? event.capability ?? "" }}</small>
      </p>
    </div>

    <div class="run-actions">
      <button
        class="execute-button"
        :disabled="running || !available"
        :title="available ? 'Starts the real backend workflow after preflight.' : unavailableReason"
        @click="run"
      >
        <span>{{ completed || failed ? "Run workflow again" : "Run physical workflow" }}</span>
        <b>↗</b>
      </button>
      <div class="preflight-state" :class="{ pass: preflight?.phase === 'passed', fail: preflight?.phase === 'failed' }">
        <span>POSE PREFLIGHT</span>
        <strong>{{ preflight?.phase?.toUpperCase() ?? "ON START" }}</strong>
      </div>
    </div>

    <p class="motion-disclaimer">
      No browser stop is shown: closing this stream cannot cancel hardware motion. Use the
      device E-stops in Teach and OT-One controls.
    </p>
  </article>
</template>
