<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { getPreflight, type PreflightResult } from "../api/teach";
import type { WorkflowEvent } from "../api/client";
import { useWorkflow } from "../composables/useWorkflow";

const props = withDefaults(defineProps<{
  available?: boolean;
  unavailableReason?: string;
}>(), {
  available: true,
  unavailableReason: "Connect the motion fleet before starting.",
});

const { events, running, run } = useWorkflow();
const posePreflight = ref<PreflightResult | null>(null);
const posePreflightLoading = ref(true);
const posePreflightError = ref("");
const plan = [
  { key: "uncap", number: "01", label: "Cooperative uncap", verifier: "CAP REMOVED" },
  { key: "transport", number: "02", label: "Transport open tube", verifier: "GRASP SECURE" },
  { key: "present", number: "03", label: "Present under tip", verifier: "TUBE ALIGNED" },
  { key: "aspirate", number: "04", label: "Aspirate 100 μL", verifier: "VOLUME OK" },
];

const latest = computed(() => events.value[events.value.length - 1]);
const failed = computed(() =>
  events.value.some((event) => event.phase === "failed" || event.phase === "escalated"),
);
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
const runPreflight = computed(() =>
  [...events.value].reverse().find((event) => event.step === "preflight"),
);
const poseTotal = computed(() => posePreflight.value?.required.length ?? 0);
const poseTaught = computed(
  () => posePreflight.value?.required.filter((pose) => pose.taught).length ?? 0,
);
const poseReady = computed(() => posePreflight.value?.ok === true);
const runAvailable = computed(
  () => props.available && poseReady.value && !posePreflightLoading.value,
);
const runBlocker = computed(() => {
  if (!props.available) return props.unavailableReason;
  if (posePreflightLoading.value) return "Checking taught poses and workflow configuration.";
  if (posePreflightError.value) return "Pose preflight unavailable: " + posePreflightError.value;
  if (!poseReady.value) {
    return posePreflight.value?.problems[0] ?? "Required workflow poses are incomplete.";
  }
  return "";
});
const headline = computed(() => {
  if (failed.value) return "Workflow needs help";
  if (completed.value) return "Sequence complete";
  if (!latest.value && posePreflightLoading.value) return "Checking pose library";
  if (!latest.value && !poseReady.value) return "Pose library incomplete";
  if (!latest.value) return "Physical run staged";
  if (latest.value.phase === "verifying") return "Verifying " + latest.value.step;
  if (latest.value.phase === "retrying") return "Retrying " + latest.value.step;
  if (latest.value.step) return "Running " + latest.value.step;
  return "Workflow active";
});
const helper = computed(() =>
  latest.value?.verification?.detail
    ?? latest.value?.detail
    ?? (runBlocker.value
      || "Opening the workflow stream starts real motion after backend preflight."),
);
const recentEvents = computed(() => events.value.slice(-4).reverse());

function stepEvent(step: string) {
  return [...events.value].reverse().find((item) => item.step === step);
}

function stepState(step: string) {
  const event = stepEvent(step);
  if (!event) return "waiting";
  if (event.phase === "passed") return "complete";
  if (event.phase === "failed" || event.phase === "escalated") return "failed";
  return ["started", "verifying", "retrying"].includes(event.phase) ? "active" : "waiting";
}

function verificationData(event: WorkflowEvent | undefined) {
  const data = event?.verification?.data;
  if (!data) return "";
  return Object.entries(data)
    .filter(([, value]) => ["string", "number", "boolean"].includes(typeof value))
    .slice(0, 2)
    .map(([key, value]) => key.replace(/_/g, " ").toUpperCase() + " " + String(value))
    .join(" · ");
}

function stepEvidence(step: string, fallback: string) {
  const event = stepEvent(step);
  if (!event) return fallback;
  const parts: string[] = [];
  if (event.attempt && event.attempt > 1) parts.push("TRY " + event.attempt);
  if (typeof event.verification?.confidence === "number") {
    parts.push(Math.round(event.verification.confidence * 100) + "% CONF");
  }
  const data = verificationData(event);
  if (data) parts.push(data);
  if (event.verification?.detail && !data) parts.push(event.verification.detail);
  return parts.join(" · ") || fallback;
}

function eventSummary(event: WorkflowEvent) {
  const parts: string[] = [];
  if (event.attempt) parts.push("TRY " + event.attempt);
  if (typeof event.verification?.confidence === "number") {
    parts.push(Math.round(event.verification.confidence * 100) + "%");
  }
  const data = verificationData(event);
  if (data) parts.push(data);
  const detail = event.verification?.detail ?? event.detail ?? event.capability;
  if (detail) parts.push(detail);
  return parts.join(" · ");
}

async function refreshPreflight() {
  posePreflightLoading.value = true;
  posePreflightError.value = "";
  try {
    posePreflight.value = await getPreflight();
  } catch (error) {
    posePreflight.value = null;
    posePreflightError.value = error instanceof Error ? error.message : String(error);
  } finally {
    posePreflightLoading.value = false;
  }
}

async function startRun() {
  await refreshPreflight();
  if (props.available && posePreflight.value?.ok) run();
}

onMounted(refreshPreflight);
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
          <small>{{ stepEvidence(step.key, step.verifier) }}</small>
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
        <small>{{ eventSummary(event) }}</small>
      </p>
    </div>

    <div class="run-actions">
      <button
        class="execute-button"
        :disabled="running || !runAvailable"
        :title="runAvailable ? 'Starts the real backend workflow after repeated server preflight.' : runBlocker"
        @click="startRun"
      >
        <span>{{ completed || failed ? "Run workflow again" : "Run physical workflow" }}</span>
        <b>↗</b>
      </button>
      <div
        class="preflight-state"
        :class="{
          pass: poseReady || runPreflight?.phase === 'passed',
          fail: (!posePreflightLoading && posePreflight !== null && !poseReady)
            || runPreflight?.phase === 'failed',
        }"
        :title="runBlocker || 'Pose and configuration preflight passed.'"
      >
        <span>POSES / CONFIG</span>
        <strong>
          {{ posePreflightLoading
            ? "CHECKING"
            : posePreflight
              ? poseTaught + " / " + poseTotal
              : "UNAVAILABLE" }}
        </strong>
      </div>
    </div>

    <p class="motion-disclaimer">
      No browser stop is shown: closing this stream cannot cancel hardware motion. Use the
      device E-stops in Teach and OT-One controls.
    </p>
  </article>
</template>
