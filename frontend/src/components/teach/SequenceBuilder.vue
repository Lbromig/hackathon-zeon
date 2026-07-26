<script setup lang="ts">
// Build an ordered walkthrough of waypoints and actions, then step through it.
//
// Four actions, matching what the arm can do: move to a taught waypoint, grip, ungrip,
// and the cap unscrew ratchet. Poses come from the teach library, so the geometry lives
// in one place and re-teaching a waypoint updates every sequence that uses it.
//
// Stepping is the point. A new sequence is walked one action at a time so the operator
// watches what the arm actually does before trusting Run all — which is why /step
// deliberately skips the whole-sequence pre-flight: a half-built sequence is expected to
// have gaps further down.
import { computed, onMounted, ref, watch } from "vue";
import * as api from "../../api/sequences";
import type { Sequence, SequenceAction, SequenceStep, StepResult } from "../../api/sequences";
import { useTeach } from "../../composables/useTeach";

const { arms, selectedId, poses, canMove } = useTeach();

const ACTIONS: SequenceAction[] = ["move", "grip", "ungrip", "unscrew"];

const list = ref<Sequence[]>([]);
const current = ref<Sequence | null>(null);
const newName = ref("");
const events = ref<StepResult[]>([]);
const cursor = ref(0); // 1-based index of the next step to run
const busy = ref(false);
const dirty = ref(false);

const poseNames = computed(() => poses.value.map((p) => p.name));
const stepCount = computed(() => current.value?.steps.length ?? 0);
const canStep = computed(() => !!current.value && cursor.value < stepCount.value && canMove.value && !busy.value);

onMounted(refresh);
watch(current, () => { events.value = []; cursor.value = 0; }, { deep: false });

async function refresh() {
  try {
    list.value = await api.listSequences();
    if (current.value) {
      current.value = list.value.find((s) => s.name === current.value?.name) ?? current.value;
    }
  } catch {
    /* advisory */
  }
}

function select(seq: Sequence) {
  current.value = JSON.parse(JSON.stringify(seq));
  dirty.value = false;
}

function create() {
  const name = newName.value.trim();
  if (!name) return;
  current.value = { name, steps: [], note: "", updated_at: "" };
  newName.value = "";
  dirty.value = true;
}

function addStep() {
  current.value?.steps.push(api.emptyStep(selectedId.value));
  dirty.value = true;
}

function removeStep(i: number) {
  current.value?.steps.splice(i, 1);
  dirty.value = true;
}

function move(i: number, delta: number) {
  const steps = current.value?.steps;
  if (!steps) return;
  const j = i + delta;
  if (j < 0 || j >= steps.length) return;
  [steps[i], steps[j]] = [steps[j], steps[i]];
  dirty.value = true;
}

async function save() {
  if (!current.value) return;
  busy.value = true;
  try {
    const saved = await api.saveSequence(current.value.name, current.value);
    current.value = saved;
    dirty.value = false;
    await refresh();
  } catch (e) {
    events.value = [{ index: 0, phase: "failed", ok: false, detail: String(e) }];
  } finally {
    busy.value = false;
  }
}

async function remove() {
  if (!current.value) return;
  list.value = await api.deleteSequence(current.value.name);
  current.value = null;
}

async function withResult(fn: () => Promise<api.RunResult>) {
  busy.value = true;
  try {
    const r = await fn();
    events.value = r.events;
    return r;
  } catch (e) {
    events.value = [{ index: 0, phase: "failed", ok: false, detail: String(e) }];
    return null;
  } finally {
    busy.value = false;
  }
}

const preflight = () => current.value && withResult(() => api.preflightSequence(current.value!.name));

async function step() {
  if (!current.value) return;
  const next = cursor.value + 1;
  const r = await withResult(() => api.runSequenceStep(current.value!.name, next));
  if (r?.ok) cursor.value = next;
}

async function runAll() {
  if (!current.value) return;
  const r = await withResult(() => api.runSequence(current.value!.name));
  cursor.value = r?.ok ? stepCount.value : 0;
}

function stepClass(i: number) {
  const n = i + 1;
  if (n === cursor.value + 1) return "border-l-2 border-sky-400 bg-sky-500/5";
  if (n <= cursor.value) return "border-l-2 border-emerald-500/60 opacity-60";
  return "border-l-2 border-transparent";
}
</script>

<template>
  <section class="card">
    <div class="flex items-baseline justify-between gap-2">
      <h2 class="card-title">Sequences</h2>
      <span v-if="current" class="num text-xs text-deck-400">
        {{ cursor }} / {{ stepCount }} done
      </span>
    </div>

    <!-- picker -->
    <div class="flex flex-wrap gap-1">
      <button
        v-for="s in list"
        :key="s.name"
        class="chip"
        :class="{ 'chip-on': current?.name === s.name }"
        @click="select(s)"
      >
        {{ s.name }}
        <span class="text-deck-500">({{ s.steps.length }})</span>
      </button>
    </div>
    <div class="mt-2 flex gap-2">
      <input
        v-model="newName"
        class="field"
        placeholder="new sequence (e.g. rack_to_ot)"
        @keyup.enter="create"
      />
      <button class="btn shrink-0" :disabled="!newName.trim()" @click="create">+ New</button>
    </div>

    <template v-if="current">
      <ul class="mt-3 space-y-1">
        <li
          v-for="(s, i) in current.steps"
          :key="i"
          class="flex flex-wrap items-center gap-1.5 py-1 pl-2"
          :class="stepClass(i)"
        >
          <span class="num w-5 shrink-0 text-xs text-deck-500">{{ i + 1 }}</span>

          <select v-model="s.action" class="field w-24 text-xs" @change="dirty = true">
            <option v-for="a in ACTIONS" :key="a" :value="a">{{ a }}</option>
          </select>

          <select v-model="s.device" class="field w-24 text-xs" @change="dirty = true">
            <option v-for="a in arms" :key="a.id" :value="a.id">{{ a.id }}</option>
          </select>

          <select
            v-if="s.action === 'move'"
            v-model="s.pose"
            class="field min-w-40 flex-1 text-xs"
            @change="dirty = true"
          >
            <option value="">— pick a waypoint —</option>
            <option v-for="n in poseNames" :key="n" :value="n">{{ n }}</option>
          </select>

          <input
            v-else-if="s.action === 'grip'"
            v-model.number="s.width"
            type="number"
            class="field w-24 num text-xs"
            placeholder="width"
            @change="dirty = true"
          />

          <select
            v-else-if="s.action === 'unscrew'"
            v-model.number="s.half_turns"
            class="field w-28 text-xs"
            @change="dirty = true"
          >
            <option v-for="n in [1, 2, 3, 4]" :key="n" :value="n">{{ n }}×180°</option>
          </select>

          <span v-else class="flex-1 text-xs text-deck-500">opens the jaws</span>

          <button class="btn btn-sm btn-ghost" title="move up" @click="move(i, -1)">↑</button>
          <button class="btn btn-sm btn-ghost" title="move down" @click="move(i, 1)">↓</button>
          <button class="btn btn-sm btn-ghost" title="remove" @click="removeStep(i)">✕</button>
        </li>
      </ul>

      <div class="mt-2 flex flex-wrap items-center gap-2">
        <button class="btn btn-sm" @click="addStep">+ Step</button>
        <button class="btn btn-sm" :class="{ 'btn-primary': dirty }" :disabled="busy" @click="save">
          {{ dirty ? "Save *" : "Save" }}
        </button>
        <button class="btn btn-sm btn-ghost" :disabled="busy" @click="preflight">Pre-flight</button>

        <span class="ml-auto flex gap-2">
          <button class="btn btn-sm btn-ghost" :disabled="!cursor" title="start again" @click="cursor = 0">
            ⟲ Reset
          </button>
          <button
            class="btn btn-sm btn-primary"
            :disabled="!canStep || dirty"
            :title="dirty ? 'save first' : 'run the next step only'"
            @click="step"
          >
            ▶ Step {{ cursor + 1 }}
          </button>
          <button
            class="btn btn-sm"
            :disabled="!canMove || busy || dirty || !stepCount"
            :title="dirty ? 'save first' : 'run every step'"
            @click="runAll"
          >
            ▶▶ Run all
          </button>
          <button class="btn btn-sm btn-ghost" @click="remove">Delete</button>
        </span>
      </div>

      <p v-if="dirty" class="mt-2 text-xs text-amber-300">
        Unsaved changes — save before running, the backend runs the stored sequence.
      </p>

      <ul v-if="events.length" class="mt-3 space-y-0.5 border-t border-deck-700 pt-2">
        <li
          v-for="(e, i) in events"
          :key="i"
          class="flex gap-2 text-xs"
          :class="e.ok ? 'text-deck-300' : 'text-red-300'"
        >
          <span class="num w-5 shrink-0 text-deck-500">{{ e.index || "—" }}</span>
          <span class="w-16 shrink-0 text-deck-500">{{ e.phase }}</span>
          <span class="min-w-0 flex-1">{{ e.detail }}</span>
        </li>
      </ul>
    </template>

    <p v-else class="mt-3 text-xs text-deck-400">
      Pick a sequence or make one. A sequence is an ordered walkthrough — move to a taught
      waypoint, grip, ungrip, unscrew — so you can compose e.g. tube rack → Opentrons deck
      from the poses you already taught, then step through it one action at a time.
    </p>
  </section>
</template>
