# How the workflow runs, step by step — and how the frontend shows it

Date 2026-07-26. Design document for the last two slices: wiring the workflow end to end, and
the Workflow tab.

Status of what already exists (all committed, 681 tests green):

| Layer | State |
|---|---|
| Contracts — 14 action kinds, typed outputs, events with `seq`, 5-slot blackboard | done |
| `Plan` — indexed, mutable, loop materialization, pre-flight | done |
| `Runner` — one worker thread, pause/resume/inject/abort, event sink | done |
| Handlers — `arm.waypoint / move_relative / gripper / decap / traverse`, `lifecycle.initialize / reconnect` | done |
| Plan data — the 19-action handover workflow | done |
| Waypoints — 15 taught, per-arm ownership enforced | done |
| **Missing handlers** — `camera.snapshot`, `lh.move_relative`, `vision.identify`, `vision.solve_offset` | **to build** |
| **HTTP + websocket API** | **to build** |
| **Workflow tab** | **to build** |

---

## 1. The run, step by step

### 1.0 Before the run: initialization

`plans/handover.build_startup()` is one `lifecycle.initialize` action, executed on the same
runner as the workflow, so it gets indices, states, per-action logs and pause for free (D5).

```
enumerate settings.fleet  (not the live driver set, so a driver that failed to
                           build still gets a recorded outcome — R-INIT-1)
  → per device: connect, record outcome; one failure never stops the rest
  → arms:    clear latched faults, THEN enable  (a latched fault refuses the enable)
  → cameras: capture one frame, discarding settle frames
  → LH:      axis wiggle both directions, then retract Z
  → if home_after: each arm to its taught HOME at the `slow` tier
       no taught HOME → ctx.warn(...) , skip that arm, do NOT guess  (R-INIT-4)
```
Outputs: `devices[{device, connected, detail, simulated}]`, `homed[]`, `home_missing[]`.
Readiness derives from the pre-flight report: blocking problems → `failed`, advisory →
`degraded`, clean → `ready`.

### 1.1 Start

`POST /api/engine/start` → `Runner.start()`:

1. **Pre-flight the whole plan before anything moves.** Four problem codes:
   `device_not_in_fleet`, `no_handler`, `waypoint_not_owned`, `waypoint_not_taught`.
   Reported as `(device, name)` pairs, never bare names.
2. Blocking problems → `StartRefused` carrying the whole report. The UI renders every
   problem. `allow_degraded=True` is the operator's explicit confirmation path (R-ENG-2).
3. `run_started` event with the per-device `simulated` map — because simulation is the
   default, and a simulated run must be impossible to mistake for a real one.
4. The worker thread begins.

### 1.2 The 19 actions

Each is wrapped identically. The wrapper — not the handler — does all of this:

```
claim the device (holder "aid<N>")     ─┐
bind log context (run_id, aid)          │  so a handler cannot emit an
emit action_started                     │  unattributable record, and cannot
resolve from_slot → inputs              │  forget to log (R-LOG-5)
    call handler(action, ctx)  ← the only handler-authored line
collect ctx artifacts + warnings        │
build ActionResult (typed outputs)      │
emit action_finished                    │
release the claim                      ─┘
```

Failure classification matters and is the wrapper's job:

| Handler raises | Result status | Run |
|---|---|---|
| ordinary exception | `failed` | halts (default `on_failure="halt"`) |
| `ActionAborted` | `aborted` | aborts — an operator stop is never reported as a broken step |
| returns wrong `Outputs` model | `failed` (`TypeError`) | halts — a mis-typed output is a bug, not a mis-rendered row |

`max_attempts` retries are each recorded and each emit their own start/finish pair, so the UI
shows attempt 2 rather than silently replacing attempt 1.

The 19 actions in order — the brief's 20 steps, with the three transitions collapsed into one
blended traverse:

| # | Action | Device | Tier | What happens |
|---|---|---|---|---|
| 1 | `arm.gripper` open | right | fast | so neither arm arrives holding something |
| 2 | `arm.gripper` open | left | fast | |
| 3 | `arm.waypoint APPROACH_RACK` | right | fast | 298 mm — **joint replay** |
| 4 | `arm.waypoint APPROACH_TUBE_GRAB` | right | fast | |
| 5 | `arm.waypoint TUBE` | right | slow | 24 mm, down onto the tube |
| 6 | `arm.gripper` close **298 counts (35 %)** | right | — | tube held |
| 7 | `arm.waypoint APPROACH_TUBE_TRANSFER` | right | medium | lift clear, loaded |
| 8 | `arm.waypoint APPROACH_CAP_GRAB` | left | fast | right arm holds the tube steady |
| 9 | `arm.waypoint CAP_GRAB` | left | slow | |
| 10 | `arm.gripper` close **298 counts** | left | — | cap held at the same width |
| 11 | `arm.decap` 4 × 90° | left | slow | see 1.3 |
| 12 | `arm.waypoint APPROACH_CAP_STORE` | left | **slow** | lifts the loosened cap off the mouth |
| 13 | `arm.waypoint CAP_STORE` | left | **fast** | 416 mm, clear of everything |
| 14 | `arm.gripper` open | left | — | cap released into its store |
| 15 | `arm.traverse` ×3 | right | fast | 430 + 337 + 205 mm, blended |
| 16 | `arm.waypoint LIQUID_HANDLER_APPROACH_DECK` | right | fast | |
| 17 | `arm.waypoint LIQUID_HANDLER_DECK` | right | slow | the loop starts here |
| 18 | `control.loop` | — | — | see 1.4 |
| 19 | `lh.move_relative dz=+40` | ot | medium | retract |

### 1.3 Inside `arm.decap` (step 11)

```
preflight the ENTIRE ratchet, every intermediate angle, before the first move
   → refuse now rather than halfway: a half-unscrewed cap with a wound wrist
     is worse than never having started
if J6 is already wound too far (bench: a fresh 360° would reach 484°):
   unwind first, and record it — wrist_rewound_before_decap / rewind_deg
per bite ×4:
   turn +90°  (jaws closed — the cap turns)
   open       (must be open before the unwind, or it screws back on)
   turn −90°  (jaws open — the wrist returns, the cap stays)
   ctx.checkpoint()   ← pause/abort lands HERE: jaws open, wrist at start
   re-grip to 298 counts, except after the last bite
```
Net wrist travel is zero at any bite size, so repeating the operation never walks J6 toward
its limit. Rotation is commanded in joint space on the tool axis — asking IK for a 180° yaw
invites a different arm configuration and a large unplanned motion.

### 1.4 Inside `control.loop` (step 18) — the servo loop

Body materialized **per iteration** as real indexed rows carrying `parent_aid` and
`iteration`, so iteration 4's frames, overlays and solved offset are each inspectable
(R-VIS-8). Eight rows per iteration:

```
for cam in (handover_cam, gripper_cam):
    camera.snapshot   fresh=True → frame:<cam>      ← must post-date the last LH move
    vision.identify   target=tip  → tip:<cam>
    vision.identify   target=tube → tube:<cam>
vision.solve_offset   device=None (fuse both views) → selected_offset  + overlay artifacts
lh.move_relative      from_slot=selected_offset, clamp_mm=15
```

Termination reads `selected_offset.magnitude_mm` against `threshold_mm = 1.5`, and
**ignores `view_disagreement_mm` entirely** — two views need never agree to a fixed
tolerance, so gating on it can leave a converged loop spinning (Q4/D16).

Bounds, and what each catches:

| Bound | Meaning |
|---|---|
| `max_iterations = 12` | "needs longer" — exhausted |
| `no_progress_abort = 3` | the calibration is stale or wrong-signed; more iterations will not help |
| `MAX_MATERIALIZED_ACTIONS` | global cap on expanded rows — a never-converging loop cannot grow the plan without bound |
| nested loop | refused outright |

Outcome → status: `converged` → `complete`; `stalled` / `exhausted` → `failed` with
`ErrorInfo.type="LoopNotConverged"`, which halts the run under the default policy;
`aborted` → `aborted`.

### 1.5 Pause, resume, abort, inject

**Pause** is cooperative and honest. `pause()` flips to `pausing` and closes the gate; the
action in flight runs to completion — never a mid-trajectory stop — and the run stops at the
next boundary as `paused`. Boundaries: before every top-level action, before every loop
iteration, **and before every row inside an iteration**, so a pause mid-loop lands between
the move and the solve. When a handler actually parks in `ctx.checkpoint()` the runner learns
and flips to `paused` immediately; leaving it at `pausing` while a parked handler sits there
would be the mirror-image lie.

**Abort** sets the flag *and* opens the gate, so a parked handler wakes and raises
`ActionAborted`. Actions never reached stay `planned`, not `skipped`.

**Inject** targets `after_aid`; position is `index_of(after_aid) + 1`.

| Case | Behaviour |
|---|---|
| at the cursor (next) | **accepted** — this is where a failure leaves you, so it must work |
| after a completed action | accepted |
| after a loop | lands after the whole materialized region, keeping regions contiguous |
| between two rows of one iteration | accepted, **inherits `parent_aid`/`iteration`**, and that iteration executes it — otherwise an accepted injection would silently never run |
| at or before the running action | refused: "would put the new action behind it, where the run would never reach it" |
| before the cursor | refused: in the past |
| a `control.loop` inside a loop region | refused — the one nested-loop route no validator sees |

Every refusal emits `inject_rejected` **and** raises, so neither the UI nor a script can miss
it. `Runner.inject` deliberately does not pause/resume around itself — the pause→inject→resume
sequence belongs to the endpoint, which is the only thing that knows whether the operator sent
one action or three.

**The e-stop is never gated.** `device_claims` has no blocking primitive at all, and a test
asserts it: `POST /api/arms/{id}/stop` must stay reachable while the engine holds an arm.
The teach/jog routes *should* consult `device_claims.holder(device)` to refuse hand-jogging an
arm the engine is moving — but the stop route must not.

---

## 2. The API surface to build

```
POST   /api/engine/plan        load a named plan (handover | startup) → RunSnapshot
GET    /api/engine/snapshot    full current state — the reconnect path
POST   /api/engine/start       {allow_degraded}  → 409 + PreflightReport when refused
POST   /api/engine/pause       → {state}
POST   /api/engine/resume
POST   /api/engine/abort
POST   /api/engine/inject      {after_aid, action} → 409 + reason on refusal
GET    /api/engine/preflight   without starting, for the readiness panel
WS     /ws/engine              the event stream
GET    /api/runs/{run_id}/artifacts/{name}   serve an overlay image
```

Two things the API slice must respect, both learned the hard way:

- **An `APIRouter` prefix applies to websocket routes too.** `/ws/engine` must be registered
  on a prefix-less router, or clients see only a closed socket.
- **`EventSink.subscribe` callbacks run on the worker thread.** Hop the loop with
  `loop.call_soon_threadsafe`. Because the API thread also emits, arrival order is not
  delivery order: **`seq` is the ordering authority.** `since(seq)` replays a bounded 2000
  events; compare against `last_seq` to distinguish "nothing new" from "fell off the buffer,
  re-fetch the snapshot".

---

## 3. The Workflow tab

### 3.1 Layout

```
┌────────────────────────────────────────────────────────────────────────┐
│  ● SIMULATED          Workflow · handover            run 2f9c   ⏱ 1:24 │ ← reality banner
├──────────────────────────────┬─────────────────────────────────────────┤
│ READINESS                    │  ▶ Start   ⏸ Pause   ⏵ Resume   ⏹ Abort │
│ ready · 2 warnings           │  ⤵ Inject                               │
│ ⚠ left arm: no taught HOME   ├─────────────────────────────────────────┤
│ ⚠ gripper_cam delivering IR  │  PLAN                        17/19 done │
├──────────────────────────────┤                                          │
│                              │   1 ✓ right gripper open                 │
│  SELECTED ACTION             │   2 ✓ left gripper open                  │
│  #18.4 solve offset          │   3 ✓ → APPROACH_RACK      298mm  joint  │
│                              │  …                                       │
│  residual   1.9 mm           │  17 ✓ → LIQUID_HANDLER_DECK              │
│  σ  x 0.3  y 0.4  z 1.1      │  18 ◐ servo loop            iter 4/12    │
│  disagreement 0.6 mm (adv.)  │    18.1 ✓ snapshot handover_cam          │
│  method  tag_3d              │    18.2 ✓ identify tip     tag_anchored  │
│                              │    18.3 ✓ identify tube    tag_anchored  │
│  [overlay handover_cam]      │    18.4 ◐ solve offset     1.9mm ↓       │
│  [overlay gripper_cam]       │    18.5 ○ nudge liquid handler           │
│                              │  19 ○ liquid handler retracts Z          │
│  ▾ logs (7)                  │                                          │
└──────────────────────────────┴─────────────────────────────────────────┘
```

### 3.2 The plan chain

One row per action. **Index is `18.4`-style for materialized loop rows** — `parent_aid` and
`iteration` give the indent and the prefix, so the operator reads "iteration 4, row 4" rather
than a flat number that shifts every iteration.

State glyphs: `○` planned · `◐` running · `✓` complete · `✗` failed · `⊘` skipped · `⊗`
aborted. Colour is secondary to the glyph, because a red/green-only encoding is unreadable
for a meaningful fraction of operators.

Each row shows, inline, the one fact that matters for its kind:

| Kind | Inline summary |
|---|---|
| `arm.waypoint` | `→ NAME`, distance, **motion path** (`joint` / `joint+offset` / `cartesian`) |
| `arm.traverse` | `→ A → B → C`, blend actually used |
| `arm.gripper` | `open` / `close 298` |
| `arm.decap` | `4 × 90°`, net wrist travel, `rewound` if it was |
| `camera.snapshot` | camera, achieved mode, `color`/`ir`, **`stale` if the frame predated the move** |
| `vision.identify` | found/not, `tag_anchored` or `classical`, score |
| `vision.solve_offset` | residual mm with a trend arrow, method, `refused` + reason |
| `lh.move_relative` | requested vs **applied** mm, `clamped` when they differ |
| `control.loop` | `iter n/max`, outcome when finished |

Expanding a row reveals its full typed outputs, its artifacts, and **the log records it
produced** (`GET /api/logs?run_id=&aid=`) — R-UI-5/8.

### 3.3 Live state, and surviving a reconnect

One store fed by `/ws/engine`, seeded by `GET /api/engine/snapshot`.

```
mount / reconnect → GET snapshot  → render everything, remember snapshot.seq
ws event arrives  → if seq <= last: drop (a re-apply is a no-op, a skip is not)
                    if seq >  last+1: gap → re-fetch snapshot
                    else apply, last = seq
plan_replaced     → rebuild rows, but keep results keyed by aid
```
Keeping results by `aid` across a renumber is what makes injection non-destructive: an
injected action shifts indices, and a completed action's outputs must not vanish because its
number changed (D4).

### 3.4 Controls

`Start` is disabled when readiness is `failed`, and renders every pre-flight problem.
At `degraded` it opens a confirmation naming each degradation, and sends `allow_degraded`.

`Pause` shows `pausing…` until the runner reports `paused` — never a fake instant stop. The
button reads "Pause (finishes current step)" so the semantics are visible before the click,
not after.

`Inject` opens a panel with **the manual schema-driven form first** (R-UI-14): pick a kind
from the 14, fill validated fields, choose the insertion point, see the diff, apply. The chat
is a second tab on the same panel, and it *proposes* — the operator applies. Refusals render
the reason verbatim.

### 3.5 What the tab must never do

- Claim `paused` while the runner says `pausing`.
- Show a simulated run without the banner.
- Renumber a completed action's results away.
- Present `view_disagreement_mm` as if it gated convergence.
- Hide a `stale` frame or a `refused` solve — those are the two failures that otherwise look
  like slow progress.

---

## 4. Build order

| Slice | Owns | Depends on |
|---|---|---|
| **W1 camera handlers** | `handlers/camera.py` | — |
| **W2 LH handlers + sim world** | `handlers/liquid_handler.py`, `core/sim/world.py`, LH capability | — |
| **W3 vision handlers** | `handlers/vision.py`, `core/perception/{tip,tube,offset,overlay}.py` | W2's sim world |
| **W4 engine API** | `api/engine.py`, `api/runs.py`, `main.py` wiring | done engine |
| **W5 workflow tab** | `frontend/src/{views/WorkflowView,stores/engine,api/engine}` + `components/workflow/` | W4's protocol |
| **W6 integration review** | nothing — reads and reports | all |

W1/W2/W4 are fully parallel. W3 needs W2's sim world; W5 needs W4's shapes but can be built
against this document's contract. W6 last.
