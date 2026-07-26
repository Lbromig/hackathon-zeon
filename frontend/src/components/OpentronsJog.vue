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
] as const;

const canMove = computed(
  () => !busy.value && status.value?.connected === true && !status.value?.reference_lost,
);
const depth = computed(() =>
  typeof status.value?.z_below_datum_mm === "number"
    ? status.value.z_below_datum_mm.toFixed(2)
    : "—",
);

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
      <button :disabled="!canMove" @click="doJog(a.axis, -1)">{{ a.minus }}</button>
      <button :disabled="!canMove" @click="doJog(a.axis, 1)">{{ a.plus }}</button>
      <span class="hint">{{ a.hint }}</span>
    </div>

    <p class="note">
      All four axes jog. <strong>Home Z only</strong> — homing Y drives a long
      search for an endstop that never reports and grinds against a hard stop.
    </p>

    <ul class="log">
      <li v-for="(l, i) in log" :key="i" :class="{ bad: !l.ok }">
        <span class="t">{{ l.t }}</span> {{ l.msg }}
      </li>
    </ul>
  </section>
</template>

<style scoped>
.jog { background: #111c30; border-radius: 12px; padding: 16px; color: #dbe4f0; }
header { display: flex; align-items: center; gap: 12px; }
header h2 { margin: 0; font-size: 16px; }
.stop { margin-left: auto; background: #dc2626; color: #fff; border: 0; border-radius: 8px; padding: 8px 16px; font-weight: 700; cursor: pointer; }
.warn { font-size: 12px; color: #f0b429; background: #2a2110; border-radius: 8px; padding: 8px 10px; margin: 10px 0; }
.row { display: flex; align-items: center; gap: 8px; margin: 6px 0; flex-wrap: wrap; }
.status { font-size: 12px; color: #6b7a90; }
.status .on { color: #22c55e; }
.status .bad { color: #f87171; font-weight: 600; }
.status .depth { margin-left: auto; }
.steps button { background: #1c2b45; color: #dbe4f0; border: 0; border-radius: 6px; padding: 5px 9px; cursor: pointer; font-size: 12px; }
.steps button.sel { background: #2e6bff; color: #fff; }
.steps button:disabled { opacity: 0.35; cursor: not-allowed; }
.steps .home { margin-left: auto; background: #334867; }
.axis .lbl { width: 22px; font-weight: 700; }
.axis button { background: #1c2b45; color: #dbe4f0; border: 0; border-radius: 6px; padding: 7px 14px; min-width: 66px; cursor: pointer; }
.axis button:disabled { opacity: 0.35; cursor: not-allowed; }
.axis .hint { font-size: 11px; color: #6b7a90; }
.axis.disabled .lbl { color: #6b7a90; }
.log { list-style: none; margin: 12px 0 0; padding: 8px; max-height: 150px; overflow-y: auto; background: #0b1220; border-radius: 8px; font-size: 11px; font-family: ui-monospace, monospace; }
.log li { padding: 2px 0; color: #9fb0c8; }
.log li.bad { color: #f87171; }
.log .t { color: #4b5b73; margin-right: 6px; }
</style>
