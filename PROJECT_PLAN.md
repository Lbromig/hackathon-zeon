# Project plan — 24h

Goal: **Cooperative Uncap → Aspirate**, verified. Three owners, one per device, each
owning their driver + the matching capability/verification slice + their share of the
backend and UI.

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
hardware, not code: the OT transport is still a no-op (Q-OT-1) and it needs a taught real bench to move
(Q-POSES-1) — see Risks.

## Timeline

- [x] **H+2** — Direction locked, 2 xArms + OT + cameras reserved, repo cloned, roles set
- [x] **H+6** — Backend boots (FastAPI + REST + WS), UI lists the fleet, teach panel drives an arm. xArm driver connects + moves for real (`drivers/xarm/driver.py` on the real `XArmAPI`, `scripts/init_xarm.py`). Teach layer is now hardened + tested (`backend/tests/test_teach_api.py`, `TeachPanel.vue`/`MoveTo.vue`), and the **camera stream path is mounted** (`main.py:67` includes `cameras.router`; MJPEG `/api/cameras/{id}/stream`, detections on `/ws/state`) with a RealSense RGB-D driver now committed (`368efba`). The **xArm safety/teaching layer** landed in git (commit `3800c2c`): manual **free-drive teaching mode** (`set_free_drive`, xArm mode 2), **joint soft-limit enforcement** (model-table backfill + config `joint_limit_overrides`) and a **cartesian pre-flight** (`check_pose_target`) that refuses moves whose IK lands outside the soft limits, plus an interactive `scripts/find_joint_limit.py` to *measure* the J5 wrist-vs-flange-camera clearance the controller can't model. The **measured J5 override is now in config for both arms** (`FLANGE_CAM_J5_LIMITS = {"5": [-78.4, 95.0]}`, measured 2026-07-25 on one arm and reused for the other, since both carry a flange camera — the second arm is still unmeasured, see Q-JLIMIT-1). Genuine dexterity groundwork, and this cycle it is committed. *OT driver's one real action (a serial aspirate) is still unverified here — its `connect()`/`_send()` are `TODO`; real OT serial work lives on `origin/feat/ot-one-serial-driver`, not merged into this branch.*
- [~] **H+12** — FLOOR demo: snap-cap uncap + place tube in OT nest + aspirate, end-to-end (guaranteed baseline). *Code-complete **and committed** this cycle (`ae94f94`): `uncap_aspirate.py::_execute` is wired — a data-driven `CHOREOGRAPHY` (left clamps the tube; right pulls the cap straight up, parks it, takes the tube, presents it under the OT tip; OT aspirates) driven through the capability interfaces against **taught poses**, with a `preflight` that refuses to run on any untaught pose and a `run()` that executes→verifies→retries. Asserted by `backend/tests/test_workflow_execute.py`; a clean checkout now runs it. **Not yet demonstrable end-to-end** for two hardware reasons only: the OT `aspirate` transport is a no-op so the pipette never draws (Q-OT-1), and the 12 choreography poses must be taught on the real bench (Q-POSES-1).*
- [~] **H+16** — Dale: dual-arm ratchet-unscrew stable · Lukas: arm↔OT calibration done · Di: cap-off + aspiration agents returning real verdicts. *Both halves have now landed on disk. Verifiers (previous cycle): all four `core/verification/agents.py` agents return **real verdicts** — `cap_removed` (cap reparented off the tube + separation gate, torque-drop telemetry bonus), `grasp_secure` (tube reparented onto a tool + gripper-width band), `tube_aligned` (distance to the pipette nozzle < 15 mm), `aspiration_ok` (positive aspirated volume) — each with a dedicated `ok=False`/`ok=True` test (`backend/tests/test_verification.py`). Wiring (this cycle): `_execute` now drives the choreography for real, so the real verdicts finally **gate real motion** in `run()`. A **perception→twin fusion loop** feeds the verifiers: `core/perception/fusion.py::TwinFuser` composes marker detections with the camera's twin pose to correct entity positions, driven by `backend/app/services/twin_fusion.py` (started in `main.py` lifespan at ~10 Hz), with a RealSense RGB-D driver (`drivers/camera/realsense.py`) on disk; new `projection.py` (twin→image overlay) and `shapes.py` (Hough-circle detection of untagged labware) round out perception. **All of the above is now committed** (commits `2b0ed34`..`e641f56`; HEAD `e641f56`) — a clean checkout runs the real verifiers and wired motion, closing the multi-cycle uncommitted-WIP gap (Q-COMMIT-1). Still open (hardware/calibration, not code): OT transport no-op (Q-OT-1); calibration hand-eye/world-frame/scan steps TODO so world poses are placeholder (Q-CALIB-1); marker→entity offsets unmeasured; right-arm J5 override unmeasured (Q-JLIMIT-1); dual-arm ratchet-unscrew (TARGET rung) not encoded — the wired path is the snap-cap FLOOR.*
- [ ] **H+20** — TARGET: dual-arm screw-cap uncap + arm-held aspiration + verify→retry loop end-to-end; stretch decision
- [ ] **H+22** — Freeze features, rehearse demo, record backup video
- [ ] **H+24** — Present

## Fallback ladder (build bottom-up)

1. **Floor:** snap-cap + OT-nest aspirate (no arm-held). Guaranteed to run.
2. **Target:** screw-cap dual-arm ratchet-unscrew + arm-held aspiration + verify loop.
3. **Stretch:** recap + return; camera visual-servo centering.

## Risks

- **The Opentrons reveal is currently mimed (now the #1 risk — honesty on the narrative climax).** `_execute`
  calls `ot.aspirate(...)` for real, but the OT-One `connect()`/`_send()` are **still** `TODO` (`connect()` stores
  `object()`, `_send()` returns `None`), so the pipette never physically draws. `aspiration_ok` would then judge
  a twin/telemetry value with no real aspirate behind it. Merge/cherry-pick `origin/feat/ot-one-serial-driver`
  into `agent-loop-p0` or the demo's climax passes green while nothing moves (Q-OT-1).
- **The wired floor path needs a taught bench to move (co-#1 risk — demo-day, requires hardware).** `_execute`
  replays 12 named taught poses; the unit suite uses a fixture, but the real bench library (`data/teach_poses.json`,
  gitignored) is **absent on disk here** and may be empty on the demo machine — `preflight` will then correctly
  *refuse to run*. A green test suite is not a moving demo; the poses must be hand-taught on the two-arm + OT rig,
  a task that cannot be pre-staged from a laptop (Q-POSES-1).
- **The working demo is now committed (former #1 risk — RESOLVED this cycle).** The wired `_execute`, the four
  verifiers, `TwinFuser`, the `twin_fusion` loop, the RealSense driver, `projection`/`shapes`/`capture`,
  `teach_poses`, and all their tests are committed onto `agent-loop-p0` (commits `2b0ed34`..`e641f56`; HEAD
  `e641f56`). A clean checkout no longer runs theater. Do **not** reopen this by branching the demo off an older
  SHA (Q-COMMIT-1 ANSWERED).
- **Verifiers/fusion gate twin geometry until calibration is real (honesty risk).** The agents query a twin
  populated by calibration seeding + kinematics + camera fusion; hand-eye/world-frame calibration is still `TODO`
  (Q-CALIB-1), so fused world poses are camera-frame until calibrated. A green `tube_aligned` proves the twin is
  consistent, not that the arm physically placed the tube — decide the *minimum* real calibration for the demo.
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
