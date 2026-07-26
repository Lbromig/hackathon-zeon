# Adversarial review — `EXECUTION_AND_VISUALIZATION.md`

Date 2026-07-26. Reviewed against the code on `agent-loop-p0` at `9b9598b`. Suite re-run: **681
passed**. Every claim below was checked against a file, and the behavioural ones were reproduced by
driving the real `Runner` with stub handlers (probes 1–8, described inline).

---

## Verdict

**Proceed with changes.** The design's model of the engine is accurate in the places that were hardest
to get right — cooperative pause and the `pausing`/`paused` distinction, abort as a separate path,
per-attempt retry recording, failure classification, the non-blocking device claim and the ungated
e-stop, and the decision not to gate the loop on `view_disagreement_mm`. Those are settled; do not
relitigate them. But three of its load-bearing premises are false against the built code, and each
one is a premise that several of the five slices would encode independently before anyone noticed:
the servo loop names a camera that provably sees nothing and disagrees with `settings.servo_cameras`
(§1.4 vs `core/config.py:557`); nothing clears the blackboard between iterations, so a failed
capture or a refused solve is silently re-consumed and re-commanded (demonstrated, probe 8); and the
injection truth table's two most important rows are wrong in the direction that accepts an unsafe
insertion and refuses the useful one (demonstrated, probes 4 and 5). Add to that a class of quieter
defect — three `Outputs` fields the tab is told to read that no handler ever writes, and non-finite
floats that make the event stream unparseable in a browser — and the document needs an editing pass
plus about forty lines of engine/plan fixes before five agents are pointed at it. None of this is
architectural; the shape is right. Fix B1–B9, apply the per-slice notes, launch.

---

## Blocking findings

### B1 — The servo loop snapshots the one camera that is known to see nothing, and the plan disagrees with config

**Claim.** §1.4: `for cam in (handover_cam, gripper_cam)`, and `vision.solve_offset device=None
(fuse both views)`.

**Why it is wrong.** `handover.SERVO_CAMERAS = ("handover_cam", "gripper_cam")`
(`backend/app/engine/plans/handover.py:37`) — whose own docstring claims it is "taken from config
rather than named here" and is not. Config says something else:
`DEFAULT_SERVO_CAMERAS = ("handover_cam", "gripper_left_cam")` (`core/config.py:557`), justified by a
measurement three lines above it: "The slot named `gripper_left_cam` is physically the RIGHT arm's
eye-in-hand camera and sees the same handover. The slot named `gripper_cam` is aimed across the room"
(`core/config.py:554-556`). `docs/v2/REQUIREMENTS.md:293` quantifies it: `gripper_left_cam` 16/25
frames with a tag, `gripper_cam` **0/53**. D19 makes config authoritative ("which cameras those are
comes from config, never hardcoded", `actions.py:339-340`).

**Failure scenario.** The run reaches step 18. `camera.snapshot gripper_cam` succeeds — a frame is a
frame. Both `vision.identify` actions on it return `found=False` in every iteration, because that
camera is pointed across the room. `vision.solve_offset(device=None)`, written to D19, enumerates
`settings.servo_cameras` and looks for `tip:gripper_left_cam` / `tube:gripper_left_cam`, which
**nothing in the plan ever writes** — `SlotEmpty`, or a one-view degraded solve, or a
`low_observability` refusal for twelve iterations and then `stalled`. If W3 instead enumerates
`blackboard.devices("tip")` it silently runs single-view and fuses a blind camera's noise into the
weighted solve. Either way the bench conclusion is "the vision doesn't work" and the actual defect is
one identifier in one tuple. Worst variant: W1 reads config, W3 reads the plan, and the two never
meet a test.

**Fix.** One authority. `handover.SERVO_CAMERAS` becomes `settings.servo_cameras` (import
`core.config.settings`, keep the module constant as an alias so `waypoints_used()`-style callers
still work), and the design's §1.4 stops naming cameras literally. Add a test that the plan's
snapshot devices equal `settings.servo_cameras`. Owner: whoever lands W1, before W3 starts.

### B2 — Nothing clears the blackboard between iterations, so a stale frame, detection or offset is silently reused *and re-commanded*

**Claim.** §1.4 presents each iteration as self-contained: fresh frame → identify → solve → nudge.
`blackboard.py:129-132` states as fact that "A loop iteration clears the per-camera slots before it
starts, so iteration 4 cannot solve against iteration 3's detections if a capture fails".

**Why it is wrong.** No caller of `Blackboard.clear` exists anywhere outside the class
(`grep -rn "blackboard.clear"` → nothing). `Runner._run_loop` (`runner.py:858-942`) never touches it.

**Failure scenario — reproduced.** Probe 8: a loop whose iteration-2 `camera.snapshot` raises
`ActionAborted`. Result: iteration 2's `vision.solve_offset` ran to `complete` **against iteration
1's frame**, `lh.move_relative` executed the resulting offset a second time, iteration 3 ran, and the
loop reported `exhausted`. Nothing in the record says a stale frame was used. The same hole makes
these three real:
- an identify handler that returns without writing on `found=False` leaves the previous iteration's
  point in `tip:<cam>`, and the fused solve mixes a fresh detection with a stale one — a wrong offset
  that looks plausible;
- a `vision.solve_offset` that refuses (`method="refused"`) and returns without writing leaves the
  previous `selected_offset` in place, so `lh.move_relative` **re-applies an already-applied
  correction**. `clamp_mm=15` does not catch a legitimate 1.6 mm re-command; the head simply
  overshoots and the loop measures a worse offset next pass;
- `Runner._watch` reads the same stale `selected_offset`, so a refused solve is scored as "no
  progress" against a number the refusal never produced.

**Fix.** Two lines in `_run_loop`, immediately after `materialize_iteration` succeeds:
`for slot in ("frame", "tip", "tube"): self.blackboard.clear(slot)` and
`self.blackboard.clear(loop.watch_slot)`. That makes the invariant structural rather than a contract
every handler must remember, and it makes `SlotEmpty` the honest failure for a skipped write. Then
state in the design that a handler **must** write its slot on every outcome including refusal, so the
two defences agree. Owner: the runner change belongs with W4 (it is the only slice already in that
file); W1/W2/W3 get the always-write rule in their briefs.

### B3 — Injecting into an iteration that has already run is accepted, mislabelled, and executed next

**Claim.** §1.5's truth table: "before the cursor → refused: in the past" and "between two rows of one
iteration → accepted, inherits `parent_aid`/`iteration`, and that iteration executes it".

**Why it is wrong.** `Plan.refusal_for_insert` returns early on the running-action branch
(`plan.py:545-552`) and **never consults `cursor` while a run is executing**. `Runner.running_aid` is
the innermost frame of `_stack` (`runner.py:357-362`), and between a loop's children — including the
whole time the run is *paused* inside a loop, since `_boundary()` is called with only the loop's aid
on the stack — that is the **loop's** aid, whose index is before the entire materialized region. So
every row of every completed iteration passes `position > running_index`.

**Failure scenario — reproduced.** Probe 4: pause requested during iteration 3's solve; run reaches
`paused` with `running_aid=1` (the loop) and `cursor=8`. Injecting `arm.gripper open` after iteration
**1**'s first row is **ACCEPTED**, adopted with `parent_aid=1, iteration=1`, and placed at index 2 —
in front of iteration 3's still-pending `lh.move_relative` at index 10. On resume it is the first
`planned` row in the region, so `next_unrun_in_region` runs it **immediately**, before iteration 3
finishes, and `_run_loop` sets `plan.cursor = index_of(injected)` (`runner.py:907`) — the cursor jumps
backwards into iteration 1. The operator's mental model was "make this the next step"; the engine's
answer was "insert it into a pass that finished forty seconds ago, run it now, and label it
iteration 1". With a tube in the right arm's jaws, "open the gripper next" and "open the gripper at an
unspecified point" are not the same request.

**Fix.** `refusal_for_insert` must apply the cursor rule *in addition to* the running rule rather than
instead of it: drop the `return None` at `plan.py:552` and fall through to the `position < cursor`
check. That alone closes it (the pending row's index is ≥ cursor). Then the truth table's mid-iteration
row becomes: accepted only within the **current** iteration, which is the only case the inheritance
logic at `plan.py:500-511` is correct for.

### B4 — After a failure inside the servo loop, the cursor lands *past the loop*, the fix injection is refused, and Resume runs step 20

**Claim.** §1.5: "at the cursor (next) → **accepted** — this is where a failure leaves you, so it must
work". §1.2/D22, and the design's Controls section offering Resume after a failure.

**Why it is wrong.** `_run` advances the cursor **before** it inspects the result, and for a `Loop`
that means `plan.cursor = region_of(aid)[1]` — the end of the whole materialized region
(`runner.py:579-582`). The loop's own status is `failed`, so the run halts with the cursor sitting on
step 20.

**Failure scenario — reproduced.** Probe 5: `vision.solve_offset` raises in iteration 2 of an
8-iteration loop.
- Final state `failed`, `cursor=7` — i.e. the `lh.move_relative dz=+40` retract, not the failed step.
- `inject(after_aid=<failed solve>)` → **REFUSED**: *"index 6 is in the past — that part of the plan
  has already run"*. The single most useful injection in the whole feature, at the single most likely
  failure point, is refused with a reason that is not true.
- `resume()` returns `True`, **skips iteration 2's pending `lh.move_relative`** (still `planned` at the
  end), never re-enters the loop, and executes step 20: the liquid handler retracts Z by 40 mm while
  the tube is still misaligned. Run then reports `failed`.

An operator watching the tab sees a red row inside the loop, presses the enabled Resume button the
design specifies, and the machine's response is to retract the pipette and stop.

**Fix.** Cheapest honest fix, all in W4's endpoint: refuse `POST /api/engine/resume` with 409 when
`plan.at(plan.cursor)` is `None` or when the failed action has `parent_aid is not None`, message
*"the failed step is inside the servo loop; the loop cannot be re-entered — inject before the loop, or
abort and re-run"*. Second, in the design: strike the unqualified "must work" and state that
inject-at-the-cursor works for top-level failures only. If there is appetite for the real fix, it is
setting the cursor to `index_of(failed_child)` and letting `_run_loop` be re-enterable — that is not a
hackathon-sized change and should not be attempted by a parallel slice.

### B5 — A non-finite float in `OffsetOutputs` makes the event stream unparseable in the browser

**Claim.** §2 names two API hazards. This is a third, and it breaks both transports.

**Why it is wrong.** `OutputsBase` does **not** set `allow_inf_nan=False` (`actions.py:461-463`);
only `ActionBase` does (`actions.py:83`). So `OffsetOutputs(magnitude_mm=nan, condition_number=inf)`
validates — and `condition_number=inf` is the *natural* numpy result for a singular matrix, which is
exactly the case D17's conditioning gate exists to report. Probe 6/7 confirm:
`model_dump(mode="json")` keeps `nan`; Starlette's `WebSocket.send_json` uses plain `json.dumps`
(`.venv/.../starlette/websockets.py:174`) and emits literal `NaN` / `Infinity`; `JSON.parse` in a
browser rejects both. Starlette's `JSONResponse.render` uses `allow_nan=False`
(`.venv/.../starlette/responses.py:198`), so a hand-rolled `JSONResponse(...)` 500s instead.

**Failure scenario.** Iteration 3's solve is ill-conditioned and honestly reports
`condition_number=inf`. The `action_finished` frame arrives, `JSON.parse` throws inside the socket
handler, the tab drops it — and because that frame was the row's only carrier of state, the row stays
`◐ running` for the rest of the run. If the handler does not catch, the socket dies and the operator
loses live view during the one iteration that went wrong.

**Fix.** Three defences, all cheap: (1) W4 sends `await ws.send_text(event.model_dump_json())` —
pydantic maps non-finite to `null` (verified), so this is correct by construction and faster;
(2) W4 declares `response_model=RunSnapshot` on the snapshot route (FastAPI's pydantic serializer
also nulls them — verified 200); (3) W3 never *reports* a non-finite number: `condition_number=None`
when not finite, `magnitude_mm=None` rather than `nan`.

### B6 — `seq` restarts at 1 for every run, so the reconnect rule silently blanks the second run

**Claim.** §3.3: `if seq <= last: drop (a re-apply is a no-op, a skip is not)`.

**Why it is wrong.** `EventSequence` is documented and implemented as **per run**, not per process
("a process-global counter would make a fresh run look like it had already missed events",
`events.py:48-50`). A fresh `Runner` gets a fresh `EventSink` with a fresh sequence
(`runner.py:322`), so run B's first event is `seq=1`. Probe 2 measured 117 events for a
4-iteration run of the real plan; a 12-iteration run is ~250.

**Failure scenario.** The operator runs the handover (tab ends with `last=117`), loads the plan again
and starts run B. The tab drops every event with `seq ≤ 117` — the whole of run B up to roughly the
decap — showing a frozen plan and a stale run id while both arms move. The design's own rule causes it.

**Fix.** `EventBase.run_id` is on every event already. W5's store resets `last=0` and rebuilds from
the snapshot whenever an event's `run_id` differs from the store's; W4's `POST /api/engine/plan`
response carries the new `run_id` so the transition is explicit rather than inferred.

### B7 — Three of the tab's promised inline summaries have no field behind them

**Claim.** §3.2's table: `arm.waypoint` shows "**motion path** (`joint` / `joint+offset` /
`cartesian`)"; `arm.decap` shows "net wrist travel, `rewound` if it was".

**Why it is wrong.** The fields exist and are never written.
- `MoveOutputs.path` (`actions.py:500-510`) — the `arm.waypoint` handler returns without it
  (`arm.py:233-239`) and instead stuffs the value into `resolved_speed["path"]` via `_speed_report`
  (`arm.py:127-138`), whose comment says "`actions.py` is frozen for Wave 1, so adding a `path` field
  to the model is a contract change". The field was added anyway. So `outputs.path` is `""` for every
  move in every run. The design's vocabulary is also wrong: the Literal is
  `joint_replay` / `joint_replay+cartesian_offset` / `cartesian`, and `_speed_report` writes two
  values (`"relative"`, `"traverse"`) that are not in the Literal at all.
- `DecapOutputs.wrist_rewound_before_decap` and `rewind_deg` (`actions.py:529-537`) — the handler
  returns without them and emits a `ctx.warn("wrist_rewound_before_decap", …)` instead
  (`arm.py:440-451`), with the same stale "frozen" comment. `net_wrist_travel_deg` **is** set ✓.

**Failure scenario.** W5 renders `joint` / `cartesian` from `outputs.path` and every one of the eleven
waypoint moves shows blank — including the four 298–430 mm moves where "did that replay taught joints
or solve IK?" is the first question after an unexpected trajectory. Meanwhile a decap that needed a
484°-class rewind renders identically to one that did not, and the tab's own §3.5 rule ("do not hide
what actually happened") is violated by omission.

**Fix.** Six lines in `arm.py`: pass `path=path` on the `MoveOutputs` at `arm.py:233`, and
`wrist_rewound_before_decap=bool(result.unwound_deg), rewind_deg=result.unwound_deg` at `arm.py:462`.
Keep the warnings — they are the reachable-by-code half. Drop `"path"` from `_speed_report` for
`waypoint` only, or leave it; do not let W5 read it. Owner: fold into W1 (a two-file, no-conflict
edit) and correct the design's path vocabulary.

### B8 — Nothing stops the plan being swapped while a run is moving arms

**Claim.** §2: `POST /api/engine/plan  load a named plan (handover | startup) → RunSnapshot`.

**Why it is wrong.** No guard is specified, and every constraint that would make one unnecessary is
absent. `Runner.start` only refuses if *its own* worker is alive (`runner.py:408-409`); a new `Runner`
knows nothing about the old one. `device_claims` is process-wide (`runner.py:295-297`) and a claim is
held for the whole action — including while a handler is parked in `ctx.checkpoint()` inside a decap
ratchet.

**Failure scenario.** Operator pauses mid-decap (parked in `on_bite`, `left` claimed by `aid11`) and
hits "load plan" to look at the startup plan. The endpoint replaces the module-level runner; the old
worker thread stays alive, parked, holding `left` forever. The new run's first `arm.gripper left`
raises `DeviceBusy`, which `_attempt` classifies as `retriable=True` (`runner.py:727`), so it retries
and fails again. The UI shows a new plan; the bench holds a cap half unscrewed.

**Fix.** `POST /api/engine/plan` returns 409 when `runner.state in {"preflight","running","pausing",
"paused"}`, with the reason and a pointer to abort. On an accepted load: `abort()` + `join(timeout)`
the previous runner before replacing it, and assert `device_claims.held() == {}` afterwards.

### B9 — The loop can report `converged` on an offset whose z was never observed

**Claim.** §1.4: "Termination reads `selected_offset.magnitude_mm` against `threshold_mm = 1.5`".

**Why it is unsafe as stated.** The read is correct and safe on the `None` paths — `Runner._watch`
falls back to the residual norm only when *every* axis is a number (`runner.py:962-980`), refuses a
per-camera `watch_slot` (`runner.py:951-957`), rejects non-finite, and `None` counts as no progress
rather than as zero. That part is right. But `OffsetOutputs.magnitude_mm` is documented as "**over the
observed axes only**" (`actions.py:619-620`), and nothing checks `observed_axes`. So a solve that sees
x and y and reports `residual_offset_mm = {"x":0.8,"y":0.9,"z":None}, magnitude_mm=1.2` terminates the
loop as `converged`.

**Failure scenario.** The side view is occluded by the jaws mid-approach — the exact case D27 says to
expect. Only the top-down `handover_cam` contributes, z is unobservable, `lh.move_relative` warns
`unobservable_axis` and commands no z (`arm.py:275-279` for the arm twin; W2's LH handler must mirror
it), and the loop declares success at 1.2 mm in x/y with an unmeasured z error. Step 20 retracts, the
tab shows `converged`, and the pipette's height relative to the tube mouth was never measured. This is
R-VIS-4's failure mode surviving in the one place the design does not guard it.

**Fix.** State the contract in the design and enforce it in W3: **`magnitude_mm` is `None` unless
every axis the move can act on is observed.** A partial norm is not a convergence metric. The loop
then stalls with `sigma_mm`/`observed_axes` on the record, which is the honest outcome. Optionally
belt-and-braces in `_watch`: treat a magnitude as unknown when `observed_axes` has fewer than three
entries.

---

## Non-blocking findings

1. **`LoopOutputs.outcome == "aborted"` when nobody aborted.** A child failing sets `_halted`, and
   `_run_loop` maps that to `outcome="aborted"` (`runner.py:918-920`); `_grade` then produces
   `status="failed"` with the message "a step inside the loop failed" (`runner.py:783-791`, confirmed
   probe 5). The Literal has no `"failed"` member and `actions.py` is frozen, so the fix is renderer
   side: W5 must take the *status* and `ErrorInfo.message` as authority and never print "aborted" for
   a loop unless `RunState` is `aborted`. §1.4's outcome→status table needs the fourth row.
2. **A handler that raises `ActionAborted` inside a loop does not abort the run.** `_run` sets the
   abort flag for a top-level action (`runner.py:583-589`); `_run_loop`'s child branch only `break`s
   (`runner.py:909-910`). Probe 8: the loop ran the rest of that iteration and two more, ending
   `exhausted`. Handlers must raise `ActionAborted` **only** via `ctx.checkpoint()`. A three-line
   runner fix (mirror `self._abort.set()`) is better than a convention; either way it belongs in the
   briefs.
3. **The readiness panel's promised contents do not exist in the readiness event.** §3.1 shows
   "⚠ left arm: no taught HOME" and "⚠ `gripper_cam` delivering IR". `Runner._warnings` is only ever
   `report.as_warnings()` (`runner.py:394-397`), i.e. pre-flight problems, and `RunSnapshot.warnings`
   comes from it (`runner.py:552-555`). `home_not_defined` (`lifecycle.py:225`) and R-CAM-15's IR
   warning are per-action `ctx.warn`s that arrive on `action_finished.result.warnings`. Also: pre-flight
   never checks HOME, because the handover plan does not reference it — and both HOMEs *are* taught
   today (`data/teach_poses.json`). W5's panel must union `ReadinessChanged.warnings` with the
   warnings of every completed action, keyed by `Warning_.code`.
4. **`Artifact.url` is always empty and `path` is absolute.** `context.artifact()` never sets `url`
   (`context.py:137-142`); the docstring says "repo-relative, under `settings.artifact_dir`"
   (`actions.py:706-707`) but `lifecycle.py:109` writes an absolute join and `settings.artifact_dir`
   resolves to an absolute path. The overlay thumbnails in §3.1 have nothing to load. Fix in W4: set
   `url` in `ActionContext.artifact` (`ctx` knows `run_id`), and make the artifact route serve by
   basename with traversal rejection — never by client-supplied path.
5. **Every `vision.identify` will carry an 820-character `repr()` of a numpy array.**
   `_inputs_for` peeks `from_slot` and `_jsonable`s it (`runner.py:808-818`); `VisionIdentify.from_slot`
   defaults to `frame`, a per-camera slot, so whatever `camera.snapshot` writes gets `repr`'d into
   `ActionStarted.inputs`, `ActionResult.inputs` **and** the log record — 48 times in a 6-iteration
   run (measured: 820 chars for a 480×640×3 array). Fix by contract: what goes in `frame:<cam>` is a
   small record (path, `captured_at`, shape, `stream`) with the pixels reachable but not `repr`-able —
   see W1's note.
6. **The §3.1 mock's `18.4` numbering is ambiguous and the row count is wrong.** The body is **8**
   rows (probe 1/2: two cameras × snapshot+2 identify, plus solve, plus nudge), not the 5 the mock
   shows, and `18.1…18.8` repeats identically for all twelve iterations — 96 rows sharing 8 labels.
   Use `18·4.7` (loop index · iteration · row) or "iter 4 · row 7"; §3.2's prose already says
   "iteration 4, row 4", so the mock is the thing that is wrong.
7. **There is no router and no store library.** `frontend/package.json` has `vue` and nothing else —
   no `vue-router`, no `pinia`. `App.vue` is a 3-tab shell with `localStorage`. Introducing a router
   to add one tab is gold-plating with a merge-conflict cost (`App.vue`, `main.ts`); a fourth tab plus
   a `composables/useEngine.ts` in the shape of the existing `useFleet.ts` is the same feature with no
   new dependency.
8. **R-ENG-11's other half has no owner.** §1.5 says the teach/jog routes "*should* consult
   `device_claims.holder(device)`". Nothing does (`grep`: `device_claims` appears only in
   `runner.py`, `engine/__init__.py`, tests). It is not in any of W1–W6's file lists. Either assign it
   to W4 (it is a five-line guard in `teach.py` on the move routes, `/stop` exempt — and note the
   design's claim that "a test asserts" the stop route stays reachable is only half true: the tests
   assert the claim registry *has no blocking primitive* and is released either way —
   `test_runner_pause.py:589-608`, whose own docstring says "if a future change adds a claim check to
   `POST /api/arms/{id}/stop`, this is the test that should have stopped it", which it would not; the
   route-level assertion does not exist) or drop the sentence.
9. **The LH capability has neither method this design assumes.**
   `drivers/capabilities/liquid_handler.py` exposes `home/pick_up_tip/drop_tip/aspirate/dispense/
   move_to` and no `move_relative`, no `initialize`, no position readback. The Opentrons driver has
   both under different names (`move_by`, `driver.py:408`; `initialize`, `driver.py:325`), and
   `lifecycle._init_liquid_handler` probes for `driver.initialize` and *warns and skips* when absent
   (`lifecycle.py:130-136`) — so §1.0's flat assertion "LH: axis wiggle both directions, then retract
   Z" is currently false for the mock. W2 owns closing all of this.
10. **`main.py` never imports the handler package.** `app.include_router` list at `main.py:88-92`;
    nothing imports `backend.app.engine.handlers`, whose import *is* the registration
    (`handlers/__init__.py:1-8`). Probe 1 with the import: pre-flight already reports `no_handler` ×4
    for the unbuilt kinds and readiness `failed`. Without it: readiness `failed` for all fourteen.
11. **`MAX_MATERIALIZED_ACTIONS` can never bite for this plan**, so §1.4's bounds table overstates it.
    `Loop`'s own validator refuses at authoring time when `len(body) × max_iterations > 500`
    (`actions.py:391-399`), and 8 × 12 = 96 (probe 1). Injections carry `origin="inject"` and are not
    counted by `materialized_count()` (`plan.py:563-566`), so they cannot push it over either. The
    binding bound is `max_iterations`; the ceiling is a second seatbelt for multi-loop plans. Answering
    the brief's question directly: **500 is enough — 96 needed — and if it were not, the failure mode
    is graceful**: `MaterializationCapped` → `outcome="exhausted"` → `status="failed"` → halt with the
    plan intact (`runner.py:887-893`).
12. **§1.3 omits the inter-bite lift.** `cap_ops` lifts `lift_per_regrip_mm` with the jaws open before
    each re-grip, following the cap up its thread (`cap_ops.py:311-317`), and reports `lifted_mm`
    (`cap_ops.py:337`). `DecapOutputs` has no field for it and the handler drops it. Real motion,
    unrecorded; at minimum add it to the design's ratchet listing.
13. **§1.2's wrapper listing is out of order in two ways.** The claim is inputs → started → claim; the
    code resolves inputs first (`runner.py:686`), emits `ActionStarted` (`runner.py:702`), then claims
    (`runner.py:709`) — so a `DeviceBusy` action has already emitted a start, which is fine but the
    UI must handle it. More importantly, "resolve `from_slot` → inputs" does **not** mean the runner
    hands resolved deltas to the handler: it records `inputs["from_slot_value"]` and nothing else
    ("Resolving the slot into `dx/dy/dz` stays the handler's job", `runner.py:806-807`), while
    `ActionResult.inputs`' own docstring claims the dump is "**after** slot resolution"
    (`actions.py:773-775`). W2 must read the slot itself; W5 must read requested millimetres from
    `LHMoveOutputs.requested_mm`, never from `inputs`.
14. **A halt emits `run_paused`.** `_execute` emits `RunPaused(reason="action_failed")`
    (`runner.py:671-675`) and the state then goes to `failed`. W5 must take `RunStateChanged` as the
    state authority and treat `run_paused` as a pointer at a row, or the tab will claim "paused" on a
    dead run — the mirror image of the §3.5 rule it is told to enforce.
15. **A skipped-past row inside a finished region stays `planned` forever** (probe 5, idx 6). The
    design's "actions never reached stay `planned`, not `skipped`" is accurate, but the tab needs a
    rule for rendering a `planned` row in a `failed`/`complete` run — greyed with "not reached", not
    `○ pending`.

---

## Factual corrections

| Design claim | Reality | Proof |
|---|---|---|
| §1.4 servo cameras are `handover_cam`, `gripper_cam` | config says `handover_cam`, `gripper_left_cam`; `gripper_cam` detected 0 tags in 53 frames | `handover.py:37` vs `core/config.py:554-557`, `REQUIREMENTS.md:293` |
| §1.4 iteration is self-contained (implied); `blackboard` docstring says a loop iteration clears the per-camera slots | no caller of `Blackboard.clear` exists | `blackboard.py:129-132`; `runner.py:858-942`; probe 8 |
| §1.5 "before the cursor → refused: in the past" | not applied while a run executes; the running-aid branch returns early | `plan.py:545-552`; probe 4 |
| §1.5 "at the cursor → accepted … this is where a failure leaves you" | false for a failure inside a loop: cursor lands past the region and the fix is refused | `runner.py:579-582`; probe 5 |
| §1.5 "between two rows of one iteration → accepted, inherits" | true, but also true for a *completed* iteration, which is not the intent | `plan.py:500-511`; probe 4 |
| §1.4 `aborted` → `aborted` | a child *failure* also yields `outcome="aborted"`, graded `failed` | `runner.py:918-920`, `783-791`; probe 5 |
| §1.4 `MAX_MATERIALIZED_ACTIONS` bounds a never-converging loop | pre-empted by `Loop`'s validator; 8×12=96 of 500; injections not counted | `actions.py:391-399`; `plan.py:563-566`; probe 1 |
| §3.2 `arm.waypoint` shows motion path `joint`/`joint+offset`/`cartesian` | `MoveOutputs.path` is never written; Literal values differ | `actions.py:500`; `arm.py:127-138`, `233-239` |
| §3.2 `arm.decap` shows `rewound` | `wrist_rewound_before_decap`/`rewind_deg` never written; only a warning code | `actions.py:529-537`; `arm.py:440-451`, `462-468` |
| §3.1 readiness panel shows "no taught HOME", "delivering IR" | `RunSnapshot.warnings` only ever carries pre-flight problems | `runner.py:394-397`, `552-555` |
| §3.1 shows 5 rows per iteration, indices `18.1…18.5` | 8 rows per iteration; labels repeat across all 12 | probe 1/2 |
| §2 lists two API hazards | a third breaks both transports: non-finite floats | `actions.py:461-463`; starlette `websockets.py:174`, `responses.py:198`; probe 6/7 |
| §3.3 "if `seq <= last`: drop" | `seq` restarts at 1 per run; drops a whole second run | `events.py:48-50`; `runner.py:322` |
| §1.0 "LH: axis wiggle both directions, then retract Z" | delegated to `driver.initialize`, which the capability does not define — warn and skip | `lifecycle.py:121-138`; `capabilities/liquid_handler.py` |
| §1.2 "claim the device … emit `action_started`" | emitted first, claimed second; `inputs` are *not* slot-resolved for the handler | `runner.py:686`, `702`, `709`, `806-807` |
| §1.5 "a test asserts" the stop route stays reachable under an engine claim | the test asserts the claim *registry* has no blocking primitive; no route-level test exists | `test_runner_pause.py:589-608`; `teach.py:488-497` |
| implied: the plan's 15 waypoints may be untaught | all 15 spec waypoints are taught **with joints**, both HOMEs included (plus 3 scratch points on `right`) | `data/teach_poses.json` |

---

## Per-slice notes

### W1 — camera handlers (`handlers/camera.py`)

- **You own the `frame:<cam>` contract and W3 consumes it, so W3 depends on you, not only on W2.**
  §4's dependency graph is wrong on this point. Publish the slot's shape in your commit message and
  in the module docstring before W3 starts.
- Do **not** put a bare numpy array in the slot. The runner `repr()`s whatever is there into every
  downstream `vision.identify`'s `inputs` and log record (`runner.py:808-818`, ~820 chars per array).
  Store a small record — `path`, `captured_at`, `width`, `height`, `stream`, and the pixels on an
  attribute that `model_dump` excludes (or a separate in-process handle) — so `_jsonable` produces
  something a human can read.
- `SnapshotOutputs.stale` (`actions.py:568-569`) is yours to compute, and the only signal the tab has
  for it. `captured_at` must be the **child-side** capture timestamp (D20), and you need a "last LH
  move at" reference to compare against — decide with W2 where that lives (simplest: W2's
  `lh.move_relative` writes nothing to the blackboard, so put a monotonic timestamp on the shared
  `Blackboard`… you cannot, it has five frozen slots. Use a module-level `last_motion_at` in W2's
  handler module and import it, and say so in both docstrings).
- `achieved_mode` and `stream` are read-back facts, not the request (`actions.py:570-575`). A slot
  delivering IR when colour was requested is a `ctx.warn` naming the slot (R-CAM-15) — and see
  non-blocking 3: that warning reaches the UI only via `action_finished`, so do not assume the
  readiness panel will show it.
- **Fix B1 as part of this slice**: `handover.SERVO_CAMERAS` from `settings.servo_cameras`, plus a
  test. And **fix B7** while you are in `arm.py` (`path=`, `wrist_rewound_before_decap=`,
  `rewind_deg=`) — it is a six-line, no-conflict edit and W5 is blocked on it.
- Never emit a non-finite float in any `Outputs` field (B5).

### W2 — LH handlers + sim world (`handlers/liquid_handler.py`, `core/sim/world.py`, LH capability)

- The capability defines **no** `move_relative`, `initialize`, or position readback
  (`capabilities/liquid_handler.py:20-39`). The Opentrons driver has `move_by` (`driver.py:408`) and
  `initialize` (`driver.py:325`). You own reconciling them; `lifecycle._init_liquid_handler` already
  probes for `driver.initialize` by name (`lifecycle.py:130`), so match that name exactly or the
  wiggle silently never runs.
- **Read `from_slot` yourself.** The runner does not resolve it into `dx/dy/dz` — it only records
  `inputs["from_slot_value"]` (`runner.py:806-807`). Reuse the logic in `arm._deltas_from_slot`
  (`arm.py:244-288`) rather than writing a second one: it already handles `OffsetOutputs`, a dumped
  dict, and `{"x","y","z"}`, and it already gets the `None`-is-not-zero rule right
  (`ctx.warn("unobservable_axis")`, refuse when nothing is observable). Lift it into a shared helper
  if you must, but do not diverge from it.
- **Agree the sign and the frame with W3 in writing, in both docstrings, before either of you starts.**
  Nothing in the design or the code states whether `residual_offset_mm` is tip→tube or tube→tip, or
  which axes of the camera frame map to which deck axes. A sign error does not crash: the loop runs,
  gets worse, and `no_progress_abort=3` ends it as `stalled` after three iterations — three
  commanded moves in the wrong direction, clamped only at 15 mm each.
- `LHMoveOutputs.requested_mm` / `applied_mm` / `provenance` are the tab's only source for "requested
  vs applied, clamped" (`actions.py:548-558`). Fill all three. `clamp_mm` is a **refusal**, not a
  clamp (R-LH-3, `actions.py:261-263`): raise, and let the row show `failed` with the reason.
- Do not raise `ActionAborted` yourself — inside a loop it does not abort the run (non-blocking 2).
  Use `ctx.checkpoint()`.

### W3 — vision handlers (`handlers/vision.py`, `core/perception/{tip,tube,offset,overlay}.py`)

- **`device=None` cannot read a per-camera slot through `get`.** `Blackboard.get("tip")` with no
  device raises `ValueError`, not `SlotEmpty` (`blackboard.py:92-95`). Enumerate with
  `blackboard.devices("tip")` (`blackboard.py:117-124`) — that is the method the design's "fuse all
  servo cameras" needs — but **intersect it with `settings.servo_cameras`** (D19), or you will fuse
  whatever happened to be written. See B1: today those two sets are disjoint on the second camera.
- `into_slot="selected_offset"` is single-valued and takes **no** device
  (`blackboard.py:83-85` raises if you pass one). That is correct and works: per-camera in, single
  out. Per-camera `frame`/`tip`/`tube`, single `offset`/`selected_offset` — the design's asymmetry is
  right.
- **Always write your slot, on every outcome including a refusal** (B2). A refused solve that returns
  without writing leaves the previous iteration's offset in place and the LH re-commands it.
  `method="refused"` with `residual_offset_mm` all-`None` is the honest value; note that
  `_deltas_from_slot` then refuses the move, which halts the run — that is the correct loud failure,
  not a bug to work around.
- **`magnitude_mm` is `None` unless every axis is observed** (B9). Never a partial norm. `_watch`
  treats `None` as no-progress, which stalls the loop honestly; a partial norm makes it declare
  success on an axis it never measured.
- `sigma_mm`, `observed_axes`, `singular_values`, `condition_number`, `contributions` are all on the
  frozen model — use them; they are what makes the refusal auditable (D17). **Non-finite values must
  become `None`** (B5): `condition_number=inf` for a singular matrix is the natural numpy answer and
  it breaks the browser's `JSON.parse`.
- `view_disagreement_mm` is advisory. Nothing in the engine reads it (`grep` confirms: only the model
  and tests) and nothing should start.
- Your blocker on W2 is the **sim world** only. Your blocker on W1 is the `frame` slot's shape. Get
  that shape from W1's docstring on day one and stub it.

### W4 — engine API (`api/engine.py`, `api/runs.py`, `main.py` wiring)

- `main.py` must `import backend.app.engine.handlers` (the import is the registration,
  `handlers/__init__.py:1-8`). Without it every kind is `no_handler` and readiness is `failed`.
- **Register `/ws/engine` on a prefix-less router** — correct as the design says.
- **Send `event.model_dump_json()` as text**, not `send_json(model_dump(mode="json"))` (B5). Declare
  `response_model=RunSnapshot` on the snapshot route for the same reason.
- **Guarantee wire order server-side rather than asking the client to repair it.** `EventSink.emit`
  allocates `seq` under the lock but calls subscribers outside it (`runner.py:169-183`), and the API
  thread also emits — so two events can reach a subscriber in the wrong order, and the design's
  "drop `seq <= last`" rule would then discard an `action_finished` the client never saw, leaving that
  row `running` forever. Do not send from the subscriber callback: `call_soon_threadsafe` into an
  `asyncio.Queue`, and have one sender task that tracks `last_sent` and calls `sink.since(last_sent)`
  before each send. The socket then emits strictly increasing `seq`, and W5's rule becomes trivially
  correct.
- **409 the resume of a mid-loop failure** (B4) and **409 a plan load during a live run** (B8);
  `abort()` + `join()` + assert `device_claims.held() == {}` before replacing a runner.
- Pass a new `run_id` on every plan load, and make the snapshot's `run_id` the thing W5 keys on (B6).
- Own the **pause → inject → resume** sequence: `Runner.inject` deliberately does not
  (`runner.py:505-511`). Refusals must return the reason verbatim *and* the `inject_rejected` event
  will already have gone out — do not double-emit.
- Artifacts: fill `Artifact.url` (in `ActionContext.artifact`, which knows `run_id`) and serve by
  basename with traversal rejection. `path` is absolute today (non-blocking 4).
- Add the two-line blackboard clear in `_run_loop` (B2) and the `refusal_for_insert` fall-through in
  `plan.py` (B3). You are the only slice in those files.
- Do **not** add any check to `POST /api/arms/{id}/stop` (D21, `teach.py:488-497`,
  `runner.py:262-267`). Adding the claim guard to the *other* teach routes is the missing owner in
  non-blocking 8 — take it if you have room.

### W5 — workflow tab

- **Build against the frozen shapes, not against §3.1's mock.** Generate TypeScript from
  `event_adapter.json_schema()` and the `Outputs` union rather than hand-writing types
  (`events.py:209-212`, `actions.py:441-443`).
- Fields that exist and are populated today: `MoveOutputs.waypoint/offsets_mm/pose_*/joints_*/
  resolved_speed`, `TraverseOutputs.reached/blend_deg`, `DecapOutputs.bites/step_deg/
  total_rotation_deg/net_wrist_travel_deg/preflight_ok`, `GripperOutputs.state/width_*`,
  `LoopOutputs.outcome/iterations/materialized/final_magnitude_mm`. Fields that will be empty until
  B7 lands: `MoveOutputs.path`, `DecapOutputs.wrist_rewound_before_decap/rewind_deg` — do not ship a
  renderer that reads them until you have confirmed the fix. `SnapshotOutputs.stale/achieved_mode/
  stream` and every `OffsetOutputs` field are W1/W3's to populate; treat absent as unknown, never as
  false.
- Reset `last_seq` and re-snapshot whenever `run_id` changes (B6). Key results by `aid`, never by
  index (that part of §3.3 is exactly right).
- `RunStateChanged` is the state authority. `run_paused` is a pointer at a row and is emitted on a
  *halt* too (non-blocking 14).
- Loop rows: 8 per iteration, and the label must carry the iteration (non-blocking 6). Nest on
  `parent_aid` + `iteration`; both are on `ActionRow` (`events.py:225-226`).
- Readiness panel: union pre-flight warnings with per-action `result.warnings`, matching on
  `Warning_.code` (non-blocking 3).
- Resume must be disabled — or clearly labelled as "abandons the servo loop" — when the failed action
  has a `parent_aid` (B4).
- No router, no pinia (non-blocking 7). A fourth tab in `App.vue` plus `composables/useEngine.ts`.
- You are safely parallel to W4 **only** because the shapes are frozen. The two things you cannot
  build against this document are the wire *ordering* guarantee and the `run_id` reset — get those
  from W4's route module the day it lands.

### W6 — integration review

- Four behaviours reproduced during this review have **no test today**. Write them, in
  `test_loop_expansion.py` / `test_runner_inject.py`, from these recipes:
  1. *(probe 4, B3)* A 3-row loop body, `max_iterations=8`, a handler that calls `runner.pause()`
     during iteration 3's second row. Once `state == "paused"`, `inject(after_aid=<iteration 1's
     first row>)` must be **refused**. Today it is accepted, inherits `iteration=1`, and runs first
     on resume.
  2. *(probe 5, B4)* Same plan, a handler that raises in iteration 2. Assert the cursor is at the
     failed child (not past the region), that injecting after it is accepted, and that `resume()`
     does not execute the action following the loop.
  3. *(probe 8, B2)* Same plan, iteration 2's snapshot handler raises. Assert the loop stops, and
     that no later action reads a slot written by an earlier iteration.
  4. *(probe 7, B5)* An `OffsetOutputs` with `condition_number=inf`, `magnitude_mm=nan` serialized
     through the websocket route: assert the frame parses under `json.loads(..., parse_constant=…)`
     rejecting `NaN`/`Infinity`, i.e. that the wire is strict JSON.
- Assert: plan snapshot devices == `settings.servo_cameras`; no `Outputs` on the wire contains a
  non-finite float; `device_claims.held()` is empty after every run; a completed action's result
  survives an injection (already covered, `test_runner_inject.py:301`).

---

## Where the design is right

Do not relitigate these; they were checked and they hold.

- **Pause.** `pausing` on request with the gate closed, `paused` at the boundary, and `paused`
  immediately when a handler actually parks in `checkpoint()` — the `_PauseGate` `on_park` hook does
  exactly what §1.5 says (`runner.py:214-249`, `632-646`). Boundaries exist before every top-level
  action, before every iteration, and before every row inside an iteration (`runner.py:571`, `881`,
  `905`). Never a mid-trajectory stop.
- **Abort.** Sets the flag *and* opens the gate so a parked handler raises; `ActionAborted` becomes
  `status="aborted"`, never `failed`; unreached actions stay `planned` (`runner.py:482-497`,
  `717-723`).
- **Retries.** Each attempt is recorded and emits its own start/finish pair; `on_failure="retry"`
  forces at least two (`runner.py:649-676`).
- **Failure classification.** The three-row table in §1.2 is exactly `_attempt`'s behaviour, including
  a mis-typed `Outputs` becoming a `TypeError`-shaped failure (`runner.py:761-769`).
- **Device claims.** Advisory, never blocking, raises `DeviceBusy` immediately, and the e-stop path is
  exempt by design and by comment (`runner.py:252-297`, `teach.py:17-19`, `488-497`).
- **The loop terminates on the remaining offset and ignores `view_disagreement_mm`.** Verified by
  grep and by two existing tests; the `None`-is-not-zero handling in `_watch` is correct and safe
  (`runner.py:944-980`).
- **`snapshot()` reads `seq` before the rows on purpose**, and every state-carrying event is absolute,
  so a re-apply is a no-op (`runner.py:543-555`). The snapshot-then-stream handover is sound.
- **`inject` does not pause/resume around itself**; that sequence belongs to the endpoint
  (`runner.py:505-511`).
- **`origin` on every row** (`plan`/`inject`/`expand`) answers "who put this here", and materialized
  rows really do carry `parent_aid`/`iteration` — verified end to end on the real 19-action plan
  (probe 2: 19 → 51 rows over 4 iterations, `converged`, 117 events).
- **The 19-action table** matches `plans/handover.py` action for action, including the deliberately
  inverted `APPROACH_CAP_STORE` slow / `CAP_STORE` fast pair and the 298-count grip.
- **§1.3's decap description** matches `cap_ops.plan_ratchet` and `run_ratchet` precisely — whole-
  ratchet pre-flight before the first move, the rewind when J6 is parked too far round, `on_bite`
  after the unwind and before the re-grip as the only safe pause point, net wrist travel zero
  (`cap_ops.py:208-230`, `267-338`). Only the inter-bite lift is missing.
- **§3.5's five rules are the right five**, and all five are enforceable. The additions worth making:
  do not claim `paused` on `run_paused(reason="action_failed")`; do not render a loop as "aborted"
  when a step inside it failed; do not offer a Resume that will skip the loop.
