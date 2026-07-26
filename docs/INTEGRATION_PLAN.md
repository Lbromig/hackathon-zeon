# Weaving it together — integration plan, review & gap analysis

Cameras are live and the arms are connected. This document answers two questions
(how the **agent** fits, how the **overlay** renders), records an independent **connection
review**, lists the verified **gaps**, and gives an ordered **plan to weave the pieces** into
a live, verified demo. Every claim below was checked against the code (file:line).

**One-line finding:** the *plumbing* is already coherent and shared — all three cameras and
both arms use device IDs that equal their twin entity IDs, and fusion, projection,
verification and the agent all read/write one global `WorldModel`. What's missing is the
**physics↔twin coupling**: cameras have no world pose, arm motion never updates the twin, and
manipulation never reparents entities. So the twin is a correct *topology* over *placeholder
geometry*. Close those seams and everything downstream lights up.

---

## 1. How the agent comes into the picture

The agent (`backend/app/agent/`, streamed on `/ws/agent`) is a **thin decision wrapper over
the same hardcoded workflow**, not a separate control path:

- `Engine.run()` (`engine.py:78`) is a deterministic **observe → decide → execute → verify →
  repeat** loop. It asks a **Policy** what to do next, executes it, verifies, and gates on
  human checkpoints.
- The **Policy** is the swappable "brain". `/ws/agent` wires the **`RuleBasedPolicy`**
  (`agent.py:63`), which just walks `uncap_aspirate.PLAN` in order. `ClaudePolicy` exists but
  is gated on `ANTHROPIC_API_KEY` and **not wired into the endpoint** (`policy.py:159`).
- **Execution is the real workflow.** Each skill's `run()` calls the *same*
  `uncap_aspirate._execute` (`tools.py:59`) that `/ws/workflow` uses — real `ArmDriver` /
  OT calls replaying taught poses. So the agent drives real motion; it makes no independent
  geometric decisions.
- **Verification** reuses the same `AGENTS` + `_collect_evidence` (`tools.py:126`).
- **Human-in-the-loop:** before `uncap` and `aspirate` (`engine.py:30`) the engine emits a
  `checkpoint` event and blocks until the operator sends any message back (`agent.py:88`).

Net: **agent ≈ workflow + pluggable brain + human gates.** For the demo it's the "autonomy"
surface; swapping `RuleBasedPolicy` → `default_policy()` is the one-line upgrade to LLM-driven
step selection once the twin is trustworthy.

## 2. How the overlay is rendered

Entirely client-side SVG over an MJPEG image — video and overlay are decoupled
(`CameraView.vue`):

- The feed is a plain `<img :src="streamUrl(...)">` pointed at
  `GET /api/cameras/{id}/stream` (MJPEG). No player, no WebRTC (`CameraView.vue:147`).
- An absolutely-positioned `<svg>` sits on top with `viewBox="0 0 W H"` in **frame pixels**
  and `preserveAspectRatio="none"` (`:173-178`), so **normalized [0,1] polygons scale into
  the frame** at any display size (`points()`, `:59-60`).
- **Data source:** the `cameras` block of **`/ws/state`** (opened in `useFleet.ts`), i.e. the
  per-camera `CameraSnapshot.detections`. Each detection carries a normalized `polygon`,
  `center`, `source`, `entity_id`, `marker_id`, and metric fields — the frontend
  `Detection` type matches `camera_hub.Detection.as_dict` 1:1 (no REST-vs-WS drift).
- **Colour = meaning** (`colour()`, `:62-65`): sky-blue = `projection` (twin outline),
  green = detected **and** mapped to a twin entity, amber = detected but **unmapped**. Labels
  show `entity_id · #marker · range` (depth preferred over PnP range, `:67-72`).
- **Click is UI-only today:** `pick(d)` (`:82`) emits `select` to a detail card — it does
  **not** drive the robot (that's the missing task #18 wiring).

So all three overlay sources (AprilTag / projection / classical-CV) arrive as one normalized
detection list and are drawn identically; the projection outlines are computed *inside the
camera worker threads* by reading the twin, which is why they ride along on the per-camera
detections rather than a separate twin stream.

---

## 3. Connection review (independent pass)

Tagged **[BLOCK]** (breaks the live demo), **[WARN]** (latent), **[NOTE]** (fine, know it).

- **[BLOCK] No shared world frame.** Camera extrinsics `T_world_cam` are never set:
  `overview_cam`/`handover_cam` sit at identity, `gripper_cam` rides an un-updated
  `right_tcp` (`definitions.py:45-47`). Fusion composes `identity @ camera_xyz`
  (`fusion.py:71`) and projection inverts identity (`projection.py:73`) → world coords are
  really camera-frame coords, **different per camera**, and overlays are geometrically wrong.
- **[BLOCK] Motion is decoupled from the twin.** The only production `set_world_pose` callers
  are `calibration/pipeline.py:92` (ot_base) and `fusion.py:82`. **Nothing writes arm FK**
  (`*_tcp`/`*_tool`/`gantry`/`nozzle` are `static=False` but never updated) and **grip/release
  never reparent** (`uncap_aspirate.py` issues `grip`/`release` but no `wm.reparent`). So
  `cap_removed` (needs cap reparented off tube, `agents.py:89`) and `grasp_secure` (needs tube
  parented to a TOOL, `agents.py:113`) **can never turn true on a live run** → the agent/
  workflow retries to `max_attempts` and stops. These pass only in unit tests that manually
  reparent (`test_integration_loop.py:82`).
- **[BLOCK] `WorldModel` is not thread-safe.** No lock in `core/worldmodel/entities.py`; the
  `_lock` in `twin_fusion.py:26` guards only the single fusion thread against itself. The
  10 Hz fusion writer runs concurrently with 3 camera projection-read threads + verify +
  `GET /api/worldmodel`. Transient mixed-transform reads today; a `dictionary changed size
  during iteration` crash the moment runtime `wm.add`/reparent (tasks #14/#18) lands.
- **[WARN] Fusion jump-gate keyed by entity only** (`fusion.py:43`). With identity extrinsics,
  three cameras write the same entity to three frames; first writer wins, others rejected as
  ">0.25 m jumps" — so silently only one camera contributes. Key it on `(cam, entity)`.
- **[WARN] OT is a no-op** → `aspiration_ok` fails: `connect` fakes `_conn=object()`
  (`opentrons/driver.py:37`), `aspirate` writes nothing (`:70`), `status` never reports
  `aspirated_volume_ul` (`:47`).
- **[NOTE] Frontend/backend contract matches** (REST and `/ws/state` share `as_dict`); camera
  bring-up ordering is correct (connect before worker construction); the `camera_xyz is None`
  guard in `fuse_once` (`twin_fusion.py:46`) is the one thing stopping projected overlays from
  being re-fused — worth a test so it isn't "cleaned up".
- **[NOTE] `/ws/state` omits the twin + verdicts** (`workflow.py:114-115` sends only
  `instruments` + `cameras`). Overlays work only because projection is computed in the worker.

**Top 3 risks:** (1) no shared world frame, (2) verification disconnected from motion,
(3) unlocked twin under concurrent access.

---

## 4. Verified gap list (what blocks a live demo)

| # | Gap | Evidence | Fix |
|---|-----|----------|-----|
| G1 | Twin has no lock | `entities.py` (none); `twin_fusion.py:26` guards only itself | Add `RLock` in `WorldModel`; use it in `world_pose/set_world_pose/reparent/snapshot`; share it with fusion/projection/verify |
| G2 | Camera `T_world_cam` never set | `definitions.py:45-47`; `pipeline.py:76,80` TODO | Implement `_world_frame` + per-camera extrinsics from a fixed tag board; `set_world_pose(cam,…)` |
| G3 | Arm FK never feeds twin | no FK `set_world_pose` caller | Small loop: read `arm` TCP (already in `status()`) → `set_world_pose("{arm}_tcp", …)`; OT gantry too |
| G4 | Manipulation never reparents | `uncap_aspirate.py` grip/release; `fusion.py:80` translate-only | On grip/release call `wm.reparent` (tube→tool, cap→dropzone); reuse `motion/adapters.py` on_attach/on_detach |
| G5 | Fusion gate ignores camera | `fusion.py:43,77` | Key `_seen` on `(cam_entity_id, entity_id)` or a fixed camera priority |
| G6 | OT no-op → aspiration fails | `opentrons/driver.py:37,47,70` | Implement `_send`, or have `aspirate` optimistically set nozzle `aspirated_volume_ul` in twin |
| G7 | `/ws/state` lacks twin + verdicts | `workflow.py:114-115` | Add `"twin": wm.snapshot()` + latest verdicts to the tick |
| G8 | No move-to-entity / click-to-pick | `CameraView.vue:82` UI-only; planner unused | Endpoint: `entity_id` → twin pose → `pick_place.plan` via `ArmDriver` Mover |
| G9 | `MARKER_MAP` placeholder offsets | `markers.py:31-38` identity offsets | Task #20: place tags, measure `T_marker_to_entity` |
| G10 | Claude policy unwired | `agent.py:63` | Swap to `default_policy()` (optional) |

---

## 5. The weave-it-together plan (ordered)

Do these in order; the first four are the critical chain that makes a **live, verified** run
possible. **[code]** = software, **[hw]** = bench. Owners: **Di** cameras/calibration/
perception, **Dale** arms/FK/reparent, **Lukas** OT/orchestrator/ws.

**W1 — Lock the twin. [code] · ~30 min · (G1)**
Add a re-entrant lock to `WorldModel` and take it in `world_pose/set_world_pose/reparent/
snapshot`; delete the now-redundant `_lock` in `twin_fusion.py` (use the twin's). Unblocks
every concurrent step below. Do first.

**W2 — One shared world frame. [code]+[hw] · task #14 · (G2, G9)**
Place a fixed AprilTag world board both fixed cameras can see. In calibration:
`_camera_intrinsics` = read RealSense factory K (free); `_world_frame`/`_arm_to_arm` =
`solvePnP` each fixed camera and both arm bases against the board → `set_world_pose`;
`_hand_eye` = `cv2.calibrateHandEye` for `gripper_cam → right_tcp`. **Done when** a tag seen
by both fixed cams localizes to the same world point within ~5 mm.

**W3 — Arm FK → twin. [code] · (G3)**
A ~10 Hz loop (or the `/ws/state` tick) reads each arm's live TCP and writes
`set_world_pose("{arm}_tcp", …)` (+ tool offset, + OT gantry/nozzle). Now the twin's moving
parts — and `gripper_cam` riding `right_tcp` — actually move. **Done when** jogging an arm
moves its twin TCP.

**W4 — Reparent on manipulation. [code] · (G4)**
In `_run_act`, on `grip`/`release` call `wm.reparent` (tube→holding tool; cap→gripper→
dropzone). Wire `motion/adapters.py` `on_attach`/`on_detach` to the twin. **Done when**
`grasp_secure` and `cap_removed` flip true during a real run instead of exhausting retries.

**W5 — Fix fusion multi-cam gate + OT. [code] · (G5, G6)**
Key the fuser on `(cam, entity)`; make OT `aspirate` set the nozzle's `aspirated_volume_ul`
(optimistic) so `aspiration_ok` passes, pending the real serial driver. **Done when** all
four verifiers can pass end-to-end.

**W6 — Surface twin + verdicts on `/ws/state`. [code] · (G7)**
Add `twin` snapshot + latest verdicts to the tick so the UI has one live state stream.

**W7 — Move-to-entity / click-to-pick. [code] · task #18 · (G8)**
`POST /api/robot/pick {entity_id}` → twin pose + CAD dims → `pick_place.plan` → arm. Wire
`CameraView.vue`'s existing `pick()` click to it. **Done when** clicking a highlighted tube
grasps it.

**W8 — Physical tags + measured offsets. [hw] · task #20 · (G9)**
Glue the tag36h11 stock, measure each `T_marker_to_entity`, fill `MARKER_MAP`.

**W9 — Rehearse the loop + failure injection. [run]**
Run `/ws/agent` (checkpoints on) through uncap→aspirate; nudge the tube after "present" →
`tube_aligned` fails → recover → passes. Record the backup video.

**Optional W10 — wire `default_policy()`** for LLM step selection once W1–W4 make the twin
trustworthy.

### Critical path
**W1 → W2 → W3 → W4** is the minimum to get a *verified* live run (everything else is
polish or hardware). W1 is 30 minutes and unblocks safe concurrent work on the rest.
