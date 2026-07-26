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
- [x] **H+6** — Backend boots (FastAPI + REST + WS), UI lists the fleet, teach panel drives an arm. xArm driver connects + moves for real (`drivers/xarm/driver.py` on the real `XArmAPI`, `scripts/init_xarm.py`). Teach layer is now hardened + tested (`backend/tests/test_teach_api.py`, `TeachPanel.vue`/`MoveTo.vue`), and the **camera stream path is mounted** (`main.py:67` includes `cameras.router`; MJPEG `/api/cameras/{id}/stream`, detections on `/ws/state`) with a RealSense RGB-D driver now committed (`368efba`). The **xArm safety/teaching layer** landed in git (commit `3800c2c`): manual **free-drive teaching mode** (`set_free_drive`, xArm mode 2), **joint soft-limit enforcement** (model-table backfill + config `joint_limit_overrides`) and a **cartesian pre-flight** (`check_pose_target`) that refuses moves whose IK lands outside the soft limits, plus an interactive `scripts/find_joint_limit.py` to *measure* the J5 wrist-vs-flange-camera clearance the controller can't model. The **measured J5 override is now in config for both arms** (`FLANGE_CAM_J5_LIMITS = {"5": [-78.4, 95.0]}`, measured 2026-07-25 on one arm and reused for the other, since both carry a flange camera — the second arm is still unmeasured, see Q-JLIMIT-1). Genuine dexterity groundwork, and this cycle it is committed. *OT driver's one real action (a serial aspirate) is still unverified here — its `connect()`/`_send()` are `TODO`; real OT serial work lives on `origin/feat/ot-one-serial-driver`, not merged into this branch.*
- [~] **H+12** — FLOOR demo: snap-cap uncap + place tube in OT nest + aspirate, end-to-end (guaranteed baseline). *Code-complete **and committed** this cycle (`ae94f94`): `uncap_aspirate.py::_execute` is wired — a data-driven `CHOREOGRAPHY` (left clamps the tube; right pulls the cap straight up, parks it, takes the tube, presents it under the OT tip; OT aspirates) driven through the capability interfaces against **taught poses**, with a `preflight` that refuses to run on any untaught pose and a `run()` that executes→verifies→retries. Asserted by `backend/tests/test_workflow_execute.py`; a clean checkout now runs it. **Not yet demonstrable end-to-end**, but the gap narrowed hard this cycle: teaching **unstalled to 8 poses across both arms** (`data/teach_poses.json`: right 6, **left 2 newly taught** — `tube_hold`/`tube_hold_approach`, saved 05:14–05:24Z), so `preflight` is closer to passing. The **one remaining hardware blocker on the reveal** is the OT `aspirate` transport no-op — the pipette never draws until `feat/ot-one-serial-driver` merges (Q-OT-1). Finish teaching the rest (`cap_lift`, `cap_dropoff*`, `present_ot`, more left poses) and the floor path runs (Q-POSES-1).*
- [~] **H+16** — Dale: dual-arm ratchet-unscrew stable · Lukas: arm↔OT calibration done · Di: cap-off + aspiration agents returning real verdicts. *Both halves have now landed on disk. Verifiers (previous cycle): all four `core/verification/agents.py` agents return **real verdicts** — `cap_removed` (cap reparented off the tube + separation gate, torque-drop telemetry bonus), `grasp_secure` (tube reparented onto a tool + gripper-width band), `tube_aligned` (distance to the pipette nozzle < 15 mm), `aspiration_ok` (positive aspirated volume) — each with a dedicated `ok=False`/`ok=True` test (`backend/tests/test_verification.py`). Wiring (this cycle): `_execute` now drives the choreography for real, so the real verdicts finally **gate real motion** in `run()`. A **perception→twin fusion loop** feeds the verifiers: `core/perception/fusion.py::TwinFuser` composes marker detections with the camera's twin pose to correct entity positions, driven by `backend/app/services/twin_fusion.py` (started in `main.py` lifespan at ~10 Hz), with a RealSense RGB-D driver (`drivers/camera/realsense.py`) on disk; new `projection.py` (twin→image overlay) and `shapes.py` (Hough-circle detection of untagged labware) round out perception. **All of the above is now committed** (commits `2b0ed34`..`e641f56`; HEAD `e641f56`) — a clean checkout runs the real verifiers and wired motion, closing the multi-cycle uncommitted-WIP gap (Q-COMMIT-1). Still open (hardware/calibration, not code): OT transport no-op (Q-OT-1); calibration hand-eye/world-frame/scan steps TODO so world poses are placeholder (Q-CALIB-1); marker→entity offsets unmeasured; right-arm J5 override unmeasured (Q-JLIMIT-1); the parent-based verifiers (`grasp_secure`, `cap_removed` reparent clause) are hollow on the live bench because `reparent`/motion→twin updates exist only in tests, never in `_execute`/`twin_fusion` (Q-TWIN-COUPLING); dual-arm ratchet-unscrew (TARGET rung) not encoded — the wired path is the snap-cap FLOOR. **This cycle (commits `aa83f21`..`ef69d43` + WIP on disk):** the work landed **off the critical path** — real, welcome robustness, but not the three items that decide the demo. (a) A **still-image camera driver** (`drivers/camera/still.py`) + camera-index safety (`CAM_EXCLUDE_INDICES`) hedge against a lost/unplugged viewpoint. (b) The **world model is now RLock-guarded** with a `lock()` accessor for atomic multi-op sequences (`test_worldmodel_concurrency.py`) — the exact substrate a reparent-on-grasp needs, though nothing wires it yet. (c) **Fixed-camera world-frame calibration is being wired** on disk (`world_board.py` + `extrinsics.py::solve_world_cam` + `pipeline._world_frame`): each fixed camera solves `T_world_cam` from a shared 210/211 board, turning fused geometry from camera-frame toward a real world frame — this materially strengthens the geometry-only verifiers (`tube_aligned`, `cap_removed`-separation), which is the recommended fallback for the hollow parent-based verify moment. Unchanged and still deciding the demo: OT transport no-op (Q-OT-1), pose-teaching stalled at 2/12 (Q-POSES-1, no new poses since 02:00Z), reparent-in-live-path (Q-TWIN-COUPLING). **Next cycle (commits `cfa8aac`..`95328c7`):** the team **committed** the fixed-camera world-frame calibration that was WIP last cycle (`cfa8aac` — last review's recommendation, done) and added a **top-down world-map visualization** (`core/viz/scene.py`, `/api/worldmodel/scene[.svg]`, `worldmap.html` + `WorldMapTab.vue`) that renders both fixed cameras in the *same* solved frame — a genuine showable asset and the honest substrate the geometry-only verifiers need. A **remote camera driver** (`drivers/camera/remote.py`, `39bdca6`) lets a second machine run the whole stack against the bench's cameras over MJPEG. All welcome — but again **off the critical path**: the three deciding items (OT serial merge, finish teaching 12 poses, wire the reparent line) **stood still for a third review running**, and the board spacing the world frame needs is still an unmeasured `TODO(measure)`. **This cycle (uncommitted on disk):** for the first time in four reviews, new work landed **on** one of the three deciding items — an **arm-FK → twin kinematics loop** (`core/kinematics.py` + `backend/app/services/kinematics.py` + `test_kinematics.py`, wired into `main.py` `kinematics.start()`) writes each connected arm's live TCP into `{arm}_tcp.local` at ~12 Hz, so the twin's moving parts now move with the real arm — the **motion→twin half of Q-TWIN-COUPLING** (also lands the world-map frontend tab, `WorldMapTab.vue` + `api/worldmodel.ts`). Real progress on the critical path at last — but with two caveats that keep it from being the demo-mover: it is **the laptop-doable half** (the manipulation-time **`reparent` line is still unwritten**, so `grasp_secure`/`cap_removed` still can't fire from a real grasp), and it is **uncommitted/untracked** — not in git, so a clean checkout still lacks it (a fresh instance of the Q-COMMIT-1 failure the project already paid for). And the two **room-only** deciding items — the OT serial merge (Q-OT-1) and pose-teaching (Q-POSES-1, still 2/12, `teach_poses.json` mtime unmoved) — **stood still for a fourth review running.** See the escalated allocation-drift risk below. **This cycle — the four-review stall finally broke on two of three deciding items (commit `8a52ec4` "Vision system etc." + working-tree WIP):** (i) the arm-FK kinematics loop was **committed** (`8a52ec4`) and remains wired into `main.py`'s lifespan — Q-KIN-1's "uncommitted" risk is closed. (ii) The **reparent half of Q-TWIN-COUPLING is now written** in the working tree: `uncap_aspirate.py` gains `_apply_twin_effect` and the `CHOREOGRAPHY` grip/release acts carry `attach=`/`to=` ids, so a real grasp calls `wm.reparent(...)` in production for the first time — and the ids are entity-consistent (`tube_1` is seeded by `pipeline.py:125`; `left_tool`/`right_tool`/`dropzone` exist), so it will actually fire once committed. (iii) **Pose-teaching unstalled**: 2/12 → **8 poses across both arms**, the **left arm taught for the first time** (`tube_hold`/`tube_hold_approach`). Only **Q-OT-1 (the Opentrons serial transport) still mimes** — now the lone room-only decider. Also this cycle: the Docker infra was removed (`8a52ec4`) — the stack runs directly, matching the bench (Q-DEPLOY-1). Caveat: the reparent WIP is uncommitted, so a clean checkout still lacks it — commit it with the pose work.*
- [~] **H+20** — TARGET: dual-arm screw-cap uncap + arm-held aspiration + verify→retry loop end-to-end; stretch decision. *Partial this cycle: the **ratchet-unscrew — the TARGET rung's signature dexterity move — now exists in code and is thoroughly tested** (`core/motion/cap_ops.py` + `backend/tests/test_cap_ops.py`, 12 tests; working tree, uncommitted). It respects the tool-cabling limit (180° bites, net-zero wrist travel, J6 pre-flighted) and is exposed via `POST /api/teach/{id}/cap` + a `CapTools.vue` panel, plus hand-guided **path teaching** (`core/motion/path_teach.py` deadband+RDP, `core/teach_paths.py`, `path_recorder.py`). **But** it is **not wired into the auto `CHOREOGRAPHY`** (which is still the FLOOR snap-cap) and is **uncommitted** (Q-CAPOPS-1, Q-COMMIT-2) — so it's a manual teach-panel capability today, not part of the end-to-end run. This raises the dexterity ceiling and strengthens the "dexterity + verified cap-removed" fallback even if the OT never draws; arm-held aspiration + the full verify→retry loop still wait on the OT transport (Q-OT-1) and a taught bench (Q-POSES-1).*
- [ ] **H+22** — Freeze features, rehearse demo, record backup video
- [ ] **H+24** — Present

## Fallback ladder (build bottom-up)

1. **Floor:** snap-cap + OT-nest aspirate (no arm-held). Guaranteed to run.
2. **Target:** screw-cap dual-arm ratchet-unscrew + arm-held aspiration + verify loop.
3. **Stretch:** recap + return; camera visual-servo centering.

## Risks

- ~~**The Opentrons reveal is currently mimed.**~~ **Falsified by this merge.** The real serial driver came in from the integration side: `drivers/opentrons/driver.py` has **zero** `TODO`s and opens a real `serial.Serial`, verified on hardware (all six axes jog, 53 mm tip pickup, a closed 200 mm XY tour back to 0.00/0.00). What is still missing is a real **aspirate** — the plunger axes have never moved and there is no volume calibration, so `aspirate` refuses by design (Q-OT-PLUNGER-1).
- **The wired floor path needs a taught bench to move (demo-day, requires hardware; teaching STALLED AGAIN this cycle).**
  `_execute` replays named taught poses; the real bench library (`data/teach_poses.json`, gitignored) is **unchanged at 8
  poses across both arms** this cycle (mtime still 05:24Z, no new teaching this run) — right (6): `cap_grasp_approach`,
  `cap_grasp`, `tube_grasp_approach`, `tube_grasp`, `transport_safe`, `present_approach`; left (2): `tube_hold_approach`,
  `tube_hold`. Both arms are started, neither finished. Still untaught: `cap_lift`, `cap_dropoff`, `cap_dropoff_retreat`,
  the `present_ot`/aspirate poses, and more left-arm poses, so `preflight` still correctly refuses the full choreography.
  Momentum from last cycle stopped — new manipulation code (the ratchet-unscrew) landed instead. Finish teaching the poses
  on the two-arm + OT rig (cannot be pre-staged from a laptop) and the floor path moves (Q-POSES-1).
- **The working demo is now committed (former #1 risk — RESOLVED this cycle).** The wired `_execute`, the four
  verifiers, `TwinFuser`, the `twin_fusion` loop, the RealSense driver, `projection`/`shapes`/`capture`,
  `teach_poses`, and all their tests are committed onto `agent-loop-p0` (commits `2b0ed34`..`e641f56`; HEAD
  `e641f56`). A clean checkout no longer runs theater. Do **not** reopen this by branching the demo off an older
  SHA (Q-COMMIT-1 ANSWERED).
- **Verifiers/fusion gate twin geometry until calibration is real (honesty risk).** The agents query a twin
  populated by calibration seeding + kinematics + camera fusion; hand-eye/world-frame calibration is still `TODO`
  (Q-CALIB-1), so fused world poses are camera-frame until calibrated. A green `tube_aligned` proves the twin is
  consistent, not that the arm physically placed the tube — decide the *minimum* real calibration for the demo.
- **The verify loop's parent-based half is now written in code — both halves of the twin coupling exist (last gap: commit it).**
  This cycle the **arm-FK → twin** motion coupling was **committed** (`8a52ec4`, wired in `main.py` lifespan) — Q-KIN-1's
  uncommitted risk is closed. And the **reparent half is now written** (working tree): `uncap_aspirate.py`'s `_apply_twin_effect`
  calls `wm.reparent(...)` from `_run_act`, driven by `attach=`/`to=` ids on the `CHOREOGRAPHY` grip/release acts — the first
  `reparent` calls in production `backend/app/`. The ids are entity-consistent (`tube_1`/`tube_1_cap` seeded by `pipeline.py:125`;
  `left_tool`/`right_tool`/`dropzone` in `definitions.py`), so `grasp_secure` (tube parented to a `TOOL`) and `cap_removed`'s
  reparent clause **can now turn true from a real grasp** once this is committed and the twin is seeded. For Track C's verification
  half this is a real unblock. The only remaining action is mechanical: **commit the reparent WIP with the pose work** (it is
  uncommitted, so a clean checkout still lacks it) — or, as a fallback, scope the demo's verify moment onto the geometry-only
  predicate the committed world frame + moving twin already make honest (Q-TWIN-COUPLING, Q-KIN-1; team `docs/INTEGRATION_PLAN.md`).
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
- **Allocation drift — re-emerged this cycle (back near the top, with Q-OT-1).** Last cycle the four-review laptop-over-room
  pattern looked broken. This cycle it returned in a softer form: the genuinely-new work is the ratchet-unscrew + path-teaching
  (Q-CAPOPS-1) — real, on-theme *dexterity* code, but **laptop/bench-buildable and uncommitted** — while the two room-only
  deciding items stood still: the OT serial merge (Q-OT-1, 6th cycle, still unmerged, pipette still can't draw) and
  pose-teaching (Q-POSES-1, no new poses this run). The pattern is now sharply legible: the team keeps producing the
  buildable-anywhere half and defers the merge-and-teach-at-the-bench half. The next block's job: **merge the OT serial driver
  and finish teaching the remaining poses at the bench**, then commit the WIP batch and measure the 210/211 board spacing
  (one caliper reading) so the committed world frame is metrically real. Treat further manipulation code as displacement until
  the room work moves (Q-OT-1, Q-POSES-1, Q-CAPOPS-1, Q-COMMIT-2, Q-CALIB-1).
