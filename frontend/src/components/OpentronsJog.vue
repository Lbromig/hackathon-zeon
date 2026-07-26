<script setup lang="ts">
// Jog pad for the OT-One, for teaching positions by hand.
//
// RELATIVE only. This machine has no working endstops, so there is no absolute
// datum to move to — see docs/OT_ONE_HARDWARE.md. That is why there is no
// coordinate readout for X: nothing measures it. Z shows depth below the last
// home, which is the only reference that exists, and it is only meaningful
// while `homed` is true.
//
// One click is one increment. There is no press-and-hold continuous jog: with
// no endstops and no current sensing a crash is invisible to software, so every
// move should be a deliberate, bounded act the operator is watching.
import { ref, computed, onMounted } from "vue";
import { jog, home, stop, getState, getLimits, type LhStatus } from "../api/opentrons";

const props = defineProps<{ deviceId: string }>();

const STEPS = [0.5, 1, 2, 5, 10] as const;
const step = ref<number>(2);
const busy = ref(false);
const status = ref<LhStatus | null>(null);
const maxStep = ref<number>(15);
const log = ref<{ t: string; msg: string; ok: boolean }[]>([]);

// Positive Z is DOWN on this unit, established by observation during bring-up.
// The UI labels the buttons by intent (Down/Up) so the operator never has to
// remember the sign convention.
// Y is joggable but not homeable: the Y fault is specific to homing's long
// endstop search, not to bounded relative moves. Verified on hardware.
const AXES = [
  { axis: "X", label: "X", minus: "−X", plus: "+X", hint: "gantry" },
  { axis: "Y", label: "Y", minus: "−Y", plus: "+Y", hint: "gantry · jog only, never home" },
  { axis: "Z", label: "Z", minus: "Up", plus: "Down", hint: "shared lift · +Z is DOWN" },
  { axis: "A", label: "A", minus: "Up", plus: "Down", hint: "right mount" },
  // Plungers. Capped far tighter than the gantry (3 mm vs 15 mm): travel is
  // short and one driven past its seal jams. Labelled by effect, not sign.
  { axis: "B", label: "B", minus: "Draw", plus: "Push", hint: "plunger · max 3 mm/step" },
  { axis: "C", label: "C", minus: "Draw", plus: "Push", hint: "plunger · max 3 mm/step" },
] as const;

// The plunger cap is smaller than the gantry cap, so large step sizes have to be
// disabled per-axis rather than globally.
const PLUNGER_AXES = ["B", "C"];
const MAX_PLUNGER_STEP = 3;

const canMove = computed(
  () => !busy.value && status.value?.connected === true && !status.value?.reference_lost,
);
const depth = computed(() =>
  typeof status.value?.z_below_datum_mm === "number"
    ? status.value.z_below_datum_mm.toFixed(2)
    : "—",
);

// A plunger refuses anything over its own cap, so disable the button rather than
// let the operator fire a request the backend will reject.
function stepTooBig(axis: string): boolean {
  return PLUNGER_AXES.includes(axis) && step.value > MAX_PLUNGER_STEP;
}

function note(msg: string, ok: boolean) {
  const t = new Date().toLocaleTimeString();
  log.value.unshift({ t, msg, ok });
  if (log.value.length > 40) log.value.pop();
}

async function refresh() {
  const r = await getState(props.deviceId);
  if (r.status) status.value = r.status;
}

async function doJog(axis: string, dir: 1 | -1) {
  if (!canMove.value) return;
  busy.value = true;
  const delta = dir * step.value;
  const r = await jog(props.deviceId, axis, delta);
  if (r.status) status.value = r.status;
  const dur = r.duration_s != null ? ` (${r.duration_s.toFixed(2)}s)` : "";
  note(`${axis} ${delta > 0 ? "+" : ""}${delta} mm${dur} — ${r.detail}`, r.ok);
  busy.value = false;
}

async function doHome() {
  busy.value = true;
  note("homing Z — drives to the top stop, takes 5-7 s", true);
  const r = await home(props.deviceId);
  if (r.status) status.value = r.status;
  const dur = r.duration_s != null ? ` (${r.duration_s.toFixed(2)}s)` : "";
  note(`home${dur} — ${r.detail}`, r.ok);
  busy.value = false;
}

async function doStop() {
  // Deliberately not gated on `busy`: an e-stop that waits for the move it is
  // interrupting would be useless.
  const r = await stop(props.deviceId);
  if (r.status) status.value = r.status;
  note(r.detail, r.ok);
  busy.value = false;
}

onMounted(async () => {
  const l = await getLimits(props.deviceId);
  if (l) maxStep.value = l.max_step_mm;
  await refresh();
});
</script>

<template>
  <section class="jog">
    <header>
      <h2>OT-One jog</h2>
      <button class="stop" @click="doStop" title="Cut motion immediately">STOP</button>
    </header>

    <p class="warn">
      Relative jogging only — no endstops on this machine, so there is no absolute
      datum. A crash is <strong>invisible</strong> to software; watch the hardware.
    </p>

    <div class="row status">
      <span :class="{ on: status?.connected }">
        {{ status?.connected ? "connected" : "disconnected" }}
      </span>
      <span :class="{ on: status?.homed }">{{ status?.homed ? "homed" : "not homed" }}</span>
      <span v-if="status?.reference_lost" class="bad">reference lost — home again</span>
      <span class="depth">Z depth below datum: <b>{{ depth }}</b> mm</span>
    </div>

    <div class="row steps">
      <span>step</span>
      <button
        v-for="s in STEPS"
        :key="s"
        :class="{ sel: step === s }"
        :disabled="s > maxStep"
        @click="step = s"
      >
        {{ s }} mm
      </button>
      <button class="home" :disabled="busy" @click="doHome">Home Z</button>
    </div>

    <div v-for="a in AXES" :key="a.axis" class="row axis">
      <span class="lbl">{{ a.label }}</span>
      <button
        :disabled="!canMove || stepTooBig(a.axis)"
        :title="stepTooBig(a.axis) ? `step too large for a plunger (max ${MAX_PLUNGER_STEP} mm)` : ''"
        @click="doJog(a.axis, -1)"
      >{{ a.minus }}</button>
      <button
        :disabled="!canMove || stepTooBig(a.axis)"
        :title="stepTooBig(a.axis) ? `step too large for a plunger (max ${MAX_PLUNGER_STEP} mm)` : ''"
        @click="doJog(a.axis, 1)"
      >{{ a.plus }}</button>
      <span class="hint">{{ a.hint }}</span>
    </div>

    <p class="note">
      All six axes jog: four gantry, two plungers. <strong>Home Z only</strong> —
      homing Y drives a long search for an endstop that never reports and grinds
      against a hard stop. Plungers are uncalibrated, so <code>aspirate</code>
      still refuses; jogging them is how the calibration gets measured.
    </p>

    <ul class="log">
      <li v-for="(l, i) in log" :key="i" :class="{ bad: !l.ok }">
        <span class="t">{{ l.t }}</span> {{ l.msg }}
      </li>
    </ul>
  </section>
</template>

<style scoped>
.jog { background: var(--zeon-surface); border: 1px solid var(--zeon-line); border-radius: 2px; padding: 16px; color: var(--zeon-text); }
header { display: flex; align-items: center; gap: 12px; }
header h2 { margin: 0; font-size: 16px; }
.stop { margin-left: auto; background: rgba(255, 100, 93, 0.1); color: var(--zeon-red); border: 1px solid var(--zeon-red); border-radius: 2px; padding: 8px 16px; font-weight: 700; cursor: pointer; }
.warn { font-size: 12px; color: var(--zeon-orange); background: rgba(255, 141, 98, 0.08); border: 1px solid rgba(255, 141, 98, 0.24); border-radius: 2px; padding: 8px 10px; margin: 10px 0; }
.row { display: flex; align-items: center; gap: 8px; margin: 6px 0; flex-wrap: wrap; }
.status { font-size: 12px; color: var(--zeon-muted); }
.status .on { color: var(--zeon-lime); }
.status .bad { color: var(--zeon-red); font-weight: 600; }
.status .depth { margin-left: auto; }
.steps button { background: var(--zeon-surface-2); color: var(--zeon-text); border: 1px solid var(--zeon-line); border-radius: 2px; padding: 5px 9px; cursor: pointer; font-size: 12px; }
.steps button.sel { border-color: var(--zeon-lime); background: rgba(213, 255, 63, 0.1); color: var(--zeon-lime); }
.steps button:disabled { opacity: 0.35; cursor: not-allowed; }
.steps .home { margin-left: auto; background: rgba(102, 128, 255, 0.12); border-color: rgba(102, 128, 255, 0.4); }
.axis .lbl { width: 22px; font-weight: 700; }
.axis button { background: var(--zeon-surface-2); color: var(--zeon-text); border: 1px solid var(--zeon-line); border-radius: 2px; padding: 7px 14px; min-width: 66px; cursor: pointer; }
.axis button:disabled { opacity: 0.35; cursor: not-allowed; }
.axis .hint { font-size: 11px; color: var(--zeon-muted); }
.axis.disabled .lbl { color: var(--zeon-muted); }
.log { list-style: none; margin: 12px 0 0; padding: 8px; max-height: 150px; overflow-y: auto; background: var(--zeon-ink); border: 1px solid var(--zeon-line); border-radius: 2px; font-size: 11px; font-family: ui-monospace, monospace; }
.log li { padding: 2px 0; color: #aeb5aa; }
.log li.bad { color: var(--zeon-red); }
.log .t { color: #515851; margin-right: 6px; }
</style>
