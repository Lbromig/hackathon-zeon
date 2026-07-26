<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { DeviceSummary } from "../api/client";
import WorkflowRunner from "./WorkflowRunner.vue";
import OpentronsJog from "./OpentronsJog.vue";
import CameraPreflight from "./CameraPreflight.vue";

const props = defineProps<{
  instruments: DeviceSummary[];
  connected: boolean;
}>();

const emit = defineEmits<{
  "open-tab": [tab: "teach" | "cameras"];
}>();

type Metric = [string, string];

const expectedFleet: DeviceSummary[] = [
  { id: "left", name: "Left arm", kind: "arm", model: "xArm Lite 6", vendor: "UFACTORY", state: "disconnected", status: {} },
  { id: "right", name: "Right arm", kind: "arm", model: "xArm Lite 6", vendor: "UFACTORY", state: "disconnected", status: {} },
  { id: "ot", name: "Opentrons (OT-One)", kind: "liquid_handler", model: "OT-One", vendor: "Opentrons", state: "disconnected", status: {} },
  { id: "gripper_cam", name: "Gripper (on-arm) cam", kind: "camera", model: "RealSense RGB-D", vendor: "Intel", state: "disconnected", status: { depth: true } },
  { id: "overview_cam", name: "Overview cam", kind: "camera", model: "RealSense RGB-D", vendor: "Intel", state: "disconnected", status: {} },
  { id: "handover_cam", name: "Handover cam", kind: "camera", model: "RealSense RGB-D", vendor: "Intel", state: "disconnected", status: {} },
];

const displayDevices = computed(() =>
  props.instruments.length ? props.instruments : expectedFleet,
);
const selectedId = ref("left");
const selected = computed(
  () => displayDevices.value.find((item) => item.id === selectedId.value) ?? displayDevices.value[0],
);
const liquidHandlers = computed(() =>
  props.instruments.filter((item) => item.kind === "liquid_handler"),
);
const onlineCount = computed(
  () => displayDevices.value.filter((item) => item.state === "connected").length,
);
const faultCount = computed(
  () => displayDevices.value.filter(deviceHasFault).length,
);
const offlineCount = computed(
  () => displayDevices.value.filter((item) => item.state === "disconnected").length,
);
const attentionLabel = computed(() => {
  const parts: string[] = [];
  if (faultCount.value) parts.push(faultCount.value + " faults");
  if (offlineCount.value) parts.push(offlineCount.value + " offline");
  return parts.length ? parts.join(" · ") : "Coverage nominal";
});
const motionBlockers = computed(() => {
  const blockers: string[] = [];
  if (!props.connected) blockers.push("backend state stream is offline");
  for (const id of ["left", "right"]) {
    const arm = device(id);
    if (!arm || arm.state !== "connected") {
      blockers.push(id + " arm is disconnected");
      continue;
    }
    const mode = arm.status.mode;
    const armState = arm.status.arm_state;
    const errorCode = arm.status.error_code;
    if (mode !== 0) blockers.push(id + " arm is not in position mode");
    if (Number(armState) !== 2) blockers.push(id + " arm is not idle (state 2 required)");
    if (errorCode !== 0) blockers.push(id + " arm has an active or unknown error");
  }
  const ot = device("ot");
  if (!ot || ot.state !== "connected" || ot.status.connected !== true) {
    blockers.push("OT-One is disconnected");
  } else if (ot.status.reference_lost === true) {
    blockers.push("OT-One reference is lost");
  }
  return blockers;
});
const motionFleetReady = computed(() => motionBlockers.value.length === 0);
const workflowUnavailable = computed(() =>
  motionBlockers.value.length
    ? "Run blocked: " + motionBlockers.value.join("; ") + "."
    : "Backend pose preflight runs when the physical workflow starts.",
);

watch(displayDevices, (devices) => {
  if (!devices.some((item) => item.id === selectedId.value)) {
    selectedId.value = devices[0]?.id ?? "";
  }
});

function device(id: string) {
  return displayDevices.value.find((item) => item.id === id);
}

function tone(state: string) {
  return state === "connected" ? "online" : state === "error" ? "error" : "offline";
}

function deviceHasFault(item: DeviceSummary) {
  if (item.state === "error") return true;
  const errorCode = item.status.error_code;
  return typeof errorCode === "number" && errorCode !== 0;
}

function shortKind(kind: string) {
  if (kind === "arm") return "A";
  if (kind === "liquid_handler") return "OT";
  return "C";
}

function valueAt(name: string): unknown {
  return selected.value?.status?.[name];
}

function formatNumber(value: unknown, digits = 1) {
  return typeof value === "number"
    ? value.toFixed(digits)
    : value == null
      ? "—"
      : String(value);
}

function formatPose(value: unknown) {
  if (!value || typeof value !== "object") return "—";
  const pose = value as Record<string, unknown>;
  return ["x", "y", "z", "roll", "pitch", "yaw"]
    .map((key) => formatNumber(pose[key]))
    .join("  ");
}

function formatJoints(value: unknown) {
  if (!Array.isArray(value)) return "—";
  return value.map((joint) => formatNumber(joint)).join("  ");
}

const metrics = computed<Metric[]>(() => {
  const current = selected.value;
  if (!current) return [];
  if (current.kind === "arm") {
    const armMetrics: Metric[] = [
      ["MODE", formatNumber(valueAt("mode"), 0)],
      ["ARM STATE", formatNumber(valueAt("arm_state"), 0)],
      ["ERROR / WARN", formatNumber(valueAt("error_code"), 0) + " / " + formatNumber(valueAt("warn_code"), 0)],
      ["XYZ · RPY", formatPose(valueAt("pose"))],
      ["JOINTS · DEG", formatJoints(valueAt("joints"))],
      ["CONTROL", current.state === "connected" ? "TEACH AVAILABLE" : "NO LINK"],
    ];
    return armMetrics;
  }
  const entries: Metric[] = Object.entries(current.status ?? {})
    .slice(0, 4)
    .map(([key, value]) => [
      key.replace(/_/g, " ").toUpperCase(),
      Array.isArray(value)
        ? value.join("  ")
        : value && typeof value === "object"
          ? JSON.stringify(value)
          : String(value),
    ]);
  const deviceMetrics: Metric[] = [
    ["STATE", current.state.toUpperCase()],
    ["MODEL", current.model || "—"],
    ...entries,
  ];
  return deviceMetrics.slice(0, 6);
});

const jointBars = computed(() => {
  const joints = selected.value?.status.joints;
  if (!Array.isArray(joints)) return [];
  const bars: Array<{ label: string; value: string; height: number }> = [];
  joints.forEach((value, index) => {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return;
    const wrapped = ((numeric + 180) % 360 + 360) % 360 - 180;
    bars.push({
      label: "J" + (index + 1),
      value: numeric.toFixed(1) + "°",
      height: Math.max(8, Math.min(100, (Math.abs(wrapped) / 180) * 100)),
    });
  });
  return bars;
});
</script>

<template>
  <section class="control-grid">
    <WorkflowRunner
      :available="motionFleetReady"
      :unavailable-reason="workflowUnavailable"
    />

    <article class="zeon-panel workcell-panel">
      <div class="panel-heading">
        <div>
          <span class="kicker">LIVE WORKCELL</span>
          <h2>Handover workcell</h2>
        </div>
        <span class="mini-legend"><i /> {{ onlineCount }} / {{ displayDevices.length }} online</span>
      </div>

      <div class="workcell-map">
        <div class="map-grid" aria-hidden="true" />
        <div class="orbit orbit-one" aria-hidden="true" />
        <div class="orbit orbit-two" aria-hidden="true" />
        <div class="transfer-line line-left" aria-hidden="true" />
        <div class="transfer-line line-right" aria-hidden="true" />

        <button
          class="machine-node node-left"
          :class="[tone(device('left')?.state ?? 'offline'), { selected: selectedId === 'left' }]"
          @click="selectedId = 'left'"
        >
          <span class="node-code">L</span>
          <span>
            <strong>LEFT ARM</strong>
            <small>{{ (device("left")?.state ?? "offline").toUpperCase() }}</small>
          </span>
        </button>

        <button
          class="machine-node node-right"
          :class="[tone(device('right')?.state ?? 'offline'), { selected: selectedId === 'right' }]"
          @click="selectedId = 'right'"
        >
          <span class="node-code">R</span>
          <span>
            <strong>RIGHT ARM</strong>
            <small>{{ (device("right")?.state ?? "offline").toUpperCase() }}</small>
          </span>
        </button>

        <button
          class="machine-node node-ot"
          :class="[tone(device('ot')?.state ?? 'offline'), { selected: selectedId === 'ot' }]"
          @click="selectedId = 'ot'"
        >
          <span class="node-code ot-code">OT</span>
          <span>
            <strong>OT-ONE</strong>
            <small>{{ (device("ot")?.state ?? "offline").toUpperCase() }}</small>
          </span>
        </button>

        <div class="handover-core">
          <span />
          <strong>HANDOVER</strong>
          <small>VERIFY · RETRY</small>
        </div>

        <button
          class="fault-node fault-gripper"
          :class="{ quiet: device('gripper_cam')?.state === 'connected' }"
          :title="'Gripper camera · ' + (device('gripper_cam')?.state ?? 'offline')"
          aria-label="Select gripper camera"
          @click="selectedId = 'gripper_cam'"
        ><span>G</span><small>GRIP CAM</small></button>
        <button
          class="fault-node fault-overview"
          :class="{ quiet: device('overview_cam')?.state === 'connected' }"
          :title="'Overview camera · ' + (device('overview_cam')?.state ?? 'offline')"
          aria-label="Select overview camera"
          @click="selectedId = 'overview_cam'"
        ><span>O</span><small>OVERVIEW</small></button>
        <button
          class="fault-node fault-handover"
          :class="{ quiet: device('handover_cam')?.state === 'connected' }"
          :title="'Handover camera · ' + (device('handover_cam')?.state ?? 'offline')"
          aria-label="Select handover camera"
          @click="selectedId = 'handover_cam'"
        ><span>H</span><small>HANDOVER</small></button>
        <div class="map-axis axis-x">X +</div>
        <div class="map-axis axis-y">Y +</div>
      </div>

      <div class="map-caption">
        <span>PLAN · UNCAP / TRANSPORT / PRESENT / ASPIRATE</span>
        <button @click="emit('open-tab', 'cameras')">
          <i /> {{ attentionLabel }}
        </button>
      </div>
    </article>

    <aside class="zeon-panel fleet-panel">
      <div class="panel-heading">
        <div>
          <span class="kicker">FLEET · {{ String(displayDevices.length).padStart(2, "0") }}</span>
          <h2>Devices</h2>
        </div>
        <span class="fleet-score">{{ Math.round((onlineCount / displayDevices.length) * 100) }}%</span>
      </div>

      <div class="device-list">
        <button
          v-for="item in displayDevices"
          :key="item.id"
          class="device-row"
          :class="{ selected: selectedId === item.id }"
          @click="selectedId = item.id"
        >
          <span class="device-icon" :class="tone(item.state)">{{ shortKind(item.kind) }}</span>
          <span class="device-copy">
            <strong>{{ item.name }}</strong>
            <small>{{ item.model || item.kind }}</small>
          </span>
          <span class="device-state" :class="tone(item.state)">
            <i /> {{ item.state }}
          </span>
        </button>
      </div>
    </aside>

    <article v-if="selected" class="zeon-panel telemetry-panel">
      <div class="telemetry-head">
        <div>
          <span class="kicker">{{ selected.kind.toUpperCase() }} · {{ selected.id.toUpperCase() }}</span>
          <h2>{{ selected.name }} telemetry</h2>
        </div>
        <div class="selected-state" :class="tone(selected.state)">
          <i /> {{ selected.state }}
        </div>
      </div>

      <div class="telemetry-body">
        <div class="metric-grid">
          <div v-for="metric in metrics" :key="metric[0]" class="metric">
            <span>{{ metric[0] }}</span>
            <strong>{{ metric[1] }}</strong>
          </div>
        </div>
        <div class="trace">
          <div class="trace-label">
            <span>JOINT MAGNITUDE · CURRENT</span>
            <b>{{ jointBars.length ? "LATEST SAMPLE" : "NO JOINT DATA" }}</b>
          </div>
          <div
            v-if="jointBars.length"
            class="trace-bars"
            :aria-label="jointBars.map((bar) => bar.label + ' ' + bar.value).join(', ')"
          >
            <i
              v-for="bar in jointBars"
              :key="bar.label"
              :style="{ height: bar.height + '%' }"
              :title="bar.label + ' · ' + bar.value"
            />
          </div>
          <div v-else class="trace-empty">Awaiting arm joint telemetry</div>
          <div class="trace-scale"><span>0°</span><span>90°</span><span>180°</span></div>
        </div>
      </div>
    </article>

    <article class="zeon-panel mission-panel">
      <div>
        <span class="kicker">SAFETY CONTRACT</span>
        <h2>Configuration checked before motion</h2>
        <p>
          The workflow checks device configuration and all taught poses before moving.
          Verification failures retry the current step, then stop for help.
        </p>
      </div>
      <button @click="emit('open-tab', 'teach')">Open teach controls <b>↗</b></button>
    </article>
  </section>

  <section class="operations-section">
    <div class="section-heading">
      <div>
        <span class="kicker">BENCH OPERATIONS</span>
        <h2>Bring-up controls</h2>
      </div>
      <p>Guarded hardware tools from Lukas’s integration branch.</p>
    </div>
    <div class="operations-grid">
      <OpentronsJog v-for="handler in liquidHandlers" :key="handler.id" :device-id="handler.id" />
      <CameraPreflight />
      <div v-if="!liquidHandlers.length" class="zeon-panel empty-operation">
        <span class="kicker">OT-ONE JOG</span>
        <strong>Waiting for fleet state</strong>
        <p>Relative jog controls appear only after the liquid handler is discovered.</p>
      </div>
    </div>
  </section>
</template>
