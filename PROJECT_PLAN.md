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
be as honest as its actions — the verifiers are now real (on-disk WIP), but `_execute` still drives
nothing, so the loop verifies twin geometry rather than a physical result (see Risks).

## Timeline

- [x] **H+2** — Direction locked, 2 xArms + OT + cameras reserved, repo cloned, roles set
- [x] **H+6** — Backend boots (FastAPI + REST + WS), UI lists the fleet, teach panel drives an arm. xArm driver connects + moves for real (`drivers/xarm/driver.py` on the real `XArmAPI`, `scripts/init_xarm.py`). Teach layer is now hardened + tested (`backend/tests/test_teach_api.py`, `TeachPanel.vue`/`MoveTo.vue`), and the **camera stream path is mounted** (`main.py:67` includes `cameras.router`; MJPEG `/api/cameras/{id}/stream`, detections on `/ws/state`) with a RealSense RGB-D driver on disk. The **xArm safety/teaching layer** landed in git this cycle (commit `3800c2c`): manual **free-drive teaching mode** (`set_free_drive`, xArm mode 2), **joint soft-limit enforcement** (model-table backfill + config `joint_limit_overrides`) and a **cartesian pre-flight** (`check_pose_target`) that refuses moves whose IK lands outside the soft limits, plus an interactive `scripts/find_joint_limit.py` to *measure* the J5 wrist-vs-flange-camera clearance the controller can't model. The **left arm's measured J5 override is now in config** (`LEFT_J5_LIMITS = {"5": [-78.4, 95.0]}`, measured 2026-07-25); the right arm is still `TODO` (different tooling → must be re-measured, not copied). Genuine dexterity groundwork, and this cycle it is committed. *OT driver's one real action (a serial aspirate) is still unverified here — its `connect()`/`_send()` are `TODO`; real OT serial work lives on `origin/feat/ot-one-serial-driver`, not merged into this branch.*
- [ ] **H+12** — FLOOR demo: snap-cap uncap + place tube in OT nest + aspirate, end-to-end (guaranteed baseline). *Not yet real: `backend/app/workflows/uncap_aspirate.py::_execute` still has every driver call commented out (TODO), so nothing moves autonomously end-to-end.*
- [~] **H+16** — Dale: dual-arm ratchet-unscrew stable · Lukas: arm↔OT calibration done · Di: cap-off + aspiration agents returning real verdicts. *Big move this cycle — the verifier half landed (on-disk WIP): all four `core/verification/agents.py` agents now return **real verdicts** — `cap_removed` (cap reparented off the tube + separation gate, torque-drop telemetry bonus), `grasp_secure` (tube reparented onto a tool + gripper-width band), `tube_aligned` (distance to the pipette nozzle < 15 mm), `aspiration_ok` (positive aspirated volume) — each with a dedicated test asserting `ok=False` and `ok=True` paths (`backend/tests/test_verification.py`). A new **perception→twin fusion loop** feeds them: `core/perception/fusion.py::TwinFuser` composes marker detections with the camera's twin pose to correct entity positions, driven by `backend/app/services/twin_fusion.py` (started in `main.py` lifespan at ~10 Hz), with an Intel RealSense RGB-D driver (`drivers/camera/realsense.py`) on disk. Di's fiducial perception was already real (`core/perception/fiducials.py`), the calibration pipeline publishes the twin (`/ws/calibrate`), the camera transport is mounted (`cameras.router` + `camera_hub`, detections on `/ws/state`), and `MARKER_MAP` carries real stock ids (180–224). **Caveat (critical):** the verifiers query a twin populated by calibration seeding + kinematics + camera fusion — they are not yet gating any physical action, because `_execute` still drives nothing (see H+12). And all of the above is **uncommitted WIP** — HEAD (`3800c2c`) still contains the old `ok=True` stubs. Still open: calibration hand-eye/world-frame/scan steps are TODO so world poses are placeholder; marker→entity offsets unmeasured; dual-arm ratchet-unscrew and arm↔OT calibration not yet landed.*
- [ ] **H+20** — TARGET: dual-arm screw-cap uncap + arm-held aspiration + verify→retry loop end-to-end; stretch decision
- [ ] **H+22** — Freeze features, rehearse demo, record backup video
- [ ] **H+24** — Present

## Fallback ladder (build bottom-up)

1. **Floor:** snap-cap + OT-nest aspirate (no arm-held). Guaranteed to run.
2. **Target:** screw-cap dual-arm ratchet-unscrew + arm-held aspiration + verify loop.
3. **Stretch:** recap + return; camera visual-servo centering.

## Risks

- **`_execute` is empty — nothing autonomous moves (new top risk).** `backend/app/workflows/uncap_aspirate.py::_execute`
  still has every driver call commented out (and the agent path via `Skill.run` reuses it), so both the
  hardcoded chain and the agent loop "pass" against empty actions. Now that the verifiers are real, this is
  the single blocker between the repo and a genuine verified end-to-end signal: the verdicts have nothing
  physical to gate. Wire the **floor** path (snap-cap → present → OT aspirate) against the now-soft-limit-safe
  `pick_place` primitives first.
- **Real verifiers + fusion are uncommitted WIP (release risk).** The four real agents, `TwinFuser`, the
  `twin_fusion` loop, and the RealSense driver all live only on disk — HEAD (`3800c2c`) still ships the old
  `ok=True` stubs. If the demo runs a checked-out clean tree it runs *theater*. Commit this WIP onto the demo
  branch before freeze (Q-COMMIT-1).
- **Verifiers gate the twin, not physical reality (honesty risk).** The agents query a twin populated by
  calibration seeding + kinematics + camera fusion. Until `_execute` drives hardware and OT reports real
  telemetry, a green verdict proves twin geometry is consistent, not that the physical action succeeded.
- Dual-arm unscrew is the long pole → Dale starts first; snap-cap fallback ready.
- Arm↔OT alignment → wide-mouth tube / funnel lead-in for slack.
- Live chaining → the verify→retry loop is now a real safety net (verdicts can fail); script a deliberate
  failure injection for the demo (Q-DEMO-1).
- OT-One `connect()`/`_send()` are still `TODO` (real serial work sits unmerged on `origin/feat/ot-one-serial-driver`),
  so no real aspirate has run and `aspiration_ok` can only fire off simulated telemetry so far.
- Camera feed is now wired (was the top infra gap last cycle): `main.py` mounts `cameras.router`, so
  `/api/cameras/{id}/stream` (MJPEG) + `/detections` are reachable and per-camera detections ride
  `/ws/state`. The vision-verifier path is no longer blocked at the transport — what's missing is the
  verifier itself (see top risk), not the plumbing.
- Fiducial poses will still be wrong until measured: `core/calibration/markers.py::MARKER_MAP` now uses
  **real** printed stock ids (`tag36h11` 180–224) but keeps `identity()` marker→entity offsets
  (0.02 placeholder). Measure the marker→entity offsets before trusting twin poses for any verifier.
- **Prioritization risk — the pattern broke this cycle (half-resolved).** Three straight reviews named two
  blockers (real verifiers, a wired `_execute`). This cycle the team finally spent on the verdict: **all four
  verifiers are now real** (twin-query + telemetry fusion, tested) and a perception→twin fusion loop feeds them.
  That closes half of Q-PRIORITY-1 — verification is no longer stubbed. What is *now* the pattern to watch:
  (a) the win is uncommitted WIP (Q-COMMIT-1), and (b) `_execute` is still empty, so the second named blocker
  is the last one standing. The next block's job is unambiguous: commit the verifiers, then wire the floor
  `_execute` so a real verdict gates real motion — no new substrate.
