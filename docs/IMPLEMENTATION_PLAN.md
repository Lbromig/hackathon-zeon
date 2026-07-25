# Implementation plan — demo-first (remaining hackathon hours)

How we get from the current stubs to a live **uncap → transport → present → aspirate**
run with **background verification** and **closed-loop recovery on error**. Ordered by
risk: the whole verify→recover loop works on markers + telemetry first; the learned
perception (SAM2 / FoundationPose / Kaolin) is layered on top only if time allows.

Companion docs: `WORLD_MODEL_REQUIREMENTS.md` (the FR-* this implements),
`DIGITAL_TWIN.md` (entities/frames), `ARCHITECTURE.md` (where each piece lives),
`AGENT_ORCHESTRATION.md` (bounded-normal + gated-recovery modes).

---

## 0. Decisions locked

- **Pose stack: FoundationPose standalone (NVlabs repo + Docker), called as a service.**
  No ROS. We wrap `run_demo.py` as a tiny socket/HTTP endpoint that takes
  `(rgb, depth, mask, mesh_key)` and returns a 4×4 pose. Rationale: on a single GPU box
  with no existing ROS 2, Isaac ROS costs hours before first pose; the standalone image
  runs today. Non-commercial GitHub build is fine (see NFR-LICENSE-1). Isaac ROS is a
  post-hackathon upgrade.
- **Markers are the always-on truth; learned perception is corrective.** FoundationPose is
  **not** on the critical path — it's Tier 2.
- **Primary tube: 50 mL** (`family="50ml"`, CAD already wired in `core/worldmodel/`).

---

## 1. Tiers (build bottom-up, ship whatever we reach)

| Tier | What works | Perception used | Maps to fallback ladder |
|------|-----------|-----------------|-------------------------|
| **T0 — guaranteed** | Full loop end-to-end: kinematics+ArUco twin, verification as twin-queries + driver telemetry, deliberate failure injection, scripted recovery | ArUco + FK only | "Floor" + verify loop |
| **T1 — strong demo** | Live segmentation overlay in UI; `cap_removed` gets a **second** signal from Kaolin render-compare (threads visible) | + SAM2/Grounded-SAM, Kaolin | "Target" |
| **T2 — stretch** | Markerless 6-DoF tube tracking; visual-servo re-align in recovery | + FoundationPose CAD tracking | "Stretch" |

**Cut rule:** if a tier isn't stable ~2 h before freeze, stop and polish the tier below.

---

## 2. The thin vertical slice (do this first, before anything fancy)

Get *one* step passing its verifier through the real loop, so the wiring is proven:

1. Pick `present` → `tube_aligned` (pure twin query, no vision).
2. Implement `TubeAlignedAgent.verify()` as a twin distance check (§4.4).
3. Seat a 50 mL tube in the twin at calibration (`add_tube_with_cap(..., "50ml")` already does this).
4. Run `/ws/workflow`; confirm the UI shows `present → verifying → passed`.
5. Nudge the target so the distance exceeds τ → confirm `retrying` → `failed → needs help`.

Once that round-trips, every other agent is the same shape. This is ~1 h and de-risks the
entire architecture.

---

## 3. Workstreams by owner (parallel)

Owners from `PROJECT_PLAN.md`. Each owns their slice of all three layers.

- **Dale — arms:** dual-arm uncap motion + cap gripper; `grasp_secure` telemetry (gripper
  width + torque); recovery motions (regrasp, re-present).
- **Lukas — OT + orchestrator:** arm↔OT calibration, aspiration choreography, the
  background verifier task, the recovery controller, orchestrator glue + `/ws` events.
- **Di — camera + perception:** camera intrinsics/extrinsics, ArUco world frame + marker
  map, segmentation service, FoundationPose service, render-compare for `cap_removed`.

Shared contract stays: nothing above `drivers/` imports a vendor SDK; perception writes to
the twin via `WorldModel.set_world_pose(...)`; agents return `VerificationResult`.

---

## 4. Component-by-component (what code goes in which stub)

### 4.1 Calibration — `core/calibration/pipeline.py` (Di) — T0

Fill the stubbed steps with OpenCV:

- `_camera_intrinsics`: ChArUco capture → `cv2.calibrateCamera`; cache `calib/intrinsics/*.json`.
- `_hand_eye`: 15–20 arm poses viewing a fixed ChArUco → `cv2.calibrateHandEye` (TSAI) →
  `gripper_cam → right_tcp`; cache `calib/hand_eye.json`.
- `_world_frame`: detect ArUco board + printed ruler → world origin + metric scale.
- `_arm_to_arm`: markers on both bases (or a shared board) → `left_base`/`right_base` in world.
- `_locate_instruments`: ArUco on the OT → `set_world_pose("ot_base", T)` (skip the orbit
  scan for the demo; a single marker is enough).
- `_detect_consumables`: keep the seeded 50 mL tube for now; upgrade to vision in T1.

**Shortcut if calibration eats time:** hand-measure/teach `ot_base`, `rack`, `dropzone`
poses and hard-code them in `calib/`. The twin doesn't care how a pose was obtained.

### 4.2 Background verifier — new `core/verification/loop.py` (Lukas) — T0  · **P1**

An async task, started on app boot, that runs continuously (target 5–15 Hz), independent
of workflow steps:

```
loop:
  ev = collect_evidence(dm)            # latest frames + telemetry
  refresh_dynamic_poses(wm, ev)        # FK always; perception if available (T2)
  for name, agent in AGENTS.items():
      verdicts[name] = agent.verify(ev) # with hysteresis
  publish(verdicts)                    # push onto /ws/state alongside the twin snapshot
```

Wire it into `backend/app/main.py` startup and merge `verdicts` into the `/ws/state`
snapshot already served by `api/workflow.py`. The step-gated `AGENTS[...].verify()` call in
`workflows/uncap_aspirate.py` stays — it just reads the same agents.

### 4.3 Verification agents — `core/verification/agents.py` (all) — T0→T1

Replace each stub `verify()` (currently `ok=True, confidence=0.0`):

- **`TubeAlignedAgent`** (T0, twin-only): `ok = wm.distance("tube_1","nozzle_target") < τ_align`.
- **`GraspSecureAgent`** (T0, telemetry): gripper width in expected band **and** (T1) tube
  mask present in frame.
- **`CapRemovedAgent`** (T0 torque → T1 vision): T0 = torque drop on the turning arm + cap
  entity reparented off the tube in the twin; T1 = add Kaolin render-compare "threads
  visible / cap-gone" score and fuse (require ≥2 signals).
- **`AspirationAgent`** (T0): OT reported aspirated volume > 0 (+ optional liquid-level delta in T1).

Add hysteresis (verdict flips only after N consistent ticks) so the background stream
doesn't chatter (FR-VER-4).

### 4.4 World-model plumbing (Lukas/Di) — T0

- `nozzle_target`: add an entity (or computed pose) for where the pipette expects the tube;
  `tube_aligned` measures against it.
- Reparenting on manipulation is already in `entities.py` — call `wm.reparent(...)` from the
  motion callbacks (`ArmDriverMover.on_attach/on_detach`) so pick/uncap update the twin.
- Confidence + staleness gating on `set_world_pose` when perception writes (T2).

### 4.5 Recovery controller — new `core/motion/recovery.py` (Lukas/Dale) — T0→T2 · **P2**

Today the orchestrator's "retry" just re-runs `_execute` (blind). Upgrade it: on a failed
verdict, dispatch a **recovery behavior** keyed by the failed predicate, then let the
existing loop re-verify.

```
RECOVERIES = {
  "tube_aligned": realign_present,   # T0: re-drive to taught pose; T2: visual-servo to pose error
  "grasp_secure": regrasp,           # open, re-approach, re-close
  "cap_removed":  retry_unscrew,     # extra ratchet turn
  "aspiration_ok": reaspirate,
}
```

Keep it **bounded** (`MAX_ATTEMPTS` already = 3) and on exhaustion stop + surface
"needs help" (already implemented). This *is* the closed-loop-only-on-error path: nominal
motion is open-loop; recovery is the only place perception drives motion.

**Demo money shot:** deliberate failure injection — nudge the tube after `present` so
`tube_aligned` fails → `realign_present` corrects → verdict flips → chain continues.

### 4.6 Perception service — new `core/perception/` (Di) — T1/T2

- `segment.py` (T1): Grounded-SAM 2 — Grounding DINO text prompt ("50 ml tube", "cap",
  "pipette nozzle") → SAM2 masks; publish masks for the UI overlay and to seed pose.
- `pose.py` (T2): thin client to the FoundationPose Docker service; input mask + depth +
  `mesh_path("tube_50ml_base")`; output pose → `wm.set_world_pose("tube_1", T)` with confidence.
- `render_compare.py` (T1): Kaolin differentiable renderer scores the CAD cap at its
  hypothesized pose vs. the frame → the second `cap_removed` signal.

Run perception in its **own process/thread**; the twin is the only shared state (NFR-PERF-2).

---

## 5. Sequenced work blocks

Rough order and size; run the three owners in parallel within each block.

| Block | Goal (exit criteria) | Owners | Tier |
|-------|----------------------|--------|------|
| **B1 (~1 h)** | Thin vertical slice (§2) round-trips: `present` passes and fails on injection | Lukas | T0 |
| **B2 (~2 h)** | Calibration real (or taught) → twin has correct `ot_base`/rack/dropzone; 50 mL tube seated | Di | T0 |
| **B3 (~2 h)** | All four agents return real T0 verdicts (twin + telemetry); reparenting wired from motion | all | T0 |
| **B4 (~1 h)** | Background verifier live; `/ws/state` streams verdicts; UI shows them | Lukas | T0 |
| **B5 (~1 h)** | Recovery behaviors per predicate; failure-injection demo works end-to-end | Dale/Lukas | T0 |
| **— T0 COMPLETE: guaranteed demo in the can. Record backup video. —** | | | |
| **B6 (~2 h)** | Grounded-SAM 2 masks in UI; `cap_removed` fused with render-compare | Di | T1 |
| **B7 (~2 h)** | FoundationPose Docker service up; markerless tube pose into twin | Di | T2 |
| **B8 (~1 h)** | Visual-servo re-align in recovery uses FoundationPose pose error | Dale/Di | T2 |
| **Freeze (~2 h)** | Feature freeze, rehearse, re-record demo at best stable tier | all | — |

---

## 6. Integration checkpoints

- After **B1**: the event contract (`/ws/workflow`) is frozen — everyone codes to it.
- After **B4**: `/ws/state` verdict shape frozen — UI builds against it.
- Everyone tests against **mocks first** (fake driver factories under the same type name,
  per the integration contract) so parallel work merges before hardware is free.

## 7. Demo script (what the audience sees)

1. Calibrated twin boots; fixed rig + arm cam in one frame; 50 mL tube tracked live.
2. Dual-arm uncap → `cap_removed` flips (torque + render-compare).
3. Transport + present → `tube_aligned` passes.
4. **We nudge the tube** → `tube_aligned` fails → recovery re-aligns → passes → resumes.
5. Aspirate → `aspiration_ok`. If any step exhausts retries, it halts with "needs help" —
   which is itself a feature (it didn't aspirate into the wrong place).

## 8. Risk cuts (where to bail)

- Calibration flaky → hand-teach poses into `calib/` (§4.1 shortcut).
- SAM2/FoundationPose won't stabilize → stay at T0; markers carry the demo.
- Dual-arm unscrew unstable → snap-cap fallback from the ladder; the loop is unchanged.
- GPU/Docker time sink → T1 render-compare needs only Kaolin (no FoundationPose), so a
  markerless-pose failure doesn't cost the second `cap_removed` signal.

## 9. Still open (blocks nothing in T0)

- Depth source on the fixed rig (RealSense per cam vs arm-cam only) — needed for T2.
- Real tube-rack CAD → fixes `WELL_PITCH` / slot pose (provisional 30 mm now).
- Recovery authority: arms-only vs arms+OT for the demo.
