# Work breakdown for parallel implementation

Date 2026-07-26. Companion to [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) (phases and locked
decisions D1–D30) and [REQUIREMENTS.md](REQUIREMENTS.md) (`R-*` ids, incl. Amendment 1 §17).

**Offset solve is locked** (operator-confirmed 2026-07-26): **O1 primary** — direct 3D from tag poses,
no jacobian on the primary path; **O5+O6+O7+O8+O9 always on** — damped least squares, online gain
adaptation, conditioning refusal, per-axis `sigma_mm`, clamp + no-progress abort; **O3 degraded** —
axis-decoupled fallback; **O2+O4 regardless** — near-orthogonal views, 4-corner + multi-frame
averaging. See [VISION_OFFSET_OPTIONS.md](VISION_OFFSET_OPTIONS.md).

---

## 0. How this is sliced, and why

Parallel agents that edit the same file conflict. So every slice below has **exclusive ownership** of a
set of paths, and no two concurrent slices share one. Cross-slice communication happens only through
**contracts frozen in Wave 0** — types, protocol shapes and interfaces that nobody in Wave 1 may change.

Three rules for every slice:

1. **Own your files, touch nothing else.** If you need a change in someone else's file, it is a
   contract change: stop and report it rather than editing across the boundary.
2. **The contracts from Wave 0 are frozen.** Import them; do not modify them.
3. **Tests are part of the slice, not a follow-up.** A slice is done when its own tests pass *and* the
   full suite is still green (baseline 216).

```
Wave 0  (SERIAL — one agent, three ordered steps; biggest blast radius, run alone)
   W0-A deletion pass  →  W0-B foundations  →  W0-C contracts
        │
        ▼
Wave 1  (PARALLEL — 7 slices, file-disjoint)
   S1 engine core      S2 arm handlers + waypoints     S3 cameras
   S4 liquid handler   S5 vision                       S6 frontend shell
   S7 frontend cameras + LH teach
        │
        ▼
Wave 2  (PARALLEL — 5 slices, each depends on named Wave 1 slices)
   S8 engine API   S9 init orchestrator   S10 workflow plan data
   S11 inject      S12 integration + acceptance
```

---

## Wave 0 — serial, one agent

Run **alone**. W0-A deletes ~40 % of the application surface; doing it concurrently with anything else
guarantees conflicts.

### W0-A — Deletion pass
Delete, one subsystem per commit, full suite green after each.

| Delete | Notes |
|---|---|
| `core/worldmodel/`, `core/kinematics.py` | **Salvage first:** the `Transform` type → `core/perception/geometry.py` |
| `core/calibration/` | **Salvage first:** tag-size/id registry → `core/perception/markers.py`, minus every twin/entity field |
| `core/verification/`, `core/viz/`, `core/sequences.py`, `core/teach_paths.py` | |
| `core/perception/{fusion,projection}.py`, `core/motion/{pick_place,adapters,path_teach}.py` | |
| `backend/app/agent/`, `backend/app/workflows/`, `backend/app/services/{twin,twin_fusion,kinematics,path_recorder}.py` | |
| `backend/app/api/{workflow,sequences,calibration}.py` | |
| `frontend/src/components/{worldmap/,teach/PathTeach.vue,teach/SequenceBuilder.vue}`, `components/{InstrumentPanel,WorkflowRunner}.vue`, `api/{sequences,worldmodel}.ts`, `public/worldmap.html` | |
| `scripts/{validate_camera,validate_motion,find_joint_limit,gripper_sequence}.py` | **Salvage first:** the RealSense-SDK-serial listing logic from `validate_camera.py` → hand to S3 as a note. Keep `record_footage.py` |
| Tests: `test_{worldmodel,worldmodel_concurrency,extrinsics,scene,fusion,projection,verification,kinematics,sequences,path_teach,agent_engine,workflow_twin_effects,workflow_execute,integration_loop}.py` | |
| `/paths*` routes in `backend/app/api/teach.py` | Keep everything else in that file |

Exit: suite green; `grep` gate proves no importer remains for any deleted module; net LOC down.

### W0-B — Foundations
| New/changed | Contents |
|---|---|
| `core/obs/log.py` | stdlib `logging`, JSONL formatter, single file, context bound **on the handler** (D23). Convert all 30 `print()` sites |
| `core/obs/tail.py` | backwards tail + cursor read |
| `backend/app/api/logs.py` | `GET /api/logs` with cursor + filters. **No websocket** (S10 cut) |
| `core/speeds.py` | `fast`/`medium`/`slow` → per-device linear/angular, clamped by existing soft limits |
| `core/config.py` | sim config (**simulation is the DEFAULT**, D29), log/artifact paths, per-camera identity + resolution schema, `servo_cameras` list |
| `justfile` | `backend` (simulated), `real`, `frontend`, `reap`, `test` |

### W0-C — Contracts (types only, no behaviour)
| File | Contents |
|---|---|
| `backend/app/engine/actions.py` | `Action` discriminated union (~14 kinds, S4 cut), typed per-kind output models, `ActionResult` incl. `simulated` (D25), handler-registry decorator |
| `backend/app/engine/events.py` | Event models **with a monotonic sequence number** (D24) |
| `backend/app/engine/blackboard.py` | Exactly five named slots: `frame`, `tip`, `tube`, `offset`, `selected_offset` (S2 cut). No generic resolver |

**Frozen after W0-C.** Wave 1 imports these and does not change them.

---

## Wave 1 — 7 parallel slices

### S1 — Engine core
**Owns:** `backend/app/engine/{plan,runner}.py`; `backend/tests/test_{plan,runner_pause,runner_inject,loop_expansion}.py`
**Requirements:** R-ENG-1/4/5/8/9/10/12/13/15/16/17/18 · D1 D4 D8 D9 D14 D22 D24
- Plan: stable identity + derived index; mutation; **bounded** loop materialization (D14) — hard cap on materialized actions, no nested loops.
- Runner: one worker thread (D1); cooperative pause gate at action boundaries **and** inside decap steps and loop iterations; resume; abort as the separate hard path.
- Injection by `after_<identity>`, **must be able to land at the cursor** (D22); refuse with a reason rather than relocating; never 409 merely because a long move is in flight.
- Action wrapper: timing, retry, artifact recording, event emission, logging — so a handler **cannot forget to log** (R-LOG-5); decides `simulated` from resolved config (D25).
- Full-state snapshot for reconnect (D24).

**Tests:** pause during a multi-second mock move stops at the next boundary and reports `pausing`; inject at cursor / after a completed action / after the last; inject into the past refused; a never-converging loop cannot grow the plan without bound; reconnect reconstructs full state.

### S2 — Arm handlers, per-arm waypoints, 90° decap
**Owns:** `backend/app/engine/handlers/arm.py`; `core/teach_poses.py`; `core/motion/cap_ops.py`; `backend/app/api/teach.py` (waypoint endpoints only); `backend/tests/test_{arm_handlers,waypoint_ownership,cap_ops,teach_api}.py`
**Requirements:** R-ARM-1…9 · **R-WP-1…8** · R-ENG-11/14 · D12 D21
- **Per-arm waypoint ownership (R-WP-1/2):** a waypoint belongs to exactly one device; a move naming a waypoint the acting device does not own is **refused at pre-flight**, message naming both owner and actor. No fallback to a same-named waypoint on another arm, no resolution by search order.
- `HOME` per arm (R-WP-4); pre-flight reports `(device, name)` pairs (R-WP-5).
- **Persistence regression test (R-WP-8):** save → reload storage → read back; assert the atomic temp+replace path. The write path already exists at `api/teach.py:758` — do not rewrite it, test it.
- Handlers: home, waypoint(+offsets), relative, grip/release, **decap = 360° in 90° steps with rewind between** (generalize `HALF_TURN_DEG`; keep net-zero wrist travel and the up-front joint pre-flight), traverse, reconnect/re-enable/re-engage (= `clear_errors` → `enable(True)` → verify with a zero-distance move).
- Device claims **must exempt** `/api/arms/{id}/stop` (D21) — it deliberately bypasses the teach lock today and carries a "do not fix that" comment.
- **Do not touch** `drivers/xarm/driver.py` (D-keep).

### S3 — Camera subprocesses, identity, resolution, colour/IR
**Owns:** `drivers/camera_proc/`; `backend/app/services/camera_supervisor.py`; `drivers/camera/{remote,replay}.py`; `backend/app/api/cameras.py`; `backend/app/engine/handlers/camera.py`; delete `backend/app/services/{camera_hub,startup_snapshot}.py`; `backend/tests/test_{camera_supervisor,camera_proc_contract,camera_identity,camera_resolution}.py`
**Requirements:** R-START-4…8 · R-CAM-1…5 · **R-CAM-6…17** · D13 D20 D26
- Process per camera; detection **in the child**, next to the pixels; serves stream/snapshot/detections/health/shutdown.
- Supervision: bounded restart, mark-unavailable, SIGTERM→SIGKILL escalation **with reporting** (D26 — restart is *not* a recovery for a child stuck in an uninterruptible device open). `just reap`, no pid registry (S9 cut).
- **Freshness (D20):** `min_seq` is unimplementable — no seq crosses the MJPEG wire. Use an explicit request/response snapshot returning the **child-side capture timestamp**, and **drain the capture buffer** (`capture()` is a bare `read()` that can return a queued older frame). The engine fails a servo iteration if the timestamp predates the last LH move.
- **Identity (R-CAM-6/7):** content fingerprint at boot — probe each index, capture, classify by channel saturation (colour vs IR), resolution, and similarity to the stored per-slot reference. Prefer the **RealSense SDK serial** when a slot runs the `realsense` driver. *Note: the USB-descriptor serial differs from the SDK serial, and device names cannot identify a cv2 index — both dead ends are documented.* Unresolvable slot ⇒ **unavailable with a reason**, never a silent substitution.
- **Resolution (R-CAM-10…13):** per-camera selection from `640×480 / 848×480 / 1280×720 (default) / 1920×1080`, all @30. Restart only that camera. **Read back the achieved mode** — OpenCV silently substitutes the nearest; RealSense rejects outright. Label 640×480 as below the servo minimum. Changing resolution invalidates resolution-bound calibration.
- **Colour/IR (R-CAM-14…16):** colour is the default; detect and warn when a slot delivers greyscale; slot resolution **prefers the colour node** of a multi-node unit. The per-camera toggle is R-CAM-17, explicitly a later phase.
- ArUco dictionary support alongside `tag36h11`. **Do not touch the tuned detector parameters** (R-NFR-3).
- Replay driver for recorded sessions; restrict which are usable (4 of 9 folders are IR).

### S4 — Liquid handler
**Owns:** `drivers/capabilities/liquid_handler.py`; `drivers/opentrons/`; `drivers/mock/__init__.py`; `core/sim/world.py`; `backend/app/engine/handlers/liquid_handler.py`; `backend/app/api/teach_lh.py`; `backend/tests/test_{lh_driver,lh_transport,sim_world}.py`
**Requirements:** R-LH-1…5 · R-SIM-5 · D3 D6 D28
- **Additive only (D28):** add relative XYZ, `initialize` (wiggle both directions per axis, then retract Z), `retract_z`, envelope limits. **Do not touch** `aspirate`/`dispense`/`pick_up_tip`/`drop_tip` — Q7.
- Transport protocol + **loopback transport asserting exact emitted wire lines** + null transport (D6). The current `connect()` assigns a dummy and `_send()` returns `None`, so every call reports success while doing nothing — that is the bug class these assertions catch.
- Relative moves as **read → clamp → absolute command** where a readback exists; relative *semantics* at the interface.
- Report position provenance (measured vs dead-reckoned); warn on drift.
- **`core/sim/world.py` (D3):** one shared tip↔tube offset that the mock LH's relative move **decrements** (with gain error + noise). This is what makes the servo loop converge *because the moves close it*.
- Refuse, never silently clamp, an out-of-envelope request.

### S5 — Vision
**Owns:** `core/perception/{tip,tube,offset,overlay,markers,geometry}.py`; `backend/app/engine/handlers/vision.py`; `drivers/camera/servo_sim.py`; `backend/tests/test_{tip_detect,tube_detect,offset_solve,overlay}.py`; `backend/tests/fixtures/vision/`
**Requirements:** R-VIS-1…5 · **R-VIS-12…17** · D27 D30 · O1/O3/O4/O5/O6/O7/O8/O9
- **O1 primary:** tip-bottom and tube-top from **PnP tag poses** on two flat 40–50 mm tags → 3D offset by vector subtraction in one camera frame. **No jacobian on the primary path.** `fiducials.py` already returns `T_cam_marker` — use it. Configured tag→feature offsets.
- **O3 degraded:** one tag visible ⇒ axis-decoupled jacobian control (x,y from top-down; z from side-on) with **O5 damped least squares** and **O6 online gain adaptation** (measure the pixel change a commanded step actually produced).
- **Always on:** O4 4-corner + multi-frame averaging · O7 conditioning **refusal** (SVD of the restricted submatrix; σ_min floor and cond ≤ ~10) · O8 per-axis `sigma_mm` from `(JᵀWJ)⁻¹` as the reported confidence · O9 clamp + no-progress abort.
- Terminate on the **remaining offset** (Q4/D16). Report inter-view disagreement as **advisory only**. There is **no** per-view "deviation" field — it is structurally always zero.
- Intrinsics per camera, bound to identity + resolution (R-VIS-17, R-CAM-9).
- Overlay renderer + artifact writer; `annotate()` finally gets a caller.
- `servo_sim.py`: synthetic converging view reading `core/sim/world.py`.
- **Fixtures (D30):** the three committed real frames in `backend/tests/fixtures/vision/` are ground truth. Assert tag 225 is detected on the handover frame; assert the 640×480 frame is the negative case.

**Tests:** inverting a jacobian column's sign makes the loop **fail visibly**, not converge (R-VIS-16); an ill-conditioned geometry is **refused**; a mismatched camera identity/resolution is **refused**; a missing detection degrades `sigma_mm` rather than producing a confident wrong answer; the classical fallback engages on an occluded tag.

### S6 — Frontend shell + workflow tab
**Owns:** `frontend/src/{main.ts,router.ts,App.vue}`; `frontend/src/stores/`; `frontend/src/views/`; `frontend/src/components/workflow/`; `frontend/src/api/{engine,logs}.ts`
**Requirements:** R-UI-1…8 · R-UI-12/13 · R-SIM-8 · D10 D24 D29
- Router + singleton `reactive()` stores (no Pinia). Shell: header + nav + `<RouterView>`.
- Plan chain: indexed rows, state chips, expandable per-action detail with **the logs that action produced**. No separate current-action card (S13 cut).
- Action outputs: images, tip/tube results, offsets.
- Run controls: start, pause (**honest `pausing` state**), resume, abort, inject.
- Reconnect via the snapshot endpoint (D24).
- Logs view; readiness panel with warnings incl. "no home defined".
- **Persistent, unmissable simulated-vs-real indicator** (D29) — a correctness requirement, not chrome, because simulation is now the default.

### S7 — Frontend cameras + liquid-handler teach
**Owns:** `frontend/src/components/cameras/`; `frontend/src/components/teach/` (incl. new `LiquidHandlerTeach.vue`); `frontend/src/api/{cameras,teach}.ts`; `frontend/src/composables/useTeach.ts`
**Requirements:** R-UI-9/10/11 · R-CAM-8/10…16 · R-WP-3
- **Retarget** `CameraTab`/`CameraView` to the subprocess endpoints — **do not rewrite them**; MJPEG `<img>` + SVG overlay works.
- **Resolution selector per camera** (4 modes; 1280×720 default; 640×480 labelled below-servo-minimum) showing the **achieved** mode, plus identity, resolved index, and colour/IR state (R-CAM-8).
- **`LiquidHandlerTeach.vue`** — XYZ jog, retract Z, run init, save named waypoint. Does not exist today in any form.
- Arm teach tab: keep as-is minus path teaching. **Waypoint pickers must offer an arm only its own waypoints (R-WP-3)** — a picker that can express an invalid pairing is a defect.

---

## Wave 2 — 5 parallel slices

| Slice | Owns | Depends on | Requirements |
|---|---|---|---|
| **S8 engine API** | `backend/app/api/engine.py` | S1 | R-ENG-2, event ws + snapshot |
| **S9 init orchestrator** | `backend/app/engine/plans/startup.py`, `backend/app/main.py` | S1 S2 S3 S4 | R-INIT-1…8, R-START-1…3/7 · D5 |
| **S10 workflow plan data** | `backend/app/engine/plans/handover.py` | S1 S2 S4 S5 | R-VIS-6…11, the 20 steps + servo loop |
| **S11 inject** | `backend/app/api/agent.py`, `backend/app/llm/inject.py`, `frontend/src/components/workflow/InjectChat.vue` | S1 S6 | R-UI-7/14 · Q9: form first, chat after |
| **S12 integration + acceptance** | `backend/tests/test_acceptance.py`, `README.md` | all | §18 all ten criteria |

---

## Contract summary — what crosses slice boundaries

Frozen in W0-C; everything below is imported, never redefined:

| Contract | Producer | Consumers |
|---|---|---|
| `Action` union + per-kind outputs + `ActionResult` | W0-C | S1 S2 S3 S4 S5 S8 S10 S11 |
| Event models + sequence number | W0-C | S1 S6 S8 |
| Blackboard's five named slots | W0-C | S1 S5 S10 |
| Logger + context binding | W0-B | everyone |
| Speed tiers | W0-B | S2 S10 |
| Camera identity + resolution config schema | W0-B | S3 S5 S7 |
| Camera child HTTP surface | S3 | S5 S7 S9 |
| `core/sim/world.py` offset | S4 | S5 |
| Waypoint ownership check | S2 | S10 |

**If a slice needs a contract changed:** stop, report it, do not edit across the boundary. A contract
change invalidates other slices' assumptions and is the one thing that cannot be merged cleanly.
