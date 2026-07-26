# Gap analysis — current system vs. reduced target scope

Date: 2026-07-26 · Branch `agent-loop-p0` · Baseline commit `a275723` (backup tag
`backup/pre-scope-reduction-20260726`) · 216 tests passing.

This document states, for each element of the target scope, **what exists today**,
**where**, and **what is missing**. It also lists what should be *removed*, since the
brief is explicitly a scope *reduction*.

---

## 0. Size of the current system

| Area | LOC | Notes |
|---|---|---|
| `backend/` | 6 597 | incl. 2 000 LOC of tests |
| `core/` | 3 071 | worldmodel, calibration, perception, motion, verification, viz |
| `drivers/` | 2 396 | xarm (678), camera×4 (~800), opentrons (75, stub), mock (327) |
| `frontend/src/` | 3 192 | 4 tabs, 15 components, no router/store |
| `scripts/` | 2 487 | bench tools; `validate_camera.py` alone is 992 |
| `docs/` | 2 537 | 22 markdown files, heavily overlapping |
| **total (excl. `third_party/`)** | **~17 600** | |

The target scope is materially *smaller* than this: it drops the digital twin, the
calibration pipeline and the twin-based verification layer, and replaces three
overlapping executors with one engine.

---

## 1. Startup / initialization

### 1.1 One command to start the backend
**Partial.** `just dev` → `uv run uvicorn backend.app.main:app --reload`
([justfile:9](../../justfile#L9)). The lifespan hook
([backend/app/main.py:28-42](../../backend/app/main.py#L28-L42)) loads the fleet, backgrounds a
per-camera startup snapshot, and starts the kinematics + twin-fusion threads.

**Missing:** the lifespan does **not connect any device** and does **not run an
initialization procedure**. `connect_all()` is only reachable via a manual
`POST /api/instruments/connect` (a button in the UI header, [App.vue:33](../../frontend/src/App.vue#L33)).
There is no discovery step, no per-device init routine, no home move, no readiness gate.

### 1.2 One command to start the frontend
**Exists.** `just frontend` → `cd frontend && npm install && npm run dev`
([justfile:13](../../justfile#L13)). Vite proxies `/api` and `/ws` to `127.0.0.1:8000`
([frontend/vite.config.ts:15-21](../../frontend/vite.config.ts#L15-L21)).

### 1.3 Two robot controllers + liquid handler controller
**Exists for the arms, stub for the liquid handler.**
- Fleet is config-driven: two `xarm` entries (`left` @ .11, `right` @ .13), one `opentrons`
  entry, four camera slots ([core/config.py:45-67](../../core/config.py#L45-L67)).
- `DeviceManager` owns one driver per entry ([device_manager.py](../../backend/app/services/device_manager.py)).
- `XArmDriver` is real and hardened: 678 LOC, joint soft-limit enforcement, cartesian
  pre-flight via IK, free-drive (mode 2), gripper auto-detect, braking on disconnect.
- **`OpentronsDriver` is a stub.** `connect()` assigns `self._conn = object()` and `_send()`
  returns `None` ([drivers/opentrons/driver.py:33-55](../../drivers/opentrons/driver.py#L33-L55)).
  Every liquid-handling call is a no-op that reports success. Real serial work lives on
  an unmerged branch (`feat/ot-one-serial-driver`).

### 1.4 Four camera controllers as subprocesses that quit cleanly
**Does not exist — architecture mismatch.** Cameras run as **in-process daemon threads**:
one `CameraWorker(threading.Thread)` per camera inside `CameraHub`
([camera_hub.py:139](../../backend/app/services/camera_hub.py#L139), [:434](../../backend/app/services/camera_hub.py#L434)).
Teardown is `camera_hub.stop_all()` with a 3 s join ([camera_hub.py:480-487](../../backend/app/services/camera_hub.py#L480-L487)).

The code documents exactly why subprocesses are the right call: opening a UVC device on
macOS can block in an **uninterruptible kernel wait** that `kill -9` cannot reap
([startup_snapshot.py:12-15](../../backend/app/services/startup_snapshot.py#L12-L15)), and repeatedly
opening/closing the same device in one process degrades it
([camera_hub.py:28-33](../../backend/app/services/camera_hub.py#L28-L33)). A thread cannot be killed;
a subprocess can. **Gap: process supervision, IPC frame transport, clean shutdown, restart.**

There is a useful precedent: `RemoteCameraDriver` ([drivers/camera/remote.py](../../drivers/camera/remote.py), 384 LOC)
already consumes another backend's MJPEG endpoint, and `HZ_CAMERA_HOST` rewrites every
camera slot to `remote` ([core/config.py:189-222](../../core/config.py#L189-L222)). A camera *subprocess*
serving the same contract is a small step from this.

### 1.5 Initialization procedure (find devices, connect, init routines)
**Missing entirely.** Nothing today:
- enables arm axes as part of boot (`enable()` exists on the driver, [xarm/driver.py:304](../../drivers/xarm/driver.py#L304), but is only called from the teach UI);
- moves the liquid-handler axes a bit in all directions and retracts Z (no such method exists on the LH capability at all);
- takes an initialization picture **per camera** — the closest thing is `startup_snapshot.run()`, which does write one frame per slot to `temp/captures/<slot>/` with 5 settle frames ([startup_snapshot.py:40-79](../../backend/app/services/startup_snapshot.py#L40-L79)). This is reusable.

The closest existing "init sequence" is `CalibrationPipeline.run()`
([core/calibration/pipeline.py:52-133](../../core/calibration/pipeline.py#L52-L133)) — 9 steps, of which
**5 are `TODO` stubs**. It is calibration, not initialization, and is out of the target scope.

### 1.6 Slow move to home; warn if no home position defined
**Missing.** `ArmDriver.home()` maps to xArm `move_gohome(wait=True)`
([xarm/driver.py:327-329](../../drivers/xarm/driver.py#L327-L329)) — the controller's zero pose, at
default speed, with no notion of a *taught* home. There is no `HOME` waypoint per arm, no
"is home defined?" check, no warning path, and no speed override on the home move.

Taught poses today ([data/teach_poses.json](../../data/teach_poses.json)): `right` has 6
(`cap_grasp_approach`, `cap_grasp`, `tube_grasp_approach`, `tube_grasp`, `transport_safe`,
`present_approach`), `left` has 2 (`tube_hold_approach`, `tube_hold`). **No home pose for either arm.**

---

## 2. Execution engine

### 2.1 Current state: three overlapping executors, none sufficient

| Executor | Where | Model | Actions supported |
|---|---|---|---|
| Hero workflow | [uncap_aspirate.py](../../backend/app/workflows/uncap_aspirate.py) | hardcoded `PLAN` (4 steps) → `CHOREOGRAPHY` dict of `Act(device, kind, pose…)` | `move`, `grip`, `release`, `aspirate` |
| Sequences | [core/sequences.py](../../core/sequences.py) | user-editable `Sequence(steps=[Step])`, JSON-persisted | `move`, `grip`, `ungrip`, `unscrew` — **arms only** |
| Agent engine | [backend/app/agent/](../../backend/app/agent/) | observe→decide→execute→verify→retry over the 4 hero *skills* | delegates to `uncap_aspirate._execute` |

All three are generators yielding `{step/index, phase, …}` dicts streamed over a websocket.
The event shapes are *similar but not identical* (`step` vs `index`, `phase` vowels differ),
which is why the frontend has two unrelated renderers.

**None of them supports:** per-action IDs, pause/resume, injection, offsets, camera actions,
liquid-handler actions beyond `aspirate`, or computational actions.

### 2.2 Required actions — status per action

**Lifecycle**
| Action | Status |
|---|---|
| initialize / reinitialize **all** devices | ✗ no init concept; `connect_all()` only ([device_manager.py:42](../../backend/app/services/device_manager.py#L42)) |
| initialize / reinitialize **individual** device | ✗ `POST /api/instruments/{id}/connect` connects only |

**Per robot**
| Action | Status |
|---|---|
| move to home | ~ `arm.home()` = controller zero, not a taught home, no speed tier |
| move to waypoint **with offsets** (default 0) | ~ waypoint replay exists (`teach_poses.get` + `move_joints`, [uncap_aspirate.py:202-217](../../backend/app/workflows/uncap_aspirate.py#L202-L217)); **offsets do not exist** |
| move_relative (offsets) | ✓ driver-level `move_relative` ([arm.py:147](../../drivers/capabilities/arm.py#L147)); exposed as `POST /api/arms/{id}/jog`; **not an engine action** |
| gripper close / open | ✓ `grip()` / `release()` ([arm.py:246,254](../../drivers/capabilities/arm.py#L246)) |
| **decap: 360° in 90° steps, rewinding between** | ✗ mismatch. `cap_ops.unscrew_cap` does **180°** bites with net-zero wrist travel ([core/motion/cap_ops.py:69-123](../../core/motion/cap_ops.py#L69-L123)). The 90°-step variant is a different plan (4 bites/turn) — the `plan_unscrew` function generalizes cleanly, but the constant and tests assume 180 |
| traverse to (set of named waypoints) | ~ closest is dense joint-path replay (`teach_paths` + `POST /api/arms/{id}/paths/{name}/replay`, blended). A "traverse a list of *named waypoints*" action does not exist |
| re-connect, re-enable, re-engage | ~ `connect()`, `enable()`, `clear_errors()` exist as *endpoints*; not as engine actions; "re-engage" is undefined |
| start / stop manual move | ✓ `set_free_drive(on)` ([xarm/driver.py:372](../../drivers/xarm/driver.py#L372)), `POST /api/arms/{id}/free_drive`; not an engine action |

**Liquid handler**
| Action | Status |
|---|---|
| move by x, y, z | ✗ `LiquidHandlerDriver` has `move_to(DeckLocation)` only ([liquid_handler.py:39](../../drivers/capabilities/liquid_handler.py#L39)) — **no relative move**, and the driver behind it is a stub |
| initialize (wiggle all axes, retract Z) | ✗ does not exist at any layer |

**Cameras**
| Action | Status |
|---|---|
| take snapshot | ~ `save_frame()` ([core/perception/capture.py:56](../../core/perception/capture.py#L56)) + `GET /api/cameras/{id}/snapshot`; not an engine action |
| search code continuously (find an AprilTag/ArUco ID) | ~ detection runs continuously at ~5 Hz in the hub and is published on `/ws/state`; there is **no "wait until marker N appears" action** |

**Interactive**
| Action | Status |
|---|---|
| pause / resume between any command | ✗ only a *checkpoint gate* on two named skills ([engine.py:30,100-107](../../backend/app/agent/engine.py#L30)); blocks the engine thread on a `threading.Event` released by a websocket message ([api/agent.py:27-38](../../backend/app/api/agent.py#L27-L38)). No arbitrary pause, no resume button, no UI |
| add input to plan / inject next step | ✗ does not exist |

**Computational**
| Action | Status |
|---|---|
| identify tip (right-arm cam + LH handover cam) | ✗ nothing. No tip detector, no tip geometry. `EntityKind.TIP` exists in the twin as a static 96-site grid seeded all-present ([core/worldmodel/definitions.py:67-74](../../core/worldmodel/definitions.py#L67-L74)) |
| identify tube, incl. via ArUco/AprilTag | ~ `ShapeDetector` buckets Hough circles by measured diameter → `"tube"` if ≥19 mm ([core/perception/shapes.py:28](../../core/perception/shapes.py#L28)); needs depth **and** intrinsics. AprilTag detection is solid ([fiducials.py](../../core/perception/fiducials.py), bench-tuned). **No tube-top / rim / top-Z concept** |
| calculate x/y/z offset tip-bottom → tube-top, multi-angle, with confidence + deviation | ✗ nothing returns a *vector* offset. Existing scalars: `WorldModel.distance(a,b)` ([entities.py:142](../../core/worldmodel/entities.py#L142)), `TubeAlignedAgent` distance + linear confidence ([verification/agents.py:132-149](../../core/verification/agents.py#L132-L149)), `TwinFuser.jump_m`. All twin-based, all scalar, no deviation estimate |
| create image overlays of distances/offsets and store processed images | ✗ `FiducialDetector.annotate()` exists ([fiducials.py:171](../../core/perception/fiducials.py#L171)) and has **zero call sites**. Overlays reach the UI as normalized JSON polygons drawn in SVG, never as rendered images. `cv2.imwrite` is only ever used on *raw* frames |

### 2.3 The vision-servo loop
The workflow's `Loop the following` block (snapshot → identify tip → identify tube →
offset+confidence per camera → overlay → pick higher-confidence → nudge the LH → repeat until
deviation < threshold) **does not exist in any form**. It needs: the four computational actions
above, a loop construct in the engine, a convergence/threshold policy, an iteration cap, and a
relative LH move that actually reaches hardware.

### 2.4 Workflow waypoint names
The target workflow names 15 waypoints (`RIGHT_ARM_APPROACH_RACK`,
`LEFT_ARM_APPROACH_CAP_GRAB`, `LEFT_ARM_TRANSITION_MID_TABLE`, …). **None of these exist**; the 8
taught poses use a different vocabulary (`cap_grasp`, `tube_hold`, …). Note also that the brief
lists the three transition waypoints and the two liquid-handler waypoints with a `LEFT_ARM_`
prefix while the acting device is the right arm — see open question **Q3**.

### 2.5 Speed tiers
The workflow specifies `fast` / `medium` / `slow` per step. Today speed is a raw
`float` threaded through `move_to(speed=)` / `move_joints(speed=)` with per-driver defaults
(`tcp_speed` 100 mm/s, `joint_speed` 20 °/s) and soft caps in `TEACH_LIMITS`
([core/config.py:23-29](../../core/config.py#L23-L29)). **No named tiers.**

---

## 3. Simulation mode

**Partial, and not a "mode".** What exists:
- Mock drivers registered as their own fleet types: `mock_arm`, `mock_camera`,
  `mock_tag_camera`, `mock_liquid_handler` ([drivers/mock/__init__.py:313-322](../../drivers/mock/__init__.py#L313-L322)).
- `fleet.mock.json` selects them wholesale via `HZ_FLEET_FILE`.
- `MockTagCameraDriver` renders **real tag36h11 markers** with slow drift and an optional flat
  depth plane + plausible intrinsics — genuinely exercises detect → polygon → overlay.
- `StillImageCameraDriver` replays a saved frame as a camera.

What is missing for the brief:
- **No single simulation flag.** Sim is selected by swapping the whole fleet file, so you
  cannot simulate cameras while driving real arms (or vice versa).
- **No replay-from-recorded-session camera.** There are **1 156 recorded frames** on disk across the
  four viewpoints (`temp/training/20260726T073940Z/` has 75 frames × 4 cameras at 1 fps, with a
  manifest), currently unused by any driver.

  **Correction (measured 2026-07-26, after the design review):** these `temp/recordings/` and
  `temp/training/` frames are *not* all usable colour imagery. But see the **superseding audit in
  §3.1 below** — the recorded *sessions* are the wrong place to judge feasibility, and the
  `temp/captures/` startup snapshots tell a materially more favourable story.

  | session / slot | n | resolution | stream | AprilTags found |
  |---|---|---|---|---|
  | `recordings/…064046Z/handover_cam` | 115 | 640×480 | colour | 188, 218 |
  | `recordings/…064046Z/overview_cam` | 115 | 640×480 | **greyscale IR** | none |
  | `recordings/…064308Z/gripper_cam` | 192 | 640×480 | colour | **none** |
  | `recordings/…064308Z/handover_cam` | 192 | 640×480 | colour | 188, 218 |
  | `recordings/…064308Z/overview_cam` | 192 | 640×480 | **greyscale IR** | none |
  | `training/…073940Z/gripper_cam` | 75 | 1280×720 | colour | **none** |
  | `training/…073940Z/gripper_left_cam` | 75 | 1280×720 | colour | 218 |
  | `training/…073940Z/handover_cam` | 75 | 1280×720 | **greyscale IR** | none |
  | `training/…073940Z/overview_cam` | 75 | 1280×720 | **greyscale IR** | none |

  Four of nine folders are RealSense **IR frames with the structured-light dot projector on** —
  every surface is covered in speckle, which defeats both AprilTag detection and Hough/ellipse
  fitting. `gripper_cam` — one of the two cameras the target workflow's servo loop names — detects
  **zero tags in 53 sampled frames** and, on visual inspection of
  `temp/captures/gripper_cam/latest_color.png`, is **aimed across the room** (ceiling, a wall, a
  seated person); the tube rack occupies only the bottom ~20 % of frame. `handover_cam` does look at
  the Opentrons deck and does see deck-rail tags — but only in its 640×480 colour session, not in
  its 1280×720 IR one. The same fleet id therefore maps to **different physical viewpoints at
  different resolutions across sessions**, which `startup_snapshot.py:3-8` documents the cause of
  ("on macOS those indices are reassigned whenever the rig changes").

  Also: the only tag ids ever detected are **188 and 218**, neither of which is in
  `MARKER_MAP` ([core/calibration/markers.py:30](../../core/calibration/markers.py#L30), which maps
  180–186 and 224). The marker map does not describe the physical bench.

  Consequences for the recorded sessions specifically: they are usable as replay fixtures for
  *stream plumbing*, but not as ground truth for tip/tube detection.

### 3.1 Superseding imagery audit — the `temp/captures/` startup snapshots

The judgement above was made on `temp/recordings/` and `temp/training/`, which are the wrong
evidence: they are two ad-hoc recording sessions, whereas `startup_snapshot` writes one frame per
slot **on every boot** and therefore covers the whole afternoon, including the window in which the
rig was actually set up for the handover. Re-measured across all 51 capture files:

**Finding 1 — both required viewpoints do see the handover, in colour, with a marker on the tube
assembly.** In the 07:11–07:19 window:

| slot | resolution | stream | tag36h11 ids detected |
|---|---|---|---|
| `handover_cam` | 1280×720 | colour | **225** (on the tube/gripper assembly, at frame centre), 189, 219, 180, 181, 183, 218 (deck) |
| `gripper_left_cam` | 1280×720 | colour | 218 (table surface) |
| `overview_cam` | 1280×720 | colour | 202, 203, 185, 184, 227, 191 |

`temp/captures/handover_cam/20260726T071143_135Z_color.png` shows the pipette tip descending onto a
tube held in the orange jaws, dead centre, with **tag 225 detected on the assembly**. This is exactly
the view the servo loop needs, and the detector already finds it with the tuned parameters — no new
code, no upscaling, no CLAHE.

**Finding 2 — the fleet ids do not identify the physical cameras.** The camera the operator identifies
as the **right arm's** gripper camera is the slot named **`gripper_left_cam`**. Its 07:18 frame
(`20260726T071850_224Z_color.png`) is an eye-in-hand view of the pipette descending into the
tube — the correct right-arm handover viewpoint. Meanwhile the slot named `gripper_cam` detects zero
tags in all 11 of its captures and its frames look across the room. This is the instability
`startup_snapshot.py:3-8` warns about, now observed: **slot names are not a usable identity for
per-camera calibration.** ([ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md) B1 reaches the same
conclusion from different evidence.)

**Finding 3 — the earlier "0 tags from the gripper camera" result was resolution, not aim.** The
640×480 frame the operator identified (`gripper_left_cam/20260726T065209_454Z_color.png`) visibly has
a marker on the tube, and the detector finds nothing. On a 2× upscale of the same frame it detects
**tag 218**. So the tube marker subtends too few pixels at 640×480 and is detectable at 1280×720.
**The servo cameras must run at ≥1280×720** — which is already a config setting
(`CAM_WIDTH_<ID>` / `CAM_HEIGHT_<ID>`, [core/config.py:225-234](../../core/config.py#L225-L234)),
not new work.

**Finding 4 — a tag wrapped on a cylindrical tube does not detect; a tag on a flat surface does.**
In the 1280×720 gripper frame the tube's own marker (~(672, 540), ~60 px, clearly legible to the eye)
is **not** detected, while the flat table marker at (1001, 649) is. The tag is warped by the tube's
curvature, so the square-quad fit fails. The handover camera's successful detection (225) is on the
flatter top of the assembly. **Consequence: put fiducials on flat surfaces — the gripper jaw or a flat
tab — never wrapped around the tube.**

**Finding 5 — the classical fallback looks viable from the gripper view.** In that same frame the
pipette tip is a bright, well-silhouetted vertical shaft against a dark background, and the tube
opening is a bright ellipse directly beneath it. This is a favourable image for tip-bottom and
tube-rim detection — far more so than the IR/speckle frames the first audit sampled.

**Finding 6 — `handover_cam` and `overview_cam` flip from colour to IR after ~07:23** and stay IR
through 07:42, including in `latest_color.png`. Colour is demonstrably achievable on both (proven
07:11–07:19), so this is a stream-selection/driver issue, not a physical limitation.

**Finding 7 — the marker map is stale, not wrong-in-kind.** Tags physically present and detected
across the captures: **180, 181, 183, 184, 185, 188, 189, 191, 202, 203, 218, 219, 225, 227.**
`MARKER_MAP` ([core/calibration/markers.py:30](../../core/calibration/markers.py#L30)) lists 180–186
and 224 — so the bench is *more* densely marked than configured. The map needs extending, and it is
being deleted anyway (§6); what survives is a tag-size registry.

**Net effect on the vision requirements:** materially de-risked. Both required viewpoints see the
handover in colour with a detectable fiducial on the tube assembly; the blockers are a **resolution
setting**, a **colour-vs-IR stream selection**, **flat-mounted fiducials**, and a **stable camera
identity** — all four of which are configuration and bench work, not perception research. See
[REQUIREMENTS.md](REQUIREMENTS.md) §15 P-1…P-3 and §16 Q1/Q2 as answered.
- **No synthetic convergence generator** — the brief needs fake images where the tip↔tube
  offset visibly shrinks per iteration until the threshold is met. Nothing generates this.
- **No `intrinsics` and no depth** in any recorded session (every manifest has
  `intrinsics: null`; zero depth files), so depth-dependent paths cannot be replayed as-is.
- Simulation is not exposed to the *action* layer: the brief says **all actions** expose a
  simulation mode, which is a property of the engine, not only of the driver.

---

## 4. Logging

**This is the largest single gap relative to the brief.**

- **Zero use of Python `logging`** anywhere in `backend/`, `core/`, `drivers/`, `scripts/`
  (verified: no `import logging`, no `getLogger`). All diagnostics are **30 bare `print()`
  calls** to stdout, prefixed by hand (`[camera_hub]`, `[config]`, `[device_manager]`, …).
- **No log file.** Nothing is persisted. The brief requires every action's inputs and outputs
  written to *one shared* log file.
- **No structured per-action record.** The workflow/sequence/agent event dicts are the closest
  thing, and they are transport-only — never persisted, and they carry `detail` strings rather
  than typed inputs/outputs.
- **No log API.** No endpoint, no websocket, no tail.
- **Frontend has no log viewer.** `CommandLog.vue` renders a *client-side* ring of teach
  commands issued from that browser tab ([useTeach.ts:87-97](../../frontend/src/composables/useTeach.ts#L87-L97));
  `WorkflowRunner.vue` renders the raw websocket event list. Neither shows server logs, and
  neither survives a refresh.

---

## 5. Frontend

### 5.1 What exists
Four tabs, no router, no store, Vue 3 + Tailwind, raw `fetch`
([App.vue](../../frontend/src/App.vue), [package.json](../../frontend/package.json)):

| Tab | Component | Substance |
|---|---|---|
| fleet | `InstrumentPanel` + `WorkflowRunner` | device cards with raw status JSON; one "Run uncap → aspirate" button + flat event list |
| teach | `TeachPanel` (+9 children) | **strong**: cartesian/joint jog, absolute move with preview + two-click arm, gripper (incl. width), cap tools, pose library, path record/replay/thin, required-pose checklist, sequence builder, command log, keyboard bindings |
| cameras | `CameraTab` / `CameraView` | MJPEG `<img>` + absolutely-positioned SVG detection overlay; overlay/label toggles; device discovery table; clickable detections |
| world | `WorldMapTab` | server-rendered SVG top-down map, 1 Hz poll |

### 5.2 What is missing
| Requirement | Status |
|---|---|
| Workflow tab | ✗ no such tab |
| Plan visualizer: current node + upcoming plan | ✗ `GET /api/workflow/plan` exists ([api/workflow.py:19](../../backend/app/api/workflow.py#L19)) and is **never called by the frontend** |
| Per-action index | ~ only in `SequenceBuilder.vue` (a hand-built sequence, not a plan) |
| Per-action state (planned / running / complete) | ✗ `WorkflowRunner` shows an append-only event log, not per-action state |
| Per-action outputs (images, offsets, tip/tube identified) | ✗ nothing renders action outputs |
| Actions "chained", expandable to show their logs | ✗ |
| Pause / resume / inject buttons | ✗ the only "Pause" buttons stop a video stream ([CameraView.vue:150](../../frontend/src/components/cameras/CameraView.vue#L150)) and a map poll |
| Inject → chat window for agent interaction | ✗ zero matches for `chat`/`agent` in `frontend/src`. Backend `/ws/agent` + `GET /api/agent/goal` have **no consumer** |
| Agent injects an action between indexes | ✗ |
| Logs available via frontend | ✗ (see §4) |
| **Liquid-handler teach tool** | ✗ **completely absent.** `liquid_handler` appears in `frontend/src` only as a string in a union type ([api/client.ts:6](../../frontend/src/api/client.ts#L6)). The teach tab is arm-only; every command path is `/api/arms/{id}/…`. An Opentrons shows up as a read-only status card |
| Camera feed per camera with AprilTag/ArUco overlays | ✓ present and good. Caveat: only the `tag36h11` **AprilTag** dictionary is detected ([fiducials.py](../../core/perception/fiducials.py)); ArUco dictionaries are not wired (the word appears once, in a comment). `identify_family()` exists as a diagnostic |

---

## 6. Scope to REMOVE

The brief is a reduction. These subsystems are **not referenced by the target scope** and
together account for roughly 40 % of non-test application code.

| Subsystem | Files / LOC | Why it goes | Removal risk |
|---|---|---|---|
| **Digital twin / world model** | `core/worldmodel/` (~470), `backend/app/services/twin.py`, `twin_fusion.py`, `kinematics.py`, `core/kinematics.py` | Target scope has no twin. Offsets come from images, not from a scene graph | Verification agents, projection overlays and the world map all depend on it — all three also go |
| **Calibration pipeline** | `core/calibration/` (~375) | Not in scope; **5 of 9 steps are TODO stubs**; needs a printed 210/211 board and an unmeasured `BOARD_SPACING_M` | Removes `/ws/calibrate` and the only publisher of the twin |
| **Twin-based verification agents** | `core/verification/` (~180) | Replaced by the offset/confidence computational actions, which are image-based | The agent engine and the hero workflow both call them |
| **World-map visualization** | `core/viz/scene.py` (124), `WorldMapTab.vue` (165), `frontend/public/worldmap.html` | Serves the twin only | none |
| **Twin→image projection** | `core/perception/projection.py` (107) | Needs twin + extrinsics | Removes `source:"projection"` overlays |
| **Perception→twin fusion** | `core/perception/fusion.py` (92) | Needs twin | none |
| **P0 agent engine (skills/policy)** | `backend/app/agent/` (~544) | Superseded by the new engine + the *inject* chat agent, which is a different job (edit a plan, not select a skill) | `/ws/agent`, `/api/agent/goal` — neither has a frontend consumer |
| **Sequences** | `core/sequences.py` (261), `api/sequences.py` (187), `SequenceBuilder.vue` (278), `api/sequences.ts` | Subsumed by the engine's plan + inject | uncommitted-as-of-yesterday feature; no data file on disk |
| **Hero workflow (as an executor)** | `backend/app/workflows/uncap_aspirate.py` (273) | Its `CHOREOGRAPHY` becomes plan *data* for the new engine; the executor half is replaced | `agent/tools.py` imports `_execute` and `_collect_evidence` |
| **Pick-and-place planner** | `core/motion/pick_place.py` (248), `core/motion/adapters.py` (51) | Unused by anything in scope; metres/radians anti-spill planner with no caller outside a stale import | none |
| **Path teaching (dense joint recording)** | `core/motion/path_teach.py` (158), `core/teach_paths.py` (87), `path_recorder.py`, `PathTeach.vue` (170) | "Traverse to a set of *named waypoints*" ≠ dense 10 Hz joint recording + RDP thinning | Keeps `data/teach_paths.json` (1 path). **See Q7 — worth keeping?** |
| **CAD meshes** | `core/worldmodel/meshes.py`, `assets/cad/` | twin-only | none |
| **Bench scripts** | `scripts/validate_camera.py` (992), `find_joint_limit.py` (391), `gripper_sequence.py` (339), `validate_motion.py` (204) | Diagnostics, not product. `record_footage.py` (205) should be **kept** — it produces simulation fixtures | none (they are standalone) |
| **Remote camera driver** | `drivers/camera/remote.py` (384) | Superseded *in purpose* by camera subprocesses — but its MJPEG-client code is directly reusable for the subprocess transport. **Refactor, don't delete** | `HZ_CAMERA_HOST` |
| **Docs** | 22 files, 2 537 lines | Describe the superseded architecture; consolidate to ~5 | none |

### What must be KEPT
- `drivers/base.py`, `drivers/capabilities/*`, `drivers/registry.py` — the driver contract is
  sound and is the one part of the architecture the brief preserves.
- `drivers/xarm/driver.py` — 678 LOC of hard-won bench knowledge (joint soft limits measured
  with a script, the J5 flange-camera clearance, braking on disconnect, gripper auto-detect).
- `drivers/camera/{realsense,still,driver}.py`, `core/perception/{capture,fiducials,shapes}.py`
  — bench-tuned AprilTag parameters with measured justifications; do not re-derive these.
- `core/motion/cap_ops.py` — generalize 180° → configurable step; keep the pre-flight.
- `core/teach_poses.py` + `data/teach_poses.json` — waypoints need renaming/extending, not replacing.
- `backend/app/api/teach.py` (768) + the arm teach UI — the brief keeps the arm teach tool.
- `drivers/mock/*` + `fleet.mock.json` — the seed of simulation mode.
- The 1 156 recorded images in `temp/` — simulation fixtures.

---

## 7. Summary: the eight real pieces of work

1. **Logging substrate** — `logging` + one JSONL action log + tail API + frontend viewer. Nothing exists.
2. **Unified execution engine** — one action model, one plan, pause/resume/inject, replacing three executors.
3. **Camera subprocess supervisor** — process-per-camera with a frame transport and clean shutdown.
4. **Initialization orchestrator** — discovery → connect → per-device init → slow home (warn if undefined).
5. **Liquid-handler capability + driver** — relative XYZ move, init wiggle, Z retract, and a *real* transport (currently a stub).
6. **Vision computational actions** — tip detection, tube-top detection, multi-view offset with confidence + deviation, annotated image output.
7. **Frontend workflow tab** — plan visualizer with per-action state/outputs/logs, pause/resume/inject, agent chat.
8. **Liquid-handler teach tool** — does not exist in any form.

Plus one deletion pass (§6) and one waypoint-renaming pass (§2.4).
