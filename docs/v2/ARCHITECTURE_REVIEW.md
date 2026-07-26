# Adversarial review — `docs/v2/ARCHITECTURE.md`

Date: 2026-07-26 · Reviewer: principal-engineer pass · Branch `agent-loop-p0` · 216 tests
passing · **no hardware attached**

Method: read `GAP_ANALYSIS.md`, then `ARCHITECTURE.md` in full, then verified every load-bearing claim
against the code. Where I measured something (tag detectability, colour vs mono, resolution, manifest
contents) the measurement is stated so it can be re-run. I did not take the design's characterisation
of the existing code on trust — see **Factual corrections** for where it does not match.

---

## Verdict

**Approve-with-changes on §§1–6 and §8–10; redesign-needed on §7 (vision) and on the freshness
contract in §4.6.** The engine, the action model, the driver-substitution simulation decision, the
thread-over-asyncio decision, the OT transport split, and the deletion plan are all better than
what exists and I would ship them close to as written. §7 is not merely risky — it is built on two
named cameras (`gripper_cam`, `handover_cam`) whose recorded imagery **contains zero detectable
AprilTags in 53 sampled frames** and, for `handover_cam` and `overview_cam`, is **greyscale
RealSense IR with the dot projector on**, which defeats both the tag-anchored primary path and the
Hough/ellipse fallback. Worse, the slot→physical-camera binding is demonstrably unstable across
sessions (the same fleet id shows two different viewpoints and two different resolutions), so
per-camera jacobians and per-camera pixel offsets keyed on a fleet id are keyed on something that
changes without notice. Separately, `deviation_mm` as defined is structurally zero in the
per-camera case and blind in exactly the correlated-failure cases it claims to catch, so it does
not satisfy the brief's "with confidence and deviation". Fix §7's identity/observability/metric
story and the ten blocking items below and this is a good plan; ship §7 as written and the servo
loop will pass in sim and drive the head sideways on the bench.

---

## Blocking findings

### B1 — The two servo cameras are chosen without evidence, and per-camera config is keyed on an unstable identity

**Claim.** §3.6 hard-codes the servo loop against `gripper_cam` and `handover_cam`. §7.4 keys
`data/camera_jacobians.json` on the fleet id. §7.2/§7.3 key the tip/tube offsets ("a `(du, dv)` in
tag widths", "config-driven ROI") on the fleet id. §12 assumption 1 mitigates drift only by
checking the *arm* pose.

**What I measured** (`FiducialDetector()` with the repo's tuned parameters, ~25 frames sampled per
folder across all three sessions in `temp/`):

| slot | sessions | greyscale (IR) | frames with a tag | ids ever seen |
|---|---|---|---|---|
| **`gripper_cam`** | 2 (192 + 75 fr) | no | **0 / 53** | — |
| **`handover_cam`** | 3 (115 + 192 + 75 fr) | 1 of 3 sessions | 41/57 colour, **0/25 IR** | 188, 218 |
| `overview_cam` | 3 | **all 3** | **0 / 82** | — |
| `gripper_left_cam` | 1 (75 fr) | no | 16 / 25 | 218 |

Visual inspection of the frames adds three facts the design does not account for:

* `gripper_cam` (right-arm flange) points **across the room** in both sessions — a tripod, a wall,
  a seated person. It never sees the deck. Its own gripper jaws are the only rig hardware in frame.
* `handover_cam` and `overview_cam` in `training/` are RealSense **IR nodes with the structured-light
  dot projector active** — every surface is covered in speckle. `overview_cam` is IR in *every*
  session.
* The `recordings/…064308Z` frames labelled `handover_cam` show the **same viewpoint** that
  `training/…073940Z` labels `gripper_left_cam` (pipette descending onto a tube in the orange
  jaws), and they are **640×480** where the training frames are **1280×720**.

That last point is the killer. `core/config.py:139-147` binds a slot to a UVC index or a RealSense
serial from env, `_apply_camera_type` (`core/config.py:151-172`) lets `CAM_*_TYPE` swap the driver
behind a slot, and `_apply_camera_format` (`core/config.py:225-234`) lets the resolution change per
slot. `startup_snapshot.py:3-8` documents the root cause in the repo's own words: "on macOS those
indices are reassigned whenever the rig changes. A slot can therefore come up pointing at a
different camera — or at nothing — with no config change and no error."

**Failure scenario.** Jacobian measured for `handover_cam` at 1280×720 on Monday: `J = [[8.4,…],[…,-8.0]]`
px/mm. Tuesday the USB order changes, or `CAM_HEIGHT_HANDOVER_CAM` is set, and the slot comes up at
640×480 on a *different* physical camera. `jacobian_pose_mismatch` does not fire — the *arm* is
parked at the right waypoint. `J` now over-states px/mm by 2×, so the solved `d_hat` under-states
the offset by 2×; the loop takes 12 damped iterations, never reaches `magnitude_mm < 1.5`, and
`no_progress_abort` never fires because it *is* improving 25 % per iteration. It ends at
`max_iterations` with the tip ~3 mm off and — because per-camera residuals are structurally zero
(B2) — `confidence 0.9, deviation_mm 0.0`. If instead the physical camera changed *view*, the sign
of a column flips and the head walks away from the tube until `clamp_mm` and the iteration cap stop
it, 15 mm at a time.

**Recommended change.**
1. Do not name cameras in `plans/handover.py`. Add `servo_cameras: list[str]` to config and select
   the pair by measured jacobian conditioning (B2), not by id.
2. `camera_jacobians.json` must record `{width, height, camera_fingerprint, measured_at, arm_waypoint}`
   where `camera_fingerprint` is the RealSense serial when available and otherwise a hash of the
   boot snapshot the init plan already takes (§5.2). Load-time mismatch on fingerprint or resolution
   must be a **refusal** (`error.code = "jacobian_camera_mismatch"`), not a `WARNING`. The mechanism
   to compare against already exists: `startup_snapshot` writes one frame per slot every boot for
   exactly this reason.
3. On the evidence, `gripper_left_cam` is a better second view than `handover_cam`, and
   `gripper_cam` should not be in the servo set at all until someone shows it seeing the handover.
   State the camera-selection criterion in the doc so the bench can falsify it in one session.
4. Note in §7 that `overview_cam` is IR-only in every recorded session and is therefore unusable as
   a replay fixture for any colour-dependent step.

---

### B2 — `deviation_mm` is structurally zero per view and blind to the correlated failures it claims to catch; the observability test does not test observability

**Claim.** §7.5: per view, solve the "minimum-norm solution restricted to the axes that view
actually observes", where observed means `‖J[:, a]‖ ≥ 0.15 · max_col_norm`. Then the stacked
weighted least squares gives `deviation_mm` = RMS residual in mm, and "**`deviation_mm` is therefore
the extent to which the two views cannot be explained by any single rigid offset** … it is precisely
what a wrong detection, a stale frame, or a stale jacobian produces."

**Four separate problems.**

**(a) The per-camera `deviation_mm` is always 0.** After restricting to observed axes, the per-view
system is 2 equations in 2 unknowns (the doc's own example: gripper observes {x, z}) —
exactly determined, so **zero residual by construction**. Every
`OffsetOutputs.deviation_mm` is 0.0 and `confidence`'s `exp(-deviation_mm/2.0)` factor is identically
1.0. `best_single` then ranks views on `tip.score · tube.score · coverage` alone — the detectors'
self-assessment, the least trustworthy number in the chain. The field is dead weight that reads as
evidence.

**(b) The fused `deviation_mm` cannot see correlated error.** With two views the residual has one
degree of freedom (the doc says so). But the two most likely failures are *common-mode*:
* Both snapshots are taken back-to-back after one LH nudge (§3.6 body). If both are pre-move (B3),
  both views measure the *same old* offset consistently → residual ≈ 0, `confidence` high → the loop
  re-commands the correction it already applied → overshoot, oscillation, and `no_progress_abort`
  blames the jacobian.
* Both jacobians come from one run of one script in one session. A sign-convention error there
  (`+dx` machine frame vs `+dx` commanded) hits both matrices identically → the stacked system is
  perfectly self-consistent → residual ≈ 0. So the design's own "single most likely real failure"
  (wrong jacobian sign, §3.6) is exactly the one this metric cannot detect. §12's risk table claims
  the opposite.

**(c) The observed-axis test is a magnitude test, not a conditioning test.** `‖J[:,a]‖ ≥ 0.15 ·
max_col_norm` asks only "does the view respond to axis *a*". A camera yawed ~45°, so +x and +z both
move the tip diagonally:

```
J = [[ 6.0, 0.1,  5.5],
     [-6.0, 0.2, -5.8]]
col norms: x 8.49, y 0.22, z 7.99  -> threshold 0.15*8.49 = 1.27  -> observed = {x, z}
restricted 2x2 = [[6.0, 5.5], [-6.0, -5.8]], det = -1.8
singular values ~ 11.66 and 0.154  ->  cond(J_xz) ~ 76
```

Both axes pass the gate. The 2×2 solve turns a 1 px detection error into ~6.5 mm and a 3 px error
(plausible for the classical detector on an IR-speckled frame) into ~20 mm. Residual is still 0,
`deviation_mm` is 0, confidence is high. The doc's worked example
(`[[8.41,0.12,-0.33],[-0.21,0.44,-7.98]]`, cond ≈ 1.06) is well conditioned, which is why the gap is
invisible in the document.

**(d) The weights double-count scale.** `w = confidence / mm_per_px` is applied to *pixel* residuals,
but the mm-per-pixel conversion is already inside `J`. WLS on pixel residuals wants `1/σ_px²`.
Applying scale twice biases fusion toward the zoomed-in camera beyond what the statistics justify —
and the weight is not squared, so it is not a variance in any case.

**Failure scenario (composite).** Gripper camera at 45° yaw as above. Tip detector on a speckled
frame reports `bottom_px` 3 px low. `d_hat` gains ~20 mm of phantom error along the ill-conditioned
direction. `deviation_mm = 0.0`, `confidence = 0.72`, `strategy = "fused"`, no warning. `LHRelative`
clamps to 15, damps to 10.5, and drives the head 10 mm into the deck edge. `LHLimits` does not catch
it because the absolute envelope is not violated mid-deck.

**Recommended change.**
1. Replace the column-norm gate with an SVD of the *restricted* submatrix: keep an axis subset only if
   `σ_min(J_S) ≥ σ_floor` and `cond(J_S) ≤ 10`. Log the singular values in `OffsetOutputs`.
2. Report a real uncertainty: from the fused WLS, `Σ_d = (Jᵀ W J)⁻¹` with `W = diag(1/σ_px²)`; publish
   `sigma_mm = sqrt(diag(Σ_d))` per axis. That *is* the brief's "confidence" in defensible form, and
   it degrades correctly with conditioning, a missing view, and a poor detector score.
3. Rename so neither number pretends to be the other: `residual_offset_mm` (= today's
   `magnitude_mm`) and `view_disagreement_mm` (advisory only). Delete `deviation_mm` from per-camera
   outputs — it can only ever be 0.
4. Add the independent staleness check the residual cannot substitute for: `OffsetOutputs` carries the
   child-side capture timestamp, and the loop fails the iteration if it predates the last
   `LHRelative` completion (B3).
5. Fix the weights to `1/σ_px²`.

---

### B3 — The freshness guarantee is not sound, and `min_seq` is unimplementable from the client the design reuses

**Claim.** §4.6: "`fresh=True` … sends `GET /snapshot?fresh=1&min_seq=<seq at request time>` … The
child blocks on its own frame condition and returns the first frame strictly newer. **This is not
optional** … `min_seq` is the correctness mechanism."

**Three defects.**

1. **The client has no `seq`.** `drivers/camera/remote.py` tracks `_frame_at` (a monotonic
   timestamp, `remote.py:83`, `:294-298`) and nothing else. The multipart parts it parses carry only
   `Content-Type` and `Content-Length` — see the producer at `camera_hub.py:502-503` and the parser
   at `remote.py:344-373`. So "the seq at request time" is a number the parent does not have. §4.3's
   "v2 changes ~nothing in it except making `base_url` point at loopback and adding
   `/snapshot?fresh=`" understates this: it needs an `X-Frame-Seq` part header, a parser change, and
   a whole second HTTP path (`capture_jpeg` today reads the cached buffer, `remote.py:179-187`).
2. **Even with a seq, the anchor is wrong.** "seq at request time" is the seq of the last frame the
   *parent's stream reader* decoded, which lags the child by the reader's decode backlog. `seq+1`
   from a lagging anchor can still be a frame the child grabbed *before* the LH move finished. The
   correct anchor is the child's own latest seq at the moment the move returned.
3. **`seq` counts grabs, not exposures.** `_pump` sets `seq = frame_no`, incremented once per
   `driver.capture()` (`camera_hub.py:220`, `:234`, `:260-263`), and `OpenCVCameraDriver.capture` is a
   bare `self._cap.read()` (`drivers/camera/driver.py:88-94`) with no `CAP_PROP_BUFFERSIZE` and no
   grab-drain. On macOS AVFoundation `read()` returns the *oldest queued* frame, so "seq strictly
   newer" is satisfied by a frame exposed several frame-periods ago — ~200 ms at 15 fps with a 3-deep
   queue, plus encode and transport. That is the same order as the LH settling the loop is observing.

(Note also: the freshness primitive the design credits to `remote.py` already exists server-side at
`camera_hub.py:181-188`, `wait_for_frame(last_seq, timeout=2.0)` — it is the thing that moves into the
child unchanged, and the design should say so.)

**Failure scenario.** Iteration 2: `LHRelative` commands −8.4 mm z and returns. `CameraSnapshot(fresh=True)`
asks for `seq > 4412` (the parent's lagging view); the child is at 4415 and returns 4416, pulled from
the UVC queue and exposed 180 ms before the move completed. Both cameras do this — they are
consecutive actions — so B2(b) applies: the views agree, residual is 0, confidence is high, and the
loop re-commands the −8.4 mm it just made. Iteration 3 overshoots the other way, `no_progress_abort`
fires, `servo_stalled` is reported, and the operator is pointed at the jacobian sign.

**Recommended change.**
1. The child stamps each frame with `seq` **and** `grabbed_at` (`CLOCK_MONOTONIC` is system-wide on
   both macOS and Linux) and emits both as multipart headers and in `/health`.
2. `fresh` becomes `GET /snapshot?after_ts=<T>`, where `T` is taken by the *handler* immediately after
   the preceding motion action returns and carried on the blackboard. Two loopback round-trips is
   ~1 ms; do not optimise it away.
3. The child's pump must drain the queue (`cap.grab()` ×2 then `cap.retrieve()`, plus best-effort
   `CAP_PROP_BUFFERSIZE=1`). Measure the residual latency and put the number in §4.6 as a budget.
4. Keep `STALE_AFTER_S = 5 s` (`remote.py:57`) for the *browser* path and add a much tighter bound for
   the servo path. A 5 s-old frame served as current (`remote.py:205-216`) is fine for an `<img>` and
   catastrophic for a servo.

---

### B4 — During a run the operator's e-stop is disabled and the documented recovery path is impossible

**Claim.** §3.1(2): "the runner tak[es] an exclusive **claim** on every device its plan touches for
the duration of the run; teach endpoints return 409 while a run holds a claim." §3.3: "A `halt`
failure pauses rather than terminates … The operator can then **jog by hand in the teach tab**,
inject a corrective action, and resume — which is the whole point of having pause/inject."

These contradict each other, and the first one is a safety regression.

`backend/app/api/teach.py:462-464` — `POST /api/arms/{id}/stop` deliberately bypasses the per-arm
busy lock, with the module docstring at `teach.py:17-19` saying "an e-stop that waits for the move it
is trying to interrupt would be useless. **Do not 'fix' that.**" §10 keeps `/api/arms/{id}/stop` in
the "unchanged" list. If claims 409 the teach router wholesale, that instruction is exactly what
gets fixed: during a run the only way to stop an arm is `POST /api/engine/abort`, which also brakes
the other arm and retracts the LH.

**Failure scenarios.** (i) `arm.waypoint` drives the right arm toward a mis-taught
`APPROACH_RACK`. The operator hits Stop in the teach tab — 409, `right is claimed by run r_…`. They
must find the Workflow tab and press Abort, which also brakes `left` mid-hold and drops the tube.
(ii) `arm.gripper` fails, the run pauses with the cursor on it (§3.3), and the operator opens the teach
tab to jog the wrist 5 mm clear — 409, because the claim is held "for the duration of the run". The
recovery workflow the whole design is built around cannot be executed.

**Recommended change.**
1. `/stop` and `/clear_errors` are **never** claimable. Same carve-out, same reasoning, as the busy
   lock. State it in §3.1 next to the claim, not by implication.
2. Claims are **released on pause and re-acquired on resume**, with a resume pre-flight that re-reads
   pose/joints (they may have moved by hand) and re-verifies `mode == 0` (the operator may have left
   free-drive on — `drivers/xarm/driver.py:372-388` and `drivers/capabilities/arm.py:130-137` both
   warn about programmed motion in mode 2). This also makes `arm.manual_move` unnecessary as a plan
   action (see S4).

---

### B5 — Loop materialization has an unbounded growth path, and the `best_single` fallback guarantees `servo_stalled`

**Claim.** §3.6: the loop body is deep-copied and spliced into the flat plan per iteration, then
"`plan_replaced` is emitted" with the whole plan. §2.2: `Loop.body: list["Action"]`,
`max_iterations: int = 12`, no `Field` bounds. §3.5: injected actions are "validate[d] … through the
`Action` union".

**(a) Unbounded growth.** `Action` includes `Loop` and `Loop.body` is `list[Action]`, so a loop may
contain a loop, and `max_iterations` has no upper bound in the model. An operator using the §9.4
manual form (or the LLM proposing one) can inject
`Loop(max_iterations=5000, body=[Loop(max_iterations=5000, body=[CameraSnapshot(...)])])`. Every
expansion emits a full-plan `plan_replaced` ("the whole plan. Full replacement, not a patch"), so the
backend broadcasts a quadratically-growing list — O(n²) bytes — and the browser store rebuilds
`actions`/`byAid` on each one. Backend RSS and the socket both die with the arms mid-plan. Nothing in
the design bounds total plan length.

**(b) `best_single` cannot converge.** §7.5: above `max_deviation_mm`, "take the highest-`confidence`
view, and move **only its observed axes**." If the chosen view observes {x, z}, `magnitude_mm =
‖d_hat‖` still includes the y error that `best_single` never moves — so `magnitude_mm` plateaus at
|y|, `no_progress_abort=3` fires, and the loop dies `servo_stalled` **every time `best_single`
triggers**. And it triggers most readily early, when the offset is large and the local jacobian is
being extrapolated — exactly when `deviation_mm` is largest.

**(c) No terminal state for exhaustion.** §3.6 defines `servo_stalled` for no-progress but says
nothing about reaching `max_iterations` with `until` false; `run_finished.status` is
`complete|failed|aborted`, so "converged" and "gave up at 12" are indistinguishable.

**Recommended change.**
1. Split the union: `BodyAction` = `Action` minus `Loop`. Nested loops are not in the brief.
2. `max_iterations: int = Field(12, ge=1, le=25)`, plus a hard `MAX_PLAN_ACTIONS = 500` enforced in
   both splice paths, failing with `plan_too_large` rather than allocating.
3. Do not broadcast the whole plan per expansion: `plan_replaced` for injections only, and
   `actions_appended {after_aid, actions[]}` for expansion. The plan is keyed on `aid`, so an append
   at a known anchor is not a second source of truth about ordering. Collapse iterations older than
   the last two into the loop's summary row — the sparkline §9.3 already wants *is* the summary.
4. Delete `best_single` (S12); evaluate `no_progress` on the axes actually being commanded.
5. Define `Loop` terminal states: `converged | exhausted | stalled | low_observability`, and say which
   map to `run_finished.status = failed`.

---

### B6 — Injection cannot land where the recovery story needs it, and 409s on any move longer than 10 s

**Claim.** §3.5 step 3 rejects an insertion point `<= cursor` ("injecting *at* the cursor is ambiguous
— inject after it and let the operator step"); step 2 waits 10 s for the runner to park and otherwise
409s.

**(a) The primary recovery insertion is rejected.** §3.3 leaves the cursor **on** the failed action
after a `halt`. The operator's need — "insert a corrective jog *before* retrying this action" —
resolves to `before_aid = <failed aid>`, i.e. index == cursor, i.e. rejected. It is not ambiguous when
the runner is parked in the gate: the injected actions run next, then the failed action. The rule
forbids the one case it exists to serve.

**(b) The 10 s deadline is shorter than several planned actions.** §3.8 sets `slow` to 8 °/s, and
`ArmHome` defaults to `slow` — a 90° J6 unwind is ~11 s. `arm.traverse` over 5 waypoints at `medium`
exceeds 10 s. `arm.decap` at `step_deg=90, turns=1` is 8 wrist moves plus 8 grip/release plus
`settle_s=0.3` each, and gates only *between bites*. So during homing, traversal and decap — most of
the plan's wall-clock — injection 409s. And `arm.traverse` is not a gate-caller at all, so pause and
injection both wait out the whole multi-waypoint sequence.

**Failure scenario.** The operator sees the arm stopping 6 mm short and presses Pause: "pausing…" for
9 s while the traverse finishes. Presses Inject: 409, "busy inside a long move". By the time the
traverse ends the next action (`arm.gripper` close) has already begun, because the gate is checked at
the top of the *next* `_run()` iteration — after the cursor advance. They now have to Abort.

**Recommended change.**
1. Allow insertion **at** the cursor when the runner is parked. Reject only `< cursor`.
2. `arm.traverse` must call `ctx.gate()` between waypoints, and `arm.decap` between every step (not
   only bites). Document a gate-cadence budget: no action may go more than ~2 s without a gate call
   except a single uninterruptible `move_joints`.
3. Replace the 409 with a queued injection: accept the mutation, mark it `pending`, apply it at the
   next gate, and emit `plan_replaced` then. The operator's intent does not expire because a
   trajectory is long. Keep 409 only for `abort`-in-progress.

---

### B7 — The logging context filter is installed where Python will not run it, so `GET /api/logs?aid=` returns nothing

**Claim.** §8.1 installs `root.addFilter(ContextFilter())  # stamps run_id/aid/kind/device`, and §8.3:
"A handler that calls into `drivers/` gets driver-level log lines stamped with the action that caused
them **for free** — which is the property that makes 'expand an action to see the logs it produced'
real instead of approximate."

**Why it is wrong.** `Logger.handle()` consults `self.filter(record)` only for records logged
*directly* to that logger; `callHandlers()` then walks ancestors invoking `hdlr.handle(record)`, which
runs *handler* filters and never ancestor *logger* filters. So a record from
`logging.getLogger("drivers.xarm")` never passes through a filter on the root **logger**. All 30
`print()` sites being converted (verified: exactly 30 in `backend/`+`core/`+`drivers/` excluding tests;
zero `import logging` anywhere) live in module loggers. Essentially every diagnostic record lands in
`lab.jsonl` with no `run_id` and no `aid`.

**Failure scenario.** The gripper faults during `arm.gripper`. `drivers/xarm/driver.py:366` logs
"gripper error not cleared". The operator expands action #23 in the UI; `ActionLogs.vue` calls
`GET /api/logs?run_id=r_…&aid=23`; `tail.py` filters on the `aid` field; the record has none; the
panel is empty. The one line that explains the failure is in the file, unattributable. This is a
silent failure — the feature looks implemented and returns zero rows.

**Recommended change.** Attach `ContextFilter` to the **handlers** (`_jsonl_handler` and
`_console_handler`), or do the stamping in the `JsonlFormatter`. Add a test that logs from
`core.perception.tip` inside a bound context and asserts the JSONL line carries `aid`. Also correct
§8.3's phrasing: `contextvars` are per-`Context`, not per-thread — a new `threading.Thread` starts
with an *empty* context, which is what you want here (the runner's context must not leak into
FastAPI's threadpool), but the reasoning as stated is wrong and someone will later "fix" it by
copying the context into worker threads.

**Related, same section:** §2.3 defines `ActionResult.inputs` as "the action's own `model_dump()`,
**post-Ref-resolution**", and §8.2 writes `inputs` into the JSONL line. For `IdentifyTip(frame_ref=Ref(slot="frame:gripper_cam"))`
the resolved value is a 1280×720×3 numpy array. Either `json.dumps` raises inside the log call (the
exact hazard `main._json_safe` and `schemas.FiniteModel` were written for) or you write megabytes of
base64 per action. Refs must be logged as `{slot, artifact_url, sha256}`, never as resolved payloads.

---

### B8 — The websocket protocol cannot reconstruct UI state after a reconnect, and the wire shape is not the schema shape

**Claim.** §3.9: "`seq` is monotonic **per run** so a reconnecting client can tell it missed something
and re-fetch `GET /api/engine/run`." §9.1: "If `event.seq !== state.lastSeq + 1`, the store re-fetches
`GET /api/engine/run`."

**(a) There is no watermark, so the refetch races.** `GET /api/engine/run` returns "plan + results +
status" (§10) with no `seq`. Reconnect goes: open socket → receive `seq=137` → detect gap → fetch
snapshot. Events 138–141 arrive during the fetch; the snapshot may or may not contain them and the
client cannot tell, so it drops or double-applies them. With a `plan_replaced` in that window the plan
ends up with an injected action the client never renders.

**(b) `seq` per run is undefined outside a run.** §3.9 emits `readiness` on `/ws/engine` and §5.1 has
readiness running from boot, before any run. What is the seq of a `readiness` with `run_id = null`?
Two producers, one counter, one scope — the invariant does not hold.

**(c) A fresh client cannot detect loss at all.** Its first event may be `seq=137` against
`lastSeq=0`, so the gap test fires on every connect: the refetch is the normal path, which makes the
race in (a) the normal path too.

**(d) `plan_replaced` uses a model that is not in the OpenAPI schema.** Its rows are
`{aid, index, kind, device, label, speed, state, parent_aid, iteration, params:{…}}`; `Action` (§2.2)
has those fields at top level and no `params` or `state`. So the one wire message the plan visualiser
is built on gets a hand-written TypeScript type after all — the exact drift §9.1 promises to avoid.

**Recommended change.**
1. `seq` is monotonic **per websocket connection process**, not per run. Include `run_id` on every
   envelope including `run_paused`/`run_resumed`/`loop_iteration` (the examples at seq 20–23 omit it
   while §3.9 claims "one envelope").
2. The server sends a synthetic `snapshot` frame as the **first message on connect**, built under the
   same lock that serialises the broadcast. No fetch, no race, no gap test on connect.
3. Keep a per-run ring of the last 512 events and support `/ws/engine?since_seq=N` for replay. Fall
   back to `snapshot` when `N` is older than the ring.
4. Define `ActionRow` as a real pydantic model exported in the OpenAPI schema, and define
   `EngineEvent` as a discriminated union on `type`. Generate both.

---

### B9 — Subprocesses do not make restart a recovery for the hazard that motivated them, and "quit cleanly" is not satisfied

**Claim.** §4.1: "**a thread cannot be abandoned, a process can.**" §12 assumption 4: "The whole §4
supervision design assumes restart is a recovery, not a degradation." §4.5: on handshake timeout
"the child is *recorded as leaked* and the supervisor moves on"; at boot, if a recorded pid "is alive
but unresponsive, `SIGKILL` once and then leave it alone".

**Why the assumption is false for the motivating case.** The hazard (`startup_snapshot.py:12-15`,
quoted verbatim in §4.1) is a child wedged in an **uninterruptible kernel wait inside the UVC open**
that `kill -9` cannot reap — and such a child still **holds the device**. So a restart of that slot
re-opens the same device and wedges identically, burning the 5-restart budget in ~30 s;
`reap_stale` at boot `SIGKILL`s a live unresponsive pid once (no effect) and leaves it, so the device
stays claimed across every subsequent boot until reboot; and `POST /api/cameras/{id}/restart`, offered
as "the explicit human override", cannot work either. This is still a large win over threads — the
*backend* survives and 3 of 4 cameras keep working — but the design must say so plainly: subprocesses
convert "the backend cannot shut down" into "this slot is dead until reboot and one unreapable process
accumulates". As written, assumption 4 reads as "restart fixes it", and the operator gets a retry
button that provably cannot help.

**"Quit cleanly" is also not satisfied, two ways.**
1. `POST /shutdown` is justified as letting the child "release the `VideoCapture` *before* its socket
   dies". But the pump thread is exactly where a dead USB device blocks — `cap.read()`
   (`drivers/camera/driver.py:91`) with no timeout. The HTTP thread answers, sets a flag, and the pump
   never returns, so `cap.release()` never runs and the process does not exit; the ladder escalates to
   `SIGKILL`. The child must bound its wait for the pump and then `os._exit(0)` — a normal interpreter
   exit joins non-daemon threads.
2. The ladder (`/shutdown → +2 s TERM → +5 s KILL → +8 s give up`) is run by "one monitor thread,
   not one per child", "ordered" — i.e. up to 32 s for four children, against §4.5's "shutdown is
   never allowed to block on a camera" and §5.0's "exits cleanly on `Ctrl-C`". Make it a parallel
   fan-out under one global 8 s deadline.

**Recommended change.** State the honest failure semantics in §4.1/§12(4). Replace the per-slot
retry button's copy with something true ("retry — will not help if the device is wedged; reboot
required"). Make shutdown a parallel fan-out with a single global deadline. Specify `os._exit(0)` in
the child after a bounded pump-release wait. Add a `camera_wedged` state distinct from
`permanently_failed`, set when the handshake times out, and do not spend the restart budget on it.

---

### B10 — `ActionResult.simulated` reports pure-compute and mock-driver actions as real, so the brief's "all actions expose a simulation mode" is not met

**Claim.** §2.5: "`ActionResult.simulated` is then derived from the actual driver's
`info.vendor == "mock"` / `meta["live"] is False`, **so a result can never claim to be real when it
was not**." §2.3: "`simulated: bool  # true if ANY device it touched was simulated`". The brief
(per `GAP_ANALYSIS.md` §3): "the brief says **all actions** expose a simulation mode, which is a
property of the engine, not only of the driver."

**Three defects in the derivation.**
1. **Pure-compute actions touch no device.** `SelectOffset` has `device: None = None` (§2.2), and "any
   device it touched" over an empty set is False. So in a full sim run, the action that produces the
   offset the robot moves on is logged and rendered as **real** — and `vision.*` are exactly the
   actions whose provenance matters most.
2. `meta["live"]` **raises on real drivers.** `DeviceInfo.meta` defaults to `{}` (`drivers/base.py:36`)
   and `OpenCVCameraDriver.info` sets `meta={"source": …}` with no `live` key
   (`drivers/camera/driver.py:36`); `XArmDriver` likewise. A dict index on a missing key raises inside
   the result builder.
3. **The vendor test is opt-in by memory.** `RemoteCameraDriver` reports `vendor="proxy"`,
   `meta["live"]=True` (`remote.py:99-106`); `StillImageCameraDriver` sets `live: False`
   (`still.py:52-63`) only because someone remembered. `replay`/`servo_sim` will be written by someone
   who might not.

**Failure scenario.** `HZ_SIM=cameras` with real arms. Every overlay, offset and `selected_offset` in
the JSONL reads `simulated: false`. Someone reviews the run next week and concludes the servo loop
converged on real pixels. It converged on `core/sim/world.py`.

**Recommended change.**
1. Add an explicit `simulated: bool` property to `InstrumentDriver`, set by `build_driver()` from the
   fleet entry, not sniffed from `info`. A driver cannot forget a value the registry assigns.
2. `ActionResult.simulated` becomes `provenance: Literal["real", "simulated", "mixed"]` computed as:
   the union of the devices the action touched **plus the provenance of every blackboard slot it
   read**. `SelectOffset` reading two simulated offsets is `simulated`. This is what makes simulation
   a property of the engine rather than of the driver, which is what the brief asks for.
3. Propagate provenance into the overlay footer (§7.6 already prints `SIM`) and into
   `run_started.simulated`.

---

## Non-blocking findings

**N1 — `SIM_SUBSTITUTIONS` is layered wrong and breaks `HZ_CAMERA_HOST`.** §2.5 rewrites
`entry["type"]`, but under §4.2 the parent-side type is always `camera_proc` and the *child* picks
the inner driver — the table conflates two layers. And `_apply_camera_host` only rewrites entries
whose type is in `CAMERA_TYPES` (`core/config.py:86-88`, `:205-207`), which will not contain
`camera_proc`/`replay`/`servo_sim` — so §4.3's "keeps working for free" is false the moment the type
is renamed. *Fix:* add a `sim_type` field instead of rewriting `type`; extend `CAMERA_TYPES`; test
that `HZ_CAMERA_HOST` still rewrites all four slots. Also, `build_driver` only knows the mocks if
`drivers.mock` was imported (`drivers/mock/__init__.py:313-322`, `drivers/registry.py:21-28`) — put
that import in `device_manager`, not `core/config.py`, which must not import `drivers`.

**N2 — `core/waypoints.py` duplicates `core/teach_poses.py`.** `load()` already returns
`{device_id: {pose_name: entry}}` (`core/teach_poses.py:38-40`; `data/teach_poses.json` has exactly
that shape). The design adds a new module, keeps the old one as "the storage layer", and migrates
`teach_poses.json` → `waypoints.json`, orphaning the 8 taught poses. *Fix:* keep both module and
file; add the new names as data. (See S1.)

**N3 — Missing dependencies make two sections unbuildable.** `pyproject.toml` has no LLM SDK and no
API-key config, but §9.4 requires a server-side LLM call site. `frontend/package.json` has one
runtime dependency (`vue`) — no `vue-router`, no `openapi-typescript`, and **no test runner**, so
every §9 component (stores, the `plan_replaced`-preserves-`results` reconciliation that is the whole
payoff of stable `aid`s) is untestable. `"build": "vue-tsc -b && vite build"` also fails on a stale
generated `src/api/generated.ts`. *Fix:* add `vitest`, a `npm run gen` step, and a CI freshness check.

**N4 — Output models cannot express the states the design describes.** (a) §7.5 uses
`tip.score_sigma_px`/`tube.score_sigma_px`; §2.3 defines only `score`. (b)
`SelectedOffsetOutputs.offset_mm: dict[str, float]` cannot express `best_single`'s "move only its
observed axes" — needs `float | None`, plus a defined `None`→0 rule in `LHRelative`. (c)
`OffsetOutputs.delta_px` is non-optional, so a *successful* detection action with `found=False` has
no representable downstream offset. The design never says whether `found=False` is a failure or a
success; it should be a success, which means `CalculateOffset` needs `found: bool` and `SelectOffset`
needs a defined zero-usable-views behaviour that is a refusal, not a zero offset.

**N5 — Only `tag36h11` is wired, but the brief and §2.2 say "AprilTag/ArUco".**
`core/perception/fiducials.py:34-35` hard-codes `DICT_APRILTAG_36h11`; `identify_family()` (`:204-219`)
is a diagnostic. Either wire a second dictionary or say ArUco is out of scope — do not leave
`CameraSearchCode` implying both.

**N6 — The pipette head is multi-channel, so "identify tip" is ambiguous.** The
`training/…073940Z/gripper_left_cam` frames show an ~8-channel head. §7.2's "fixed offset from the
tag origin" is 8 offsets, only one of which is over the tube. Add `channel` to the tip config and
`TipOutputs`, or state that the target channel is fixed.

**N7 — `ArmDecap` step/turn arithmetic is undefined for non-dividing values.** `step_deg=100,
turns=1` → 3.6 bites; `plan_unscrew` takes an integer (`core/motion/cap_ops.py:69-89`). Validate
that `turns*360/step_deg` is integral, or take `bites: int`. `unscrew_cap`'s summary string also
hardcodes `HALF_TURN_DEG` (`cap_ops.py:118-123`).

**N8 — Two clamps for one quantity.** `LHLimits.max_step_mm = 25.0` (§6.1) and `LHRelative.clamp_mm
= 15.0` (§2.2) bound the same command in two layers with two values. Make the driver authoritative;
let the action only tighten.

**N9 — `arm.traverse` must call `wait_for_idle`.** Blending needs `wait=False` per waypoint and one
wait at the end (`drivers/xarm/driver.py:419-445`; `drivers/capabilities/arm.py:157-165`). Otherwise
the runner advances the cursor while the arm is still moving. Add to §2.4's handler contract: a
motion handler must not return until motion has stopped.

**N10 — `ShapeDetector` reuse needs a shim.** It takes a whole frame and returns *normalized*
polygons/centres, with only `radius_px` in pixels (`core/perception/shapes.py:75-89`, `:99-107`).
§7.3's "candidate circles inside the ROI" needs a pixel-space, ROI-aware call — a small wrapper, but
not the no-change reuse described.

**N11 — `FiducialDetector.preprocess` is an instance method** (`fiducials.py:114-131`). §7.2's
"reused, not reimplemented" means promoting it to a module function; it is stateless apart from the
CLAHE object.

**N12 — `fleet.mock.json` has three cameras, `DEFAULT_FLEET` has four.** `gripper_left_cam` is
absent, so "four camera controllers as subprocesses" is never exercised in the only hardware-free
fleet that exists.

**N13 — `slow = 8 °/s` is slower than the design's own deadlines tolerate.** It makes `ArmHome`
(which defaults to `slow`) a 10–15 s action, colliding with B6's 10 s injection deadline and with
`HOMING` at every boot. Consider `slow = 15 °/s`, reserving 8 °/s for `arm.decap`.

**N14 — 50 runs of retained overlay JPEGs will retain images of bystanders.** The recorded frames
contain three identifiable faces. `data/runs/` is deliberately *not* transient (§7.6). Settle the
retention and gitignore policy before the first run writes there.

**N15 — `Artifact.sha256` has no verifier** (§7.6 justifies it as "verifiable"). Verify it in
`GET /api/runs/{run_id}/artifacts/{name}` or drop the field (S7).

**N16 — Testability without hardware: what genuinely cannot be tested, and should be labelled.** Most
of the design is testable, which is a real achievement — the engine, plan mutation, loop expansion,
handlers against mocks, `LoopbackTransport`'s wire lines, the offset algebra against `core/sim/world`,
`tail.py`, and the whole logging path are all pure-pytest. These are not, and the doc should mark them
`HW-ONLY` rather than implying coverage:
* **The macOS uninterruptible-open path** (§4.1, §4.5) — the entire justification for §4. You cannot
  synthesise an unreapable process. Test the *ladder* with a child that ignores SIGTERM (`signal.SIG_IGN`)
  and a child that never handshakes (`time.sleep(60)`); that covers the policy, not the hazard.
* **Real frame latency and UVC buffer depth** (B3). Measurable only on the bench; publish the number.
* **Jacobian *validity*.** `scripts/calibrate_lh_jacobian.py` in sim recovers the `J` that `servo_sim`
  was configured with — i.e. it tests the fitter's algebra, not that a real camera has a
  well-conditioned constant `J`. §7.4's "the calibration *procedure itself* is unit tested" overstates
  this; say "the estimator is unit tested".
* **The classical tip/tube detectors' accuracy.** No labelled ground truth exists in `temp/`, and (B1)
  no recorded frame from a servo viewpoint contains a tag to anchor against. You can test determinism
  and no-crash on the recorded frames; you cannot test correctness. Budget a labelling pass (30–50
  clicked tip-bottom / tube-top points) — it is a couple of hours and it converts §7 from
  unfalsifiable to measurable.
* **`SerialTransport`** — correctly marked `HW-ONLY` already.
* **The entire frontend** — see N3; there is no test runner.

---

## Where the design is right

Do not relitigate these.

1. **Threads, one worker, asyncio only for fan-out (§3.1)** — and for the right reason: the xArm
   driver has no lock of its own (no `threading` import in 678 lines) and `move_joints(wait=True)`
   blocks for seconds (`drivers/xarm/driver.py:419-436`); `api/teach.py:1-19` already learned this.
   Async buys nothing and risks freezing the pause button.
2. **Simulation as driver substitution, never `if simulate:` (§2.5, §11.10).** The most important
   decision in the document. The mechanism is right; only the *reporting* is broken (B10).
3. **`core/sim/world.py` as the one shared sim fact (§2.5).** The best idea in the doc — closing the
   loop through an offset the mock LH actually decrements is the only thing that makes the wrong-sign
   jacobian bug reproducible in pytest.
4. **Stable `aid` + derived `index`, injection by `after_aid` (§3.2)**, and the resulting ability to
   preserve `results` by `aid` across a renumber (§9.1).
5. **Initialization as a plan on the same runner (§5.2).** Gets logs, states, indexes and pause for
   free and makes "reinitialize a device" literally the boot code. Do not write an imperative boot
   function "just for now".
6. **The OT transport split with `LoopbackTransport` asserting exact wire lines (§6.2).** The right
   answer to a stub whose `_send` returns `None` while every call reports success
   (`drivers/opentrons/driver.py:47-56`); `G91 / G0 X1.500 F1800 / G90` assertions catch exactly the
   bug class that makes it dangerous. `source="dead_reckoned"` is honest and free.
7. **JSONL, one writer, children→stdout→parent (§8.2, §8.4)**, including `camera_stdout_unparsed`
   rather than dropping a line.
8. **Rejecting metric triangulation (§11.8) and a learned detector (§11.7).** Verified: `intrinsics:
   null` in all three manifests, zero depth files on disk.
9. **The deletion graph is genuinely clean** — I traced it. The only salvage edges are
   `core/perception/fiducials.py:30-31` (`markers.spec_for`, `worldmodel.entities.Transform`);
   `annotate()` has zero call sites; `DeckLocation`'s only non-driver consumer
   (`backend/app/workflows/uncap_aspirate.py:13,229`) is itself on the delete list; every twin
   consumer is on the list. §12's "the graph is cut cleanly" is accurate.
10. **`cap_ops` 180° → configurable step.** `plan_unscrew` (`core/motion/cap_ops.py:69-89`) already
    emits the turn/open/unwind/close ratchet the brief's "rewinding between" asks for; only
    `HALF_TURN_DEG` (`:34`) needs parameterising, and 90° bites make `_preflight_turns` (`:131-148`)
    *easier* because the peak wrist excursion halves. Keep net-zero wrist travel and the pre-flight.
11. **Materializing loop bodies as indexed actions (§3.6)** with `parent_aid`/`iteration` indentation.
    (Bound it per B5.)
12. **Propose ≠ apply, server-side, with a mandatory no-LLM manual form (§9.4).**
13. **Pause is cooperative and the UI says "pausing…" (§3.4).** The honest answer; do not let anyone
    "improve" it into a mid-trajectory `driver.stop()`.
14. **No Pinia; add `vue-router` (§9.1, §11.4).**

---

## Factual corrections

| # | Claim in `ARCHITECTURE.md` | Reality | Proof |
|---|---|---|---|
| 1 | §4.3: "v2 changes ~nothing in [`remote.py`] except making `base_url` point at `127.0.0.1:<port>` … and adding `/snapshot?fresh=`" | It has no `seq` concept, no snapshot-over-HTTP client path, and its staleness bound is 5 s. Needs a part-header parser change, a new HTTP path, and a tighter freshness bound. | `drivers/camera/remote.py:57`, `:83`, `:179-187`, `:205-216`, `:344-373` |
| 2 | §4.3 credits `remote.py` with the frame-freshness machinery | The blocking "give me a frame newer than N" primitive is server-side in the hub, not in the client. | `backend/app/services/camera_hub.py:181-188` |
| 3 | §4.6: `min_seq=<seq at request time>` is implementable from the streaming client | The multipart parts carry only `Content-Type` and `Content-Length`; no seq crosses the wire. | producer `camera_hub.py:502-503`; parser `remote.py:344-373` |
| 4 | §4.6 treats "seq strictly newer" as "exposed after the request" | `seq` counts `driver.capture()` calls; `capture()` is a bare `cap.read()` with no buffer drain, so it returns queued (older) frames. | `camera_hub.py:220,234,260-263`; `drivers/camera/driver.py:88-94` |
| 5 | §7.1 / `GAP_ANALYSIS.md` §3: "1 156 real **colour** images" | 4 of the 9 recorded camera folders (`overview_cam` ×3, `handover_cam` ×1) are greyscale RealSense IR frames with the dot projector on. | measured: `temp/{recordings,training}/*/{overview_cam,handover_cam}` — every sampled frame has B==G==R |
| 6 | §7.2/§7.3: tag-anchored tip and tube detection is the **primary** path on `gripper_cam` and `handover_cam` | `gripper_cam`: 0 tags in 53 sampled frames across 2 sessions. `handover_cam`: 0 tags in the 25 sampled training frames (IR). Only 2 distinct ids (188, 218) are ever detected, on deck rails. | measured with `FiducialDetector()` over `temp/` |
| 7 | §12 assumption 1: drift is mitigated because "the jacobian is measured with the arm parked at `LEFT_ARM_LIQUID_HANDLER_DECK`" | That waypoint does not exist; none of the 15 brief names exist. The library has 8 differently-named poses and no `HOME`. | `data/teach_poses.json` → `{right: [cap_grasp, cap_grasp_approach, present_approach, transport_safe, tube_grasp, tube_grasp_approach], left: [tube_hold, tube_hold_approach]}` |
| 8 | §12 risk table: "the depth path is exercised by `MockTagCameraDriver` … which already exists for exactly this" | Unreachable under the new sim mechanism: `SIM_SUBSTITUTIONS` maps every camera type to `replay`, and `mock_tag_camera` is not a substitution target. `intrinsics()` also returns `None` unless `depth: true`. | `ARCHITECTURE.md:560-566`; `drivers/mock/__init__.py:252-255` |
| 9 | §4.3: "`HZ_CAMERA_HOST` keeps working for free" | `_apply_camera_host` only rewrites entries whose `type` is in `CAMERA_TYPES`, which does not and will not contain `camera_proc`/`replay`/`servo_sim`. | `core/config.py:86-88`, `:205-207` |
| 10 | §1 layout: `core/waypoints.py [new] device-scoped waypoint namespace` | `core/teach_poses.py` is already device-scoped with the same shape. | `core/teach_poses.py:38-40`; `data/teach_poses.json` |
| 11 | §2.5: "`ActionResult.simulated` is … derived from `info.vendor == "mock"` / `meta["live"] is False`, so a result can never claim to be real when it was not" | `DeviceInfo.meta` defaults to `{}` and real drivers have no `live` key, so the index raises; and pure-compute actions touch no device, so they report `simulated=False` in a full sim run. | `drivers/base.py:30-36`; `drivers/camera/driver.py:36`; `still.py:52-63` (the one driver that sets `live`) |
| 12 | §7.3: "Reuse `ShapeDetector` for candidate circles inside the ROI" | No ROI parameter; returns normalized coordinates, not pixel geometry. | `core/perception/shapes.py:75-89`, `:99-107` |
| 13 | §8.1: `root.addFilter(ContextFilter())` stamps every record | Logger filters are not applied to records propagated from descendant loggers; only handler filters are. | Python `logging.Logger.callHandlers` semantics; all 30 converted `print()` sites are in module loggers |
| 14 | §3.1(2): claims 409 the teach endpoints "for the duration of the run", and §10 keeps `/api/arms/{id}/stop` unchanged | `/stop` explicitly bypasses the equivalent lock today, with a "do not fix that" comment. Claims as specified re-break it. | `backend/app/api/teach.py:17-19`, `:462-464` |
| 15 | §7.5's example implies the observability gate is a conditioning test | It is a column-norm test; a 45°-yawed camera passes it with `cond ≈ 76`. | worked counterexample in B2(c) |
| 16 | Implicit throughout §7: a fleet id identifies a physical camera at a stable resolution | The same id shows two viewpoints and two resolutions (640×480 vs 1280×720) across two sessions three hours apart. | `temp/recordings/…064308Z/handover_cam` (640×480) vs `temp/training/…073940Z/handover_cam` (1280×720); `startup_snapshot.py:3-8` documents the cause |

---

## Simplification opportunities

The brief is a scope reduction; the design adds ~35 new files. These cuts are safe and I would make
all of them.

**S1 — Delete `core/waypoints.py` and the `waypoints.json` migration.** Use `core/teach_poses.py` +
`data/teach_poses.json`; saves a module, a migration, and the risk of orphaning the 8 taught poses. (N2)

**S2 — Collapse `Ref` + `Blackboard` to five named slots.** The whole system uses `frame:<cam>`,
`tip:<cam>`, `tube:<cam>`, `offset:<cam>`, `selected_offset`. A generic registry plus a dotted-path
resolver is an interpreter you will debug during bring-up. Keep `Ref` only where it must be data
(`LHRelative.offset_ref`); elsewhere let handlers read the previous action's typed output for their
device by convention.

**S3 — Delete `Condition` (`all_of`/`slot`/`field`/`op`/`value`).** One loop, one termination test.
Make `Loop.until` a named predicate (`Literal["offset_converged"]` + thresholds). A mini expression
language for one call site is the definition of gold-plating, and it is the part an LLM is most likely
to author wrongly.

**S4 — Cut the union from 21 kinds to ~14.** Merge `InitializeAll`/`InitializeDevice`
(`device: str | None`); fold `ArmHome` into `ArmWaypoint(waypoint="HOME", speed="slow")`; merge
`IdentifyTip`/`IdentifyTube` into `vision.identify(target=…)`; fold `RenderOverlay` into
`CalculateOffset(overlay=True)` (the plan pairs them 1:1 and an overlay without an offset is never
wanted); **delete `ArmManual`**, which §3.4 refuses unless the run is paused or not running — i.e. a
teach-tab action in a plan action's clothes. Each removed kind is a model, a handler, a TS renderer
and a test.

**S5 — Delete `POST /api/engine/step`.** Not in the brief; a third control mode with its own runner
state, when "pause → inject → resume" already covers the need.

**S6 — Delete run history (`GET /api/runs`, `run.json`, 50-run retention, prune-at-boot).** The brief
asks for a log file and a viewer. This is a second persistence layer with its own retention, pruning
and replay-rendering surface, and the JSONL already carries every `ActionResult`. Removes the whole
retention risk row in §12.

**S7 — Delete `Artifact.sha256`, `bytes`, `width`, `height`.** Nothing consumes them. (N15)

**S8 — Simplify `CameraSearchCode` to `{device, marker_id, timeout_s}`.** Drop `require_all` and the
id list until a plan needs them.

**S9 — Delete `data/runtime/cameras.json` and boot-time adoption.** §4.5/§12(5) already concede
`--reload` is not the supported path, so adoption exists only to make an unsupported mode "merely
untidy". Replace with `just reap` = `pkill -f drivers.camera_proc`.

**S10 — Delete `/ws/logs` and the follower thread.** `GET /api/logs?since_seq=` polled at 1 Hz is
~15 lines. A follower plus inode-change-on-rotation handling plus a second fan-out is ~150 lines
whose failure mode (silently stops following after a rotation) is invisible until you need the log.

**S11 — Defer the LLM inject path** (`backend/app/llm/`, `/inject/propose`, proposal store, TTL, diff
renderer). §9.4 already mandates the schema-generated manual form as a hard requirement — ship that.
The LLM half adds a dependency, a key, a proposal lifecycle and a diff view for a capability the form
already provides, and §12 concedes the LLM is never load-bearing.

**S12 — Delete `SelectOffset.strategy = "best_single"`** — the branch that guarantees `servo_stalled`
(B5b). One weighted solve with per-axis `sigma_mm` plus an explicit `low_observability` refusal is
simpler *and* correct.

**S13 — Frontend: delete `CurrentActionCard.vue`** (it is `PlanChain`'s row at `cursor`) and the
`/workflow/:runId` route (one run at a time, §12(6) — keep `/workflow/:aid`, which is the deep-link
need §9.1 actually justifies).

Net: roughly 10 fewer modules, 7 fewer action kinds, 2 fewer transports, 1 fewer persistence layer.

---

## Answers to the design's open questions

**Q-WP-1 (are the `LEFT_ARM_*` names a typo, or is the acting device?)** The **names** are, and the
fix is to delete the prefix rather than correct it: the key is already `(device_id, name)`, so
`LEFT_ARM_` inside a name stored under `right` is redundancy that will mislead every reader forever
— §12's own risk row exists only because of it. Store `APPROACH_CAP_GRAB`, `TRANSITION_MID_TABLE`,
`LIQUID_HANDLER_DECK` under the acting device. Keep the pre-flight's `(device, waypoint)` listing
regardless. Resolve before anyone teaches a pose: a pose taught under the wrong device is the one
version of this mistake that moves the wrong arm.

**Q-SUBPROC-1 (is camera-only isolation acceptable?)** **Yes — close it as decided, not open.** The
uninterruptible-open hazard is a UVC/AVFoundation property (`startup_snapshot.py:12-15`); the xArm
path is a pure-Python TCP client with no such failure mode in 678 lines of bench-hardened driver.
Putting `move_joints(wait=True)` behind local HTTP would need an out-of-band abort channel and
generous timeouts, and would break the one thing that works well today: a synchronous, in-process,
lock-free motion call. Keep the `camera_proc` seam as the documented pattern, and add one sentence to
§12 saying *why* the arm has no equivalent hazard, so it does not get reopened.

**Q-DEV-1 (does "deviation below threshold" mean the residual offset or two-view disagreement?)**
Terminate on the **residual offset**; never gate termination on disagreement. Rename to
`residual_offset_mm` (terminate `< 1.5`) and `view_disagreement_mm` (warn above ~1.0, never in
`until`). The brief's "confidence and deviation" is then satisfied by two separately defensible
numbers plus the per-axis `sigma_mm` from B2. Gating `until` on disagreement as §3.6 does is actively
harmful: in the single-view case §7.5 sets it from detector-implied uncertainty, which can exceed
1.0 mm forever — so a converged loop runs to `max_iterations` and reports failure while on target.

**Q-TIP-1 (may a fiducial go on the pipette carriage and on the tube/rack?)** Plan for **no on the
pipette, yes on the rack**. The evidence forces it: `gripper_cam` sees no tags at all, and
`handover_cam` sees only the two deck-rail tags — so a carriage sticker is unvalidated on the very
views that would use it, while deck-mounted tags are detected in ~72 % of the colour `handover_cam`
frames. Therefore make the **classical** tip detector the primary path and budget real effort for it
(today it is the least-specified part of §7: five magic constants and a score formula, now with no
tagged fixture to tune against). Anchor the *tube* to a **rack-cell tag** — cheap, evidenced, and it
supplies the identity §7.3 correctly says you otherwise have to guess. Revisit the carriage tag once
one session shows a tag detected from a servo viewpoint.

**Q-PATH-1 (delete dense path teaching?)** **Delete it.** `move_joints(radius=…)` plus
`wait_for_idle()` (`drivers/xarm/driver.py:419-445`) already gives blended traversal of named
waypoints, which is what `arm.traverse` needs. 415 LOC plus a Vue component plus an RDP thinner plus
6 routes for one recorded path is not a defensible ratio. One sanity check first: replay that path's
endpoints as an `arm.traverse` of 3–4 named waypoints in the mock fleet; if the shape is acceptable,
delete. Strip `core.teach_paths`, `core.motion.path_teach`, `path_recorder` and the `/paths*` routes
in one commit — `api/teach.py` imports all three.

**Q-LH-1 (does the OT-One accept relative moves; does anything report absolute position?)**
Unanswerable without the unit, and it should not block: implement `move_relative` as **read → clamp →
absolute move** from day one while keeping relative *semantics* at the ABC. The servo loop needs
relative semantics, never relative *commands*; an absolute move against a readback beats
dead-reckoning plus `G91`; and if the unit is Smoothie G-code then `M114` exists and absolute `G0` is
the better primitive anyway. Keep `source="dead_reckoned"`, the `lh_position_drift` warning, and
`LoopbackTransport`'s exact-line assertions — those pay off either way. This collapses §12(3) from
"a section needs rework" to "a config flag".

**Q-SIM-1 (`HZ_SIM` default `all` or `none`?)** **`none`**, as designed, plus three things that make
the demo case work anyway: (a) `just sim` sets `HZ_SIM=all` and becomes the documented first-run
command in the README; (b) `backend/tests/conftest.py` forces `HZ_SIM=all` so no test can reach for
hardware; (c) if `HZ_SIM=none` and **zero** devices connect, show one banner — "no hardware found —
start with `just sim`" — instead of a fleet of red cards. A bench machine surprised by a simulated
arm is worse than a laptop that needs one extra word on the command line.
