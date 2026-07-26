<script setup lang="ts">
// The inject panel (R-UI-6/7/14, §3.4).
//
// **The manual form is first and is the required path** (R-UI-14): pick one of the 14 kinds,
// fill validated fields, choose the insertion point, see what changes, apply. The chat is an
// accelerator on a second tab, and it *proposes* — the operator applies. There is no LLM route in
// this build, and the chat tab says so rather than pretending to think.
//
// Three things this panel exists to get right:
//
//  * **Injection targets an identity, never a position.** The payload is `after_aid`, and the
//    index is only ever shown as a consequence. An injection renumbers every index after it, so a
//    panel that sent "at index 19" would be sending a different request by the time it arrived.
//  * **A refusal is rendered verbatim.** Several cases are refused deliberately — into the past,
//    in front of the running action, a loop inside a loop region — and the backend's sentence is
//    the operator's answer. We add a local advisory *before* the send, and never substitute it for
//    the real reason.
//  * **The most useful injection is after the failed row**, which is where the panel opens: the
//    cursor stays on a row that failed inside the servo loop, so `position = cursor + 1` is
//    accepted and the fix becomes the next thing that runs.
import { computed, inject, onMounted, ref, watch } from "vue";
import { ACTION_KINDS, getWaypointReport, type ActionKind, type WaypointReport } from "../../api/engine";
import { CamerasKey } from "../../stores/fleet";
import { useEngine } from "../../stores/engine";
import {
  COMMON_FIELDS, KIND_SPECS, SLOTS, buildAction, defaultsFor, parseAction, specFor,
  type FieldSpec, type FormValues,
} from "./injectSchema";

const emit = defineEmits<{ (e: "close"): void }>();

const { state, chain, cursorRow, failedRow, injectDefaultAid, commands } = useEngine();
const cameras = inject(CamerasKey, undefined);

const tab = ref<"form" | "chat">("form");
const kind = ref<ActionKind>("arm.gripper");
const values = ref<FormValues>(defaultsFor("arm.gripper"));
const afterAid = ref<number | null>(null);
const jsonMode = ref(false);
const jsonText = ref("");
const applied = ref("");
const showAdvanced = ref(false);

const waypoints = ref<WaypointReport | null>(null);
onMounted(async () => {
  try {
    waypoints.value = await getWaypointReport();
  } catch {
    // A missing waypoint library is not fatal here: the field stays free text, and pre-flight
    // refuses an unowned or untaught name with a message naming both arms (R-WP-2).
  }
});

// Open on the failed row, or on the row before the cursor so the new action runs next.
watch(injectDefaultAid, (aid) => {
  if (afterAid.value === null) afterAid.value = aid;
}, { immediate: true });

const spec = computed(() => specFor(kind.value));

watch(kind, (next) => {
  values.value = defaultsFor(next);
  const s = specFor(next);
  jsonMode.value = !!s?.jsonOnly;
  if (jsonMode.value) {
    // A skeleton with a real body, not an empty one: an empty `body` is refused, and a template
    // that cannot be sent teaches nothing about what a loop looks like.
    jsonText.value = JSON.stringify({
      kind: next,
      threshold_mm: 1.5,
      max_iterations: 12,
      body: [
        { kind: "camera.snapshot", device: "handover_cam", fresh: true, into_slot: "frame" },
        { kind: "vision.identify", device: "handover_cam", target: "tip", from_slot: "frame" },
        { kind: "vision.identify", device: "handover_cam", target: "tube", from_slot: "frame" },
        { kind: "vision.solve_offset", into_slot: "selected_offset" },
        { kind: "lh.move_relative", device: "ot", from_slot: "selected_offset", clamp_mm: 15 },
      ],
    }, null, 2);
  }
});

// --- device and waypoint pickers --------------------------------------------

const armIds = computed(() => (waypoints.value?.devices ?? []).map((d) => d.device));
const cameraIds = computed(() => Object.keys(cameras?.value ?? {}));
/** The run's own device map is the most authoritative list of ids there is. */
const runDevices = computed(() => Object.keys(state.simulated));

function deviceOptions(field: FieldSpec): string[] {
  const all = new Set<string>([...runDevices.value, ...Object.keys(state.readinessDevices)]);
  if (field.role === "arm") for (const id of armIds.value) all.add(id);
  if (field.role === "camera") for (const id of cameraIds.value) all.add(id);
  const list = Array.from(all);
  if (field.role === "arm") {
    const arms = new Set(armIds.value);
    // Only filter when we actually know which ids are arms; otherwise offer everything rather
    // than an empty picker.
    if (arms.size) return list.filter((id) => arms.has(id));
  }
  if (field.role === "camera" && cameraIds.value.length) {
    const cams = new Set(cameraIds.value);
    return list.filter((id) => cams.has(id));
  }
  return list;
}

/** Only waypoints the chosen arm owns — a picker that can express another arm's is a defect. */
const waypointOptions = computed(() => {
  const device = String(values.value.device ?? "");
  const arm = (waypoints.value?.devices ?? []).find((d) => d.device === device);
  if (!arm) return [];
  return arm.waypoints.map((w) => ({ name: w.name, taught: w.taught }));
});

// --- the insertion point ----------------------------------------------------

const targetRow = computed(() =>
  afterAid.value === null ? null : chain.value.find((item) => item.aid === afterAid.value) ?? null);

/** `index_of(after_aid) + 1`, the engine's own rule — shown, never sent. */
const landsAt = computed(() => (targetRow.value ? targetRow.value.row.index + 1 : 0));

const nextRow = computed(() => chain.value.find((item) => item.row.index === landsAt.value) ?? null);
const runningRow = computed(() => chain.value.find((item) => item.state === "running") ?? null);

const renumbered = computed(() => chain.value.filter((item) => item.row.index >= landsAt.value).length);

/**
 * What the engine will probably say, before it says it.
 *
 * Advisory only, and never a substitute for the 409: `Plan.refusal_for_insert` is the authority
 * and applies both the running rule and the cursor rule, and this client cannot see which action
 * the runner considers "running" at the instant the request lands.
 */
const advisory = computed(() => {
  if (afterAid.value !== null && !targetRow.value) {
    return "that aid is not in this plan — injection targets an identity, and that one no longer "
      + "exists here.";
  }
  const running = runningRow.value;
  if (running && landsAt.value <= running.row.index) {
    return `#${running.display} (${running.row.kind}) is executing now. Inserting at index `
      + `${landsAt.value} would put the new action behind it, where the run would never reach it `
      + "— the engine refuses this. Inject after the running action instead.";
  }
  if (chain.value.length && landsAt.value < state.cursor) {
    return `index ${landsAt.value} is behind the run, which is at #${cursorRow.value?.display ?? state.cursor}`
      + " — the engine refuses an injection into the past rather than relocating it.";
  }
  if (kind.value === "control.loop" && targetRow.value?.row.parent_aid != null) {
    return "a `control.loop` injected inside a loop region is refused: it is the one nested-loop "
      + "route no validator sees.";
  }
  if (targetRow.value?.row.parent_aid != null && targetRow.value.row.iteration != null) {
    return `this lands inside iteration ${targetRow.value.row.iteration} of loop `
      + `#${targetRow.value.row.parent_aid}, inherits that iteration, and runs as part of it. `
      + "Accepted only within the iteration the run is currently on.";
  }
  return "";
});

const advisoryIsRefusal = computed(() =>
  advisory.value.includes("refuses") || advisory.value.includes("no longer"));

// --- build and apply --------------------------------------------------------

const built = computed(() => {
  if (jsonMode.value) {
    const parsed = parseAction(jsonText.value);
    if (!parsed.kind || parsed.problems.length) {
      return { errors: { json: parsed.problems.join("; ") || "unreadable action" }, action: null };
    }
    const out = buildAction(parsed.kind, parsed.values);
    // A loop's `body` (and `until` / `corrected_axes`) has no form input, so it survives only by
    // being carried through untouched — dropping it would post the one field the operator wrote.
    if (out.action) out.action = { ...out.action, ...parsed.extra };
    return out;
  }
  return buildAction(kind.value, values.value);
});

const errorList = computed(() => Object.entries(built.value.errors));
const payload = computed(() =>
  JSON.stringify({ after_aid: afterAid.value, action: built.value.action }, null, 2));

const busy = computed(() => state.busy === "inject");

async function apply(): Promise<void> {
  const action = built.value.action;
  if (!action) return;
  applied.value = "";
  const ok = await commands.inject(afterAid.value, action);
  if (ok) {
    applied.value = `injected after aid ${afterAid.value ?? "(front)"} — the plan was replaced, `
      + "and every connected client saw it.";
  }
}

function useJsonFromForm(): void {
  jsonText.value = JSON.stringify(built.value.action ?? { kind: kind.value }, null, 2);
  jsonMode.value = true;
}

// --- the chat tab -----------------------------------------------------------

interface ChatLine { from: "operator" | "panel"; text: string }
const chat = ref<ChatLine[]>([{
  from: "panel",
  text: "No agent endpoint is configured in this build, so I cannot compose an action for you. "
    + "The manual form is the supported path (R-UI-14). If an agent elsewhere proposes an action, "
    + "paste its JSON below: I will validate it against the frozen schema, load it into the form, "
    + "and you apply it — a proposal is never applied for you.",
}]);
const chatInput = ref("");
const proposal = ref("");
const proposalProblems = ref<string[]>([]);

function send(): void {
  const text = chatInput.value.trim();
  if (!text) return;
  chat.value.push({ from: "operator", text });
  chat.value.push({
    from: "panel",
    text: "There is no agent route to send that to (`POST /api/engine/propose` does not exist in "
      + "this build). Describe it in the form instead, or paste a proposed action's JSON.",
  });
  chatInput.value = "";
}

function stageProposal(): void {
  const parsed = parseAction(proposal.value);
  proposalProblems.value = parsed.problems;
  if (!parsed.kind) return;
  kind.value = parsed.kind;
  // `watch(kind)` resets the values, so assign after it has run.
  requestAnimationFrame(() => {
    values.value = parsed.values;
    jsonMode.value = false;
    tab.value = "form";
  });
}

const KIND_GROUPS = [
  { title: "arm", kinds: ACTION_KINDS.filter((k) => k.startsWith("arm.")) },
  { title: "vision + cameras", kinds: ACTION_KINDS.filter((k) => k.startsWith("camera.") || k.startsWith("vision.")) },
  { title: "liquid handler", kinds: ACTION_KINDS.filter((k) => k.startsWith("lh.")) },
  { title: "lifecycle + control", kinds: ACTION_KINDS.filter((k) => k.startsWith("lifecycle.") || k.startsWith("control.")) },
];

const titleOf = (k: ActionKind) => KIND_SPECS.find((s) => s.kind === k)?.title ?? k;

// `FormValues` holds strings, numbers, booleans and nulls in one bag — which is the shape the
// schema needs — so the free-text inputs bind through these rather than `v-model` on a union.
const text = (name: string): string => {
  const value = values.value[name];
  return value === null || value === undefined ? "" : String(value);
};
const setText = (name: string, event: Event): void => {
  values.value[name] = (event.target as HTMLInputElement | HTMLTextAreaElement).value;
};
const bool = (name: string): boolean => values.value[name] === true;
const setBool = (name: string, event: Event): void => {
  values.value[name] = (event.target as HTMLInputElement).checked;
};
</script>

<template>
  <section class="card border-amber-500/50">
    <div class="mb-3 flex flex-wrap items-center gap-2">
      <h2 class="card-title mb-0">Inject an action</h2>
      <div class="flex gap-1" role="tablist" aria-label="inject mode">
        <button
          class="chip" :class="tab === 'form' ? 'chip-on' : ''" role="tab"
          :aria-selected="tab === 'form'" @click="tab = 'form'"
        >
          form (required path)
        </button>
        <button
          class="chip" :class="tab === 'chat' ? 'chip-on' : ''" role="tab"
          :aria-selected="tab === 'chat'" @click="tab = 'chat'"
        >
          chat (proposes only)
        </button>
      </div>
      <button class="btn btn-sm btn-ghost ml-auto" @click="emit('close')">close</button>
    </div>

    <p
      v-if="state.api === 'absent'"
      class="mb-3 rounded-lg border border-red-500/60 bg-red-950/30 px-3 py-2 text-xs text-red-200"
    >
      <span aria-hidden="true">✗</span> the engine API is unavailable, so nothing can be injected
      yet. The form still validates a payload against the frozen schema.
    </p>

    <!-- ======================= the form ======================= -->
    <div v-if="tab === 'form'" class="space-y-3">
      <!-- 1. what to insert -->
      <div>
        <label class="text-xs font-semibold uppercase tracking-wide text-deck-400" for="inject-kind">
          1 · kind
        </label>
        <select id="inject-kind" v-model="kind" class="field mt-1">
          <optgroup v-for="group in KIND_GROUPS" :key="group.title" :label="group.title">
            <option v-for="k in group.kinds" :key="k" :value="k">
              {{ titleOf(k) }} — {{ k }}
            </option>
          </optgroup>
        </select>
        <p v-if="spec" class="mt-1 text-[11px] text-deck-400">{{ spec.summary }}</p>
        <p v-if="spec?.jsonOnly" class="mt-1 text-[11px] text-amber-300">
          <span aria-hidden="true">⚠</span> {{ spec.jsonOnly }}
        </p>
      </div>

      <!-- 2. where -->
      <div>
        <label class="text-xs font-semibold uppercase tracking-wide text-deck-400" for="inject-after">
          2 · insertion point
        </label>
        <select id="inject-after" v-model="afterAid" class="field mt-1">
          <option :value="null">— at the front of the plan (before every row) —</option>
          <option v-for="item in chain" :key="item.aid" :value="item.aid">
            after #{{ item.display }} · {{ item.row.label || item.row.kind }}
            <span v-if="item.aid === failedRow?.aid"> (the failed row)</span>
          </option>
        </select>
        <div class="mt-1 flex flex-wrap gap-2">
          <button
            v-if="failedRow"
            class="chip"
            :class="afterAid === failedRow.aid ? 'chip-on' : ''"
            title="the cursor is on the failed row, so this lands at cursor + 1 and is accepted"
            @click="afterAid = failedRow.aid"
          >
            after the failed row #{{ failedRow.display }}
          </button>
          <button
            v-if="injectDefaultAid !== null"
            class="chip"
            @click="afterAid = injectDefaultAid"
          >
            make it the next step
          </button>
        </div>
      </div>

      <!-- 3. what changes -->
      <div class="rounded-lg border border-deck-600 bg-deck-900 px-3 py-2 text-xs">
        <p class="font-semibold text-deck-200">what changes</p>
        <ul class="mt-1 space-y-0.5 text-deck-300">
          <li>
            the new action gets index <span class="num">{{ landsAt }}</span>
            <span v-if="targetRow">
              , after #{{ targetRow.display }} ({{ targetRow.row.label || targetRow.row.kind }})
            </span>
            <span v-if="nextRow">
              and before #{{ nextRow.display }} ({{ nextRow.row.label || nextRow.row.kind }})
            </span>
          </li>
          <li v-if="renumbered">
            <span class="num">{{ renumbered }}</span> row(s) renumber by +1. Results stay with
            their <span class="num">aid</span>, so nothing already recorded is lost.
          </li>
          <li v-if="targetRow?.row.iteration != null" class="text-amber-200">
            it inherits <span class="num">parent_aid</span> and
            <span class="num">iteration {{ targetRow.row.iteration }}</span>, and that iteration
            executes it — otherwise an accepted injection would silently never run.
          </li>
          <li v-if="cursorRow" class="text-deck-400">
            the run is at #{{ cursorRow.display }} ({{ cursorRow.state }}).
          </li>
        </ul>
        <p
          v-if="advisory"
          class="mt-2 rounded border px-2 py-1"
          :class="advisoryIsRefusal
            ? 'border-red-500/60 bg-red-950/40 text-red-200'
            : 'border-amber-500/60 bg-amber-950/30 text-amber-200'"
        >
          <span aria-hidden="true">{{ advisoryIsRefusal ? "✗" : "⚠" }}</span>
          {{ advisory }}
          <span class="block text-[11px] opacity-80">
            Read before sending; the engine's own answer is the authority and is shown verbatim
            below.
          </span>
        </p>
      </div>

      <!-- 4. the fields -->
      <div v-if="!jsonMode && spec" class="space-y-2">
        <p class="text-xs font-semibold uppercase tracking-wide text-deck-400">3 · fields</p>
        <div v-for="field in spec.fields" :key="field.name">
          <label class="text-xs text-deck-300" :for="`f-${field.name}`">
            {{ field.label }}
            <span v-if="field.required" class="text-amber-300" title="required">*</span>
          </label>

          <select
            v-if="field.type === 'enum'"
            :id="`f-${field.name}`"
            v-model="values[field.name]"
            class="field mt-0.5"
          >
            <option v-for="option in field.options" :key="option" :value="option">{{ option }}</option>
          </select>

          <select
            v-else-if="field.type === 'slot'"
            :id="`f-${field.name}`"
            v-model="values[field.name]"
            class="field mt-0.5"
          >
            <option v-if="field.nullable" :value="null">— none —</option>
            <option v-for="slot in SLOTS" :key="slot" :value="slot">{{ slot }}</option>
          </select>

          <label
            v-else-if="field.type === 'bool'"
            class="mt-0.5 flex items-center gap-2 text-xs text-deck-200"
          >
            <input
              :id="`f-${field.name}`"
              type="checkbox"
              :checked="bool(field.name)"
              @change="setBool(field.name, $event)"
            />
            {{ bool(field.name) ? "yes" : "no" }}
          </label>

          <template v-else-if="field.type === 'device'">
            <input
              :id="`f-${field.name}`"
              class="field mt-0.5"
              :value="text(field.name)"
              :list="`devices-${field.name}`"
              :placeholder="field.required ? 'a fleet id' : 'empty = every device'"
              @input="setText(field.name, $event)"
            />
            <datalist :id="`devices-${field.name}`">
              <option v-for="id in deviceOptions(field)" :key="id" :value="id" />
            </datalist>
          </template>

          <template v-else-if="field.type === 'waypoint'">
            <input
              :id="`f-${field.name}`"
              class="field mt-0.5"
              :value="text(field.name)"
              list="waypoint-names"
              placeholder="a taught name this arm owns"
              @input="setText(field.name, $event)"
            />
            <datalist id="waypoint-names">
              <option v-for="w in waypointOptions" :key="w.name" :value="w.name">
                {{ w.taught ? "taught" : "NOT TAUGHT" }}
              </option>
            </datalist>
            <p v-if="!waypointOptions.length" class="text-[11px] text-deck-400">
              pick the arm first — the picker only ever offers waypoints that arm owns.
            </p>
          </template>

          <textarea
            v-else-if="field.type === 'text'"
            :id="`f-${field.name}`"
            class="field mt-0.5"
            rows="2"
            :value="text(field.name)"
            @input="setText(field.name, $event)"
          />

          <input
            v-else
            :id="`f-${field.name}`"
            class="field mt-0.5"
            :type="field.type === 'number' || field.type === 'int' ? 'number' : 'text'"
            :step="field.type === 'int' ? 1 : 'any'"
            :placeholder="field.nullable ? 'empty = null' : ''"
            :value="text(field.name)"
            @input="setText(field.name, $event)"
          />

          <p v-if="field.help" class="text-[11px] text-deck-400">{{ field.help }}</p>
          <p v-if="built.errors[field.name]" class="text-[11px] text-red-300">
            {{ built.errors[field.name] }}
          </p>
        </div>

        <details class="rounded-lg border border-deck-700" :open="showAdvanced">
          <summary class="cursor-pointer px-2 py-1 text-[11px] text-deck-400">
            every action also carries: label, speed, on_failure, max_attempts, note
          </summary>
          <div class="space-y-2 px-2 pb-2">
            <div v-for="field in COMMON_FIELDS" :key="field.name">
              <label class="text-xs text-deck-300" :for="`c-${field.name}`">{{ field.label }}</label>
              <select
                v-if="field.type === 'enum'"
                :id="`c-${field.name}`"
                v-model="values[field.name]"
                class="field mt-0.5"
              >
                <option v-for="option in field.options" :key="option" :value="option">{{ option }}</option>
              </select>
              <input
                v-else
                :id="`c-${field.name}`"
                class="field mt-0.5"
                :type="field.type === 'int' ? 'number' : 'text'"
                :value="text(field.name)"
                @input="setText(field.name, $event)"
              />
              <p class="text-[11px] text-deck-400">{{ field.help }}</p>
              <p v-if="built.errors[field.name]" class="text-[11px] text-red-300">
                {{ built.errors[field.name] }}
              </p>
            </div>
          </div>
        </details>
      </div>

      <!-- the JSON path: a loop body, or anything the form cannot express -->
      <div v-if="jsonMode" class="space-y-1">
        <label class="text-xs font-semibold uppercase tracking-wide text-deck-400" for="inject-json">
          3 · the action, as JSON
        </label>
        <textarea id="inject-json" v-model="jsonText" class="field num" rows="10" spellcheck="false" />
        <p class="text-[11px] text-deck-400">
          Validated against the same schema. Extra fields are reported rather than dropped: the
          backend forbids them, so a silently discarded field would send something other than
          what you wrote.
        </p>
      </div>

      <div class="flex flex-wrap items-center gap-2">
        <button
          class="btn btn-primary"
          :disabled="!built.action || busy || state.api === 'absent'"
          :title="built.action ? 'send POST /api/engine/inject' : 'fix the fields first'"
          @click="apply"
        >
          <span aria-hidden="true">⤵</span> {{ busy ? "Injecting…" : "Apply" }}
        </button>
        <button v-if="!jsonMode" class="btn btn-sm" @click="useJsonFromForm">edit as JSON</button>
        <button v-else class="btn btn-sm" :disabled="!!spec?.jsonOnly" @click="jsonMode = false">
          back to the form
        </button>
        <span v-if="errorList.length" class="text-xs text-red-300">
          {{ errorList.length }} field(s) need fixing
        </span>
      </div>

      <details class="rounded-lg border border-deck-700">
        <summary class="cursor-pointer px-2 py-1 text-[11px] text-deck-400">
          exactly what will be sent
        </summary>
        <pre class="num overflow-x-auto px-2 pb-2 text-[11px] text-deck-200">{{ payload }}</pre>
      </details>
    </div>

    <!-- ======================= the chat ======================= -->
    <div v-else class="space-y-3">
      <p class="rounded-lg border border-amber-500/50 bg-amber-950/20 px-3 py-2 text-xs text-amber-100">
        The chat <strong>proposes</strong>; you apply. It is an accelerator over the form, never a
        second way to change the plan — and in this build there is no agent endpoint behind it at
        all, so it cannot compose an action for you.
      </p>

      <div class="max-h-48 space-y-2 overflow-auto rounded-lg border border-deck-600 bg-deck-900 p-2">
        <p
          v-for="(line, i) in chat"
          :key="i"
          class="text-xs"
          :class="line.from === 'operator' ? 'text-deck-100' : 'text-deck-300'"
        >
          <span class="font-semibold">{{ line.from === "operator" ? "you" : "panel" }}:</span>
          {{ line.text }}
        </p>
      </div>

      <div class="flex gap-2">
        <input
          v-model="chatInput"
          class="field"
          placeholder="describe the action you want inserted"
          @keyup.enter="send"
        />
        <button class="btn" @click="send">Send</button>
      </div>

      <div>
        <label class="text-xs text-deck-300" for="proposal">
          a proposed action, as JSON — validated, then loaded into the form for you to apply
        </label>
        <textarea id="proposal" v-model="proposal" class="field num mt-1" rows="6" spellcheck="false" />
        <ul v-if="proposalProblems.length" class="mt-1 space-y-0.5">
          <li v-for="(p, i) in proposalProblems" :key="i" class="text-[11px] text-red-300">{{ p }}</li>
        </ul>
        <button class="btn btn-sm mt-1" :disabled="!proposal.trim()" @click="stageProposal">
          Validate and load into the form
        </button>
      </div>
    </div>

    <!-- The engine's answer, verbatim, whichever tab produced the request. -->
    <p
      v-if="state.commandError"
      class="mt-3 whitespace-pre-wrap rounded-lg border border-red-500/60 bg-red-950/40 px-3 py-2 text-xs text-red-200"
    >
      <span class="font-semibold">the engine refused:</span> {{ state.commandError }}
    </p>
    <p v-if="applied" class="mt-3 text-xs text-emerald-300">
      <span aria-hidden="true">✓</span> {{ applied }}
    </p>

    <!-- Every client sees every refusal, not only whoever sent it. -->
    <div v-if="state.rejections.length" class="mt-3">
      <p class="text-[11px] font-semibold uppercase tracking-wide text-deck-400">
        recent refusals (any client)
      </p>
      <ul class="mt-1 space-y-1">
        <li v-for="(r, i) in state.rejections" :key="i" class="text-[11px] text-red-300">
          <span class="num">after aid {{ r.afterAid ?? "front" }}</span> · {{ r.reason }}
        </li>
      </ul>
    </div>
  </section>
</template>
