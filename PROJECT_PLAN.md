# Project plan — 24h

Goal: **Cooperative Uncap → Aspirate**, verified. Three owners, one per device, each
owning their driver + the matching capability/verification slice + their share of the
backend and UI.

## Objective — OT-One motion, then vision-driven positioning

**The pipette moves in all directions, so we can build a real-world understanding
with depth perception and have an agent drive the pipette to where the tube
actually is.**

Four steps. The first is done; the rest now have a route rather than a blocker.

1. **Move every axis under software control.** **DONE.** All six axes jog from the
   UI (X, Y, Z, A + plungers B/C). Motion is continuous, not stepped: coordinated
   multi-axis `G0`s queued as one path, +6 ms/leg over an 806 mm 3D sweep. Y jogs
   fine — the fault was only ever *homing* it. Travel measured: X ~375 mm usable,
   Z ~90 mm below the homed top.
2. **A coordinate frame.** **Exists, with a caveat.** Homing zeroes against a
   mechanical stop rather than a switch, but a hard stop is repeatable and the
   position counter **survives reconnection**, so a homed axis carries a real
   machine coordinate. `move_to_machine` commands absolute positions. Y is the
   exception: unhomeable, so no machine coordinate — vision must supply it.
3. **Depth perception → tube pose.** `core/perception/fiducials.py` returns a real
   6-DoF `T_cam_marker`. Open: marker→entity offsets are unmeasured
   (Q-FIDUCIAL-IDS), so poses are numerically placeholder.
4. **Agent-directed motion.** `core/calibration/ot_hand_eye.py` fits the
   camera↔OT transform from paired observations and returns a relative correction
   in OT axes. Tested against a mock camera; needs a real one plus a nozzle marker.

### The property that makes this more than positioning

This machine has **no endstops and no current sensing**. A stalled stepper still
has its steps issued, the move completes in the predicted time, and the counter
keeps counting — so a crash silently corrupts the machine coordinate while every
software signal reports success. Re-observing the nozzle with the camera is the
only thing that can detect it. **Positioning and verification are therefore the
same loop**, which is why the servo correction belongs in the calibration module
rather than being a separate nicety.

## Ownership

| Owner | Device | Owns |
|-------|--------|------|
| **Dale** | Arm (2× xArm Lite 6) | `drivers/xarm`, arm↔arm shared-frame calibration, cooperative ratchet-unscrew + present motion, 3D-printed cap gripper |
| **Lukas** | Liquid handler (Opentrons OT-One) | `drivers/opentrons`, arm↔OT frame calibration, aspiration choreography, orchestrator/state machine + API glue |
| **Di** | Camera | `drivers/camera`, verification agents (cap/grasp/pose/aspiration), perception, camera feeds in UI |

Shared: everyone wires their step into `workflows/uncap_aspirate.py` behind the driver
interfaces, so work proceeds in parallel against mocks.

## Integration contract (so parallel work merges cleanly)

- Drivers implement the capability ABCs in `drivers/capabilities/` — nothing above the
  driver layer imports a vendor SDK.
- The orchestrator only calls capability methods + verification agents.
- Devices are declared in `core/config.py` (`DEFAULT_FLEET`); use mocks by
  registering a fake factory under the same type name.

## Orchestration (landed since scaffold)

Beyond the hardcoded chain in `workflows/uncap_aspirate.py`, a **P0 agent loop** now exists
(`backend/app/agent/` — `engine.py` spine, `policy.py` brain, `tools.py` toolbox; streamed over
`/ws/agent`). It observes → decides → executes a vetted skill → verifies → repeats, defaulting to
an offline `RuleBasedPolicy` (no API key/hardware needed) with a gated `ClaudePolicy` behind
`ANTHROPIC_API_KEY`. It reuses the same skills, twin, and verification agents, so it does not
replace the plan — it realizes the P0 rung of `docs/AGENT_ORCHESTRATION.md`. **Both named blockers are
closed and now committed:** the verifiers are real (`409f562`) *and* `_execute` drives real motion
(`ae94f94`) — it runs a data-driven `CHOREOGRAPHY` table via `_run_act` (capability calls: `move_joints`/
`move_to` gated by `check_joint_target`/`check_pose_target`, `grip`/`release`, OT `aspirate`), replaying
**taught poses** with a loud `preflight` that refuses to start on any untaught pose. `run()` now genuinely
executes → verifies → retries per step (`backend/tests/test_workflow_execute.py`). **The whole stack landed
in git this cycle** (commits `2b0ed34`..`e641f56`; HEAD `e641f56`), so a clean checkout of `agent-loop-p0`
runs the real thing — the multi-cycle "uncommitted WIP" gap is closed (Q-COMMIT-1). Remaining caveats are now
hardware, not code. **The OT transport is NOT a no-op** — `drivers/opentrons/driver.py` has zero `TODO`s and
drives G-code over USB serial; all six axes move on the bench, X travel is measured to ~375 mm usable and Z to
~90 mm, and `move_to_machine` commands absolute coordinates. What OT `aspirate` still needs is the plunger
calibration (Q-OT-PLUNGER-1), a bench measurement, not code. It also needs a taught real bench to move
(Q-POSES-1) — see Risks.

## Timeline

- [x] **H+2** — Direction locked, 2 xArms + OT + cameras reserved, repo cloned, roles set
- [x] **H+6** — Backend boots (FastAPI + REST + WS), UI lists the fleet, teach panel drives an arm. xArm driver connects + moves for real (`drivers/xarm/driver.py` on the real `XArmAPI`, `scripts/init_xarm.py`). Teach layer is now hardened + tested (`backend/tests/test_teach_api.py`, `TeachPanel.vue`/`MoveTo.vue`), and the **camera stream path is mounted** (`main.py:67` includes `cameras.router`; MJPEG `/api/cameras/{id}/stream`, detections on `/ws/state`) with a RealSense RGB-D driver now committed (`368efba`). The **xArm safety/teaching layer** landed in git (commit `3800c2c`): manual **free-drive teaching mode** (`set_free_drive`, xArm mode 2), **joint soft-limit enforcement** (model-table backfill + config `joint_limit_overrides`) and a **cartesian pre-flight** (`check_pose_target`) that refuses moves whose IK lands outside the soft limits, plus an interactive `scripts/find_joint_limit.py` to *measure* the J5 wrist-vs-flange-camera clearance the controller can't model. The **measured J5 override is now in config for both arms** (`FLANGE_CAM_J5_LIMITS = {"5": [-78.4, 95.0]}`, measured 2026-07-25 on one arm and reused for the other, since both carry a flange camera — the second arm is still unmeasured, see Q-JLIMIT-1). Genuine dexterity groundwork, and this cycle it is committed. **The OT-One's real action landed too, on this branch** (not on `feat/ot-one-serial-driver`): `drivers/opentrons/driver.py` has zero `TODO`s and drives G-code over USB serial to the Smoothieboard `v1.0.3`. Verified on the bench — all six axes jog (4 gantry + 2 plungers), continuous coordinated multi-axis motion (806 mm 3D sweep at +6 ms/leg overhead, closing to 0.00 on every axis), tip pickup at a measured 53 mm engagement depth, X travel measured to ~375 mm usable and Z clear to ~90 mm below the homed top, and absolute positioning via `move_to_machine` — the position counter survives reconnection, so a homed axis carries a real machine coordinate. Driveable from the browser (`OpentronsJog.vue` → `/api/liquid-handlers/{id}/jog`), so a click moves the gantry. *Still unverified: a real **aspirate**, which needs the plunger calibrated (Q-OT-PLUNGER-1) — a bench measurement, not code.*
- [~] **H+12** — FLOOR demo: snap-cap uncap + place tube in OT nest + aspirate, end-to-end (guaranteed baseline). *Code-complete **and committed** this cycle (`ae94f94`): `uncap_aspirate.py::_execute` is wired — a data-driven `CHOREOGRAPHY` (left clamps the tube; right pulls the cap straight up, parks it, takes the tube, presents it under the OT tip; OT aspirates) driven through the capability interfaces against **taught poses**, with a `preflight` that refuses to run on any untaught pose and a `run()` that executes→verifies→retries. Asserted by `backend/tests/test_workflow_execute.py`; a clean checkout now runs it. **Not yet demonstrable end-to-end** for two hardware reasons only: the OT `aspirate` transport is a no-op so the pipette never draws (Q-OT-1), and only 2 of the 12 choreography poses are taught so far (`data/teach_poses.json` now on disk, right arm) — the rest and the left arm must still be taught on the real bench before `preflight` will run (Q-POSES-1).*
- [~] **H+16** — Dale: dual-arm ratchet-unscrew stable · Lukas: arm↔OT calibration done · Di: cap-off + aspiration agents returning real verdicts. *Both halves have now landed on disk. Verifiers (previous cycle): all four `core/verification/agents.py` agents return **real verdicts** — `cap_removed` (cap reparented off the tube + separation gate, torque-drop telemetry bonus), `grasp_secure` (tube reparented onto a tool + gripper-width band), `tube_aligned` (distance to the pipette nozzle < 15 mm), `aspiration_ok` (positive aspirated volume) — each with a dedicated `ok=False`/`ok=True` test (`backend/tests/test_verification.py`). Wiring (this cycle): `_execute` now drives the choreography for real, so the real verdicts finally **gate real motion** in `run()`. A **perception→twin fusion loop** feeds the verifiers: `core/perception/fusion.py::TwinFuser` composes marker detections with the camera's twin pose to correct entity positions, driven by `backend/app/services/twin_fusion.py` (started in `main.py` lifespan at ~10 Hz), with a RealSense RGB-D driver (`drivers/camera/realsense.py`) on disk; new `projection.py` (twin→image overlay) and `shapes.py` (Hough-circle detection of untagged labware) round out perception. **All of the above is now committed** (commits `2b0ed34`..`e641f56`; HEAD `e641f56`) — a clean checkout runs the real verifiers and wired motion, closing the multi-cycle uncommitted-WIP gap (Q-COMMIT-1). Still open (hardware/calibration, not code): OT transport no-op (Q-OT-1); calibration hand-eye/world-frame/scan steps TODO so world poses are placeholder (Q-CALIB-1); marker→entity offsets unmeasured; right-arm J5 override unmeasured (Q-JLIMIT-1); the parent-based verifiers (`grasp_secure`, `cap_removed` reparent clause) are hollow on the live bench because `reparent`/motion→twin updates exist only in tests, never in `_execute`/`twin_fusion` (Q-TWIN-COUPLING); dual-arm ratchet-unscrew (TARGET rung) not encoded — the wired path is the snap-cap FLOOR. **This cycle (commits `aa83f21`..`ef69d43` + WIP on disk):** the work landed **off the critical path** — real, welcome robustness, but not the three items that decide the demo. (a) A **still-image camera driver** (`drivers/camera/still.py`) + camera-index safety (`CAM_EXCLUDE_INDICES`) hedge against a lost/unplugged viewpoint. (b) The **world model is now RLock-guarded** with a `lock()` accessor for atomic multi-op sequences (`test_worldmodel_concurrency.py`) — the exact substrate a reparent-on-grasp needs, though nothing wires it yet. (c) **Fixed-camera world-frame calibration is being wired** on disk (`world_board.py` + `extrinsics.py::solve_world_cam` + `pipeline._world_frame`): each fixed camera solves `T_world_cam` from a shared 210/211 board, turning fused geometry from camera-frame toward a real world frame — this materially strengthens the geometry-only verifiers (`tube_aligned`, `cap_removed`-separation), which is the recommended fallback for the hollow parent-based verify moment. Unchanged and still deciding the demo: OT transport no-op (Q-OT-1), pose-teaching stalled at 2/12 (Q-POSES-1, no new poses since 02:00Z), reparent-in-live-path (Q-TWIN-COUPLING).*
- [ ] **H+20** — TARGET: dual-arm screw-cap uncap + arm-held aspiration + verify→retry loop end-to-end; stretch decision
- [ ] **H+22** — Freeze features, rehearse demo, record backup video
- [ ] **H+24** — Present

## Fallback ladder (build bottom-up)

1. **Floor:** snap-cap + OT-nest aspirate (no arm-held). Guaranteed to run.
2. **Target:** screw-cap dual-arm ratchet-unscrew + arm-held aspiration + verify loop.
3. **Stretch:** recap + return; camera visual-servo centering.

## Risks

- ~~**The Opentrons reveal is currently mimed.**~~ **Falsified by this merge.** The real serial driver came in from the integration side: `drivers/opentrons/driver.py` has **zero** `TODO`s and opens a real `serial.Serial`, verified on hardware (all six axes jog, 53 mm tip pickup, a closed 200 mm XY tour back to 0.00/0.00). What is still missing is a real **aspirate** — the plunger axes have never moved and there is no volume calibration, so `aspirate` refuses by design (Q-OT-PLUNGER-1).
- **The wired floor path needs a taught bench to move (co-#1 risk — demo-day, requires hardware; teaching now
  STARTED).** `_execute` replays 12 named taught poses; the unit suite uses a fixture, and the real bench library
  (`data/teach_poses.json`, gitignored) is **now present on disk with 2 of 12 poses taught** on the real arm
  (`cap_grasp_approach`, `cap_grasp`, right arm, saved 2026-07-26T02:00Z). The remaining 10 poses and the **entire
  left arm** are untaught, so `preflight` will still correctly *refuse to run* the full choreography. Real forward
  motion — a bench is connected and being taught — but a green unit suite is not yet a moving demo; the rest must
  be hand-taught on the two-arm + OT rig, which cannot be pre-staged from a laptop (Q-POSES-1).
- **The working demo is now committed (former #1 risk — RESOLVED this cycle).** The wired `_execute`, the four
  verifiers, `TwinFuser`, the `twin_fusion` loop, the RealSense driver, `projection`/`shapes`/`capture`,
  `teach_poses`, and all their tests are committed onto `agent-loop-p0` (commits `2b0ed34`..`e641f56`; HEAD
  `e641f56`). A clean checkout no longer runs theater. Do **not** reopen this by branching the demo off an older
  SHA (Q-COMMIT-1 ANSWERED).
- **Verifiers/fusion gate twin geometry until calibration is real (honesty risk).** The agents query a twin
  populated by calibration seeding + kinematics + camera fusion; hand-eye/world-frame calibration is still `TODO`
  (Q-CALIB-1), so fused world poses are camera-frame until calibrated. A green `tube_aligned` proves the twin is
  consistent, not that the arm physically placed the tube — decide the *minimum* real calibration for the demo.
- **The verify loop's parent-based half is hollow on the live bench (NEW — verification honesty).** `reparent()`
  and motion-driven twin pose updates are exercised **only in tests** — there are **zero** `reparent` calls in
  production `backend/app/` or `core/` code, and neither the choreography/`_execute` nor `twin_fusion` mutates
  parent links on manipulation. So `grasp_secure` (tube parented to a `TOOL`) and `cap_removed`'s reparent clause
  can never turn true from a real grasp until arm-FK pose + manipulation-time reparenting are wired into the live
  loop; the retry loop then degrades to perception-geometry-only for those steps. For Track C — whose *other* half
  is physical verification — this is as on-theme a threat as the OT no-op. Wire a minimal reparent-on-grasp/place
  into `_execute` (mirroring what the tests already do), or scope the demo's verify moment onto a geometry-only
  predicate (`tube_aligned`/`cap_removed` separation) that perception can actually drive (Q-TWIN-COUPLING; team
  `docs/INTEGRATION_PLAN.md`).
- Dual-arm unscrew is the long pole → Dale starts first; the wired path is the snap-cap **floor** (straight-up
  cap lift, no ratchet); dual-arm ratchet-unscrew (TARGET) is not yet encoded in the choreography.
- Arm↔OT alignment → wide-mouth tube / funnel lead-in for slack.
- Live chaining → the verify→retry loop is now a real, wired safety net (verdicts can fail *and* gate real
  motion); script a deliberate failure injection for the demo (Q-DEMO-1).
- Camera feed is now wired (was the top infra gap last cycle): `main.py` mounts `cameras.router`, so
  `/api/cameras/{id}/stream` (MJPEG) + `/detections` are reachable and per-camera detections ride
  `/ws/state`. The vision-verifier path is no longer blocked at the transport — what's missing is the
  verifier itself (see top risk), not the plumbing.
- Fiducial poses will still be wrong until measured: `core/calibration/markers.py::MARKER_MAP` now uses
  **real** printed stock ids (`tag36h11` 180–224) but keeps `identity()` marker→entity offsets
  (0.02 placeholder). Measure the marker→entity offsets before trusting twin poses for any verifier.
- **Prioritization risk — now fully resolved.** Three straight reviews named two code blockers (real verifiers,
  a wired `_execute`); both went real and, this cycle, both got **committed** — so real verdicts gate real motion
  on a clean checkout. Q-PRIORITY-1 and Q-COMMIT-1 are both answered. The remaining work is purely hardware
  bring-up on the critical path: **teach the bench** (Q-POSES-1) and **wire the OT serial transport / merge
  `feat/ot-one-serial-driver`** (Q-OT-1). Do **not** open new substrate — spend the remaining time on the two-arm
  + OT rig, not the codebase.
- **Allocation drift — NEW, watch this.** This cycle's effort (still-cam hedge, worldmodel concurrency guard,
  fixed-camera world-frame calibration) is honest, useful work — but it is **off the critical path**: all three
  deciding items (OT serial merge, finish teaching 12 poses, wire one reparent line *or* commit to a geometry-only
  verify moment) stood still for a full cycle. The calibration WIP is the most defensible of the three because it
  strengthens the geometry-only verifier fallback — but it is still uncommitted and the board spacing is unmeasured.
  With the clock running, the next block must spend on the rig and the OT merge, not more perception polish.
