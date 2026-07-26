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
replace the plan — it realizes the P0 rung of `docs/AGENT_ORCHESTRATION.md`. Caveat: it can only
be as honest as the verifiers, which are still stubs (see Risks).

## Timeline

- [x] **H+2** — Direction locked, 2 xArms + OT + cameras reserved, repo cloned, roles set
- [x] **H+6** — Backend boots (FastAPI + REST + WS), UI lists the fleet, teach panel drives an arm. xArm driver connects + moves for real (`drivers/xarm/driver.py` on the real `XArmAPI`, `scripts/init_xarm.py`). Teach layer is now hardened + tested (`backend/tests/test_teach_api.py`, `TeachPanel.vue`/`MoveTo.vue`), and the **camera stream path is mounted** (`main.py:67` includes `cameras.router`; MJPEG `/api/cameras/{id}/stream`, detections on `/ws/state`) with a RealSense RGB-D driver on disk. This cycle the **xArm safety/teaching layer deepened** (on-disk WIP): manual **free-drive teaching mode** (`set_free_drive`, xArm mode 2), **joint soft-limit enforcement** (model-table backfill + config `joint_limit_overrides`) and a **cartesian pre-flight** (`check_pose_target`) that refuses moves whose IK lands outside the soft limits, plus an interactive `scripts/find_joint_limit.py` to *measure* the J5 wrist-vs-flange-camera clearance the controller can't model — genuine dexterity groundwork. *OT driver's one real action (a serial aspirate) is still unverified here — its `connect()`/`_send()` are `TODO`; real OT serial work lives on `origin/feat/ot-one-serial-driver`, not merged into this branch.*
- [ ] **H+12** — FLOOR demo: snap-cap uncap + place tube in OT nest + aspirate, end-to-end (guaranteed baseline). *Not yet real: `backend/app/workflows/uncap_aspirate.py::_execute` still has every driver call commented out (TODO), so nothing moves autonomously end-to-end.*
- [ ] **H+16** — Dale: dual-arm ratchet-unscrew stable · Lukas: arm↔OT calibration done · Di: cap-off + aspiration agents returning real verdicts. *Landed toward this: Di's fiducial perception is real (`core/perception/fiducials.py` — AprilTag tag36h11 + 6-DoF pose), the calibration pipeline now runs and publishes the twin (`/ws/calibrate`), the camera transport is now mounted (`cameras.router` + `camera_hub`, detections on `/ws/state`), and `MARKER_MAP` now carries real stock ids (180–224). Still open — unchanged this cycle: verification agents remain `ok=True` stubs (no real verdict yet); calibration hand-eye/world-frame/scan steps are TODO so twin poses are placeholder; marker→entity offsets unmeasured.*
- [ ] **H+20** — TARGET: dual-arm screw-cap uncap + arm-held aspiration + verify→retry loop end-to-end; stretch decision
- [ ] **H+22** — Freeze features, rehearse demo, record backup video
- [ ] **H+24** — Present

## Fallback ladder (build bottom-up)

1. **Floor:** snap-cap + OT-nest aspirate (no arm-held). Guaranteed to run.
2. **Target:** screw-cap dual-arm ratchet-unscrew + arm-held aspiration + verify loop.
3. **Stretch:** recap + return; camera visual-servo centering.

## Risks

- **Verification is entirely stubbed (top risk).** All four agents in `core/verification/agents.py`
  return `ok=True, confidence=0.0, detail="stub"`, so the verify→retry loop can never fail or
  retry — the "physical verification" half of Track C is currently theater. Make **one** verifier
  real (e.g. `cap_removed` from gripper-torque drop or a simple vision/marker cue) before polishing
  anything else.
- Dual-arm unscrew is the long pole → Dale starts first; snap-cap fallback ready.
- Arm↔OT alignment → wide-mouth tube / funnel lead-in for slack.
- Live chaining → the verify→retry loop is the safety net; prefer deliberate failure injection in the demo.
- The hero workflow `_execute` is still empty (commented TODOs); wire at least the floor path so the
  chain and the agent loop drive real hardware, not no-ops.
- Camera feed is now wired (was the top infra gap last cycle): `main.py` mounts `cameras.router`, so
  `/api/cameras/{id}/stream` (MJPEG) + `/detections` are reachable and per-camera detections ride
  `/ws/state`. The vision-verifier path is no longer blocked at the transport — what's missing is the
  verifier itself (see top risk), not the plumbing.
- Fiducial poses will still be wrong until measured: `core/calibration/markers.py::MARKER_MAP` now uses
  **real** printed stock ids (`tag36h11` 180–224) but keeps `identity()` marker→entity offsets
  (0.02 placeholder). Measure the marker→entity offsets before trusting twin poses for any verifier.
- **Prioritization risk (now three cycles).** Three straight reviews named exactly two blockers — real
  verifiers and a wired `_execute`. This cycle again shipped *around* them: deeper xArm safety/teaching
  (free-drive mode, joint soft-limit enforcement + `check_pose_target` pre-flight, `find_joint_limit.py`),
  more camera-hub and validation scripts — real dexterity progress, but **neither blocker moved**. The arm
  is now genuinely operable and self-collision-safe; the *scored* half of Track C (verification) is exactly
  as stubbed as it was three cycles ago. Everything needed to make one verifier real has sat on disk for
  three cycles. The next block must spend on the verdict, not more substrate — or the team must state
  explicitly that it is betting the demo on the dexterity ceiling (Q-PRIORITY-1).
