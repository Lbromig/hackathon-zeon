# Project plan — 24h

Goal: **Cooperative Uncap → Aspirate**, verified. Three owners, one per device, each
owning their driver + the matching capability/verification slice + their share of the
backend and UI.

## Objective — OT-One free-space motion, then agent-directed positioning

**The pipette can move in all directions, so that we can build a real-world
understanding with depth perception, and have an AI agent drive the pipette to
where the tube actually is.**

That is the target. It decomposes into four steps, and they are strictly ordered
because each one is meaningless without the one before it.

1. **Move every axis under software control.** X, Z and A jog from the UI today
   (`/api/liquid-handlers/{id}/jog`). **Y does not move** — see the blocker below.
2. **Establish a coordinate frame.** Perceive the deck and express positions in a
   frame shared with the world model.
3. **Depth perception.** Recover the tube's position in 3D, not just in the image
   plane, and put it into `core/worldmodel/` as a pose like any other entity.
4. **Agent-directed motion.** The agent asks for a pose; the driver executes it
   and a verification agent confirms arrival.

### The blocker, stated plainly

Steps 2 to 4 all assume the machine can be told *"go to this coordinate."* **It
cannot.** No endstop on any axis registers with the board — `M119` was polled for
18 s while the Z limit switch was pressed by hand and no bit ever changed, and the
firmware config holds no axis limit entries. `G28.2` therefore never terminates on
a limit; it drives a fixed search distance into the mechanical stop and zeroes the
counter there. So `Z=0` is not a physical datum and absolute coordinates reference
a position that was never established. Everything working today is *relative*
jogging, which needs no datum. Details in `docs/OT_ONE_HARDWARE.md`.

This means an AI agent cannot currently be handed a target position at all, no
matter how good the perception is: there is no frame to express it in. Vision
would recover where the tube is and the machine would still have no way to be told
where to go.

There are two ways through, and one has to be chosen:

- **Repair the reference.** Fix the endstop wiring or the firmware config so
  `G28.2` terminates on a switch. This makes homing a true datum, unblocks Y, and
  makes absolute positioning trustworthy. Cleanest, and it also removes the fact
  that every home currently drives into a hard stop.
- **Close the loop with vision instead.** Never trust machine coordinates; have
  the camera measure the pipette *and* the tube, and jog relatively until the
  error is small. Slower and needs the camera calibrated to the deck, but it
  works on the hardware exactly as it is, and it is closer to the verification
  approach the rest of Track C already uses.

### Status

| Step | State |
|------|-------|
| X / Z / A relative jog, from UI and REST | working, on hardware |
| Tip pickup | working — 53 mm engagement, measured |
| Y axis | **blocked** — drives looking for a switch that never reports, and grinds |
| Absolute positioning / any coordinate frame | **blocked** — no datum exists |
| Depth perception → tube pose | not started, blocked on a frame |
| Agent-directed move-to-tube | not started, blocked on the above |

One safety property to carry into the agent work: with no endstops and no current
sensing, **a crash is invisible to software.** A stalled stepper skips steps and
the call returns exactly as it would on a clean move. Motion duration proves a
move ran, never that the path was clear. An agent given authority to move this
machine has no feedback channel that would tell it that it hit something, so
vision has to supply that before it is left unattended.

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
be as honest as the verifiers — which, as of `c29a76c`, are real and fail closed (see Risks).

## Timeline

- [x] **H+2** — Direction locked, 2 xArms + OT + cameras reserved, repo cloned, roles set
- [x] **H+6** — Backend boots (FastAPI + REST + WS), UI lists the fleet, teach panel drives an arm. xArm driver connects + moves for real (`drivers/xarm/driver.py` on the real `XArmAPI`, `scripts/init_xarm.py`). Teach layer is now hardened + tested (`backend/tests/test_teach_api.py`, `TeachPanel.vue`/`MoveTo.vue`), and the **camera stream path is mounted** (`main.py` includes `cameras.router`; MJPEG `/api/cameras/{id}/stream`, detections on `/ws/state`) with a RealSense RGB-D driver on disk. *OT driver's one real action (a serial aspirate) is still unverified here — its `connect()`/`_send()` are `TODO`; real OT serial work lives on `origin/feat/ot-one-serial-driver`, not merged into this branch.*
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

- ~~**Verification is entirely stubbed (top risk).**~~ **Closed `c29a76c`.** All four agents are
  real and **fail closed** — absent evidence returns `ok=False` with a reason, where the stubs
  returned `ok=True, confidence=0.0`. `cap_removed` fuses a wrist-torque collapse with cap-marker
  travel. Note the originally-assumed signal did not exist: the Lite 6 has no F/T sensor and the
  SDK rejects `grip(force=…)`, so there is no gripper-force channel — `joints_torque` carries it.
- **Nothing moves yet (now the top risk).** `uncap_aspirate._execute` still has every driver call
  commented out, and the agent toolbox's `Skill.run` reuses it. Because the verifiers are now
  honest, the chain correctly **stops at step 1** rather than falsely reporting four green steps.
  Wiring the floor path through `core/motion/pick_place.py` is the one thing between here and an
  end-to-end run.
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
- **Prioritization risk (this cycle).** The last two reviews named exactly two blockers — real verifiers
  and a wired `_execute`. This cycle shipped infra around them (camera hub, teach hardening, docker,
  mock fleet, motion-validation scripts) but touched **neither**. Everything needed to make one verifier
  real has been on disk for a full cycle; the next block must spend on the verdict, not more substrate.
