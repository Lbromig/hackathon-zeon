# World-Model Perception & Verification — Requirements

**Scope:** the perception + world-model layer that keeps the digital twin
(`core/worldmodel/`) aligned with reality, verifies physical actions in the
background, and drives closed-loop recovery **only** when an action fails. This document
extends `docs/DIGITAL_TWIN.md` (the entity/transform-tree design) and feeds
`docs/ARCHITECTURE.md` (updated in the same change).

**Use context:** research / hackathon, **non-commercial**. This unlocks the strongest
model choices (FoundationPose, BundleSDF, Kaolin `non_commercial`) without license friction.

---

## 1. Operating principles (the two hard constraints)

These two principles override the older "verify after each step" phrasing in
`docs/DIGITAL_TWIN.md` and shape every requirement below.

- **P1 — Verification runs as a background process.** A perception daemon continuously
  keeps the *dynamic* entities (tube, cap, pipette nozzle, held tool) localized and
  continuously evaluates the verification predicates. It is always-on during a run, not
  gated on a step boundary. Its output is advisory state published to the twin and UI.
- **P2 — Control is closed-loop only on error, for recovery.** Nominal motion is
  open-loop / scripted (kinematics + taught poses). A closed perception→motion loop is
  engaged **only** when a verification predicate fails or its confidence drops below
  threshold; once recovery succeeds, control hands back to open-loop.

Everything else — calibration, detection, pose, the twin — exists to serve P1 and P2.

---

## 2. Definitions

| Term | Meaning |
|------|---------|
| **Twin** | The `WorldModel` scene graph: entities with poses relative to a parent, composed to world. |
| **Dynamic entities** | tube, cap, nozzle, held tool — pose changes at runtime; the perception targets. |
| **Predicate** | A boolean-with-confidence query over the twin, e.g. `cap_removed`, `tube_aligned`. |
| **Background verifier** | Async task that refreshes dynamic-entity poses and re-evaluates predicates at a fixed cadence. |
| **Recovery controller** | Closed-loop behavior engaged on predicate failure (re-localize / regrasp / re-align / retry). |
| **Render-compare** | Analysis-by-synthesis: render a known CAD entity at a hypothesized pose (Kaolin) and score it against the live frame. |

---

## 3. Functional requirements

### 3.1 Calibration & shared frame (FR-CAL)

| ID | Requirement | Verified by |
|----|-------------|-------------|
| FR-CAL-1 | Camera intrinsics shall be available for each camera. All three are RealSense, so **factory intrinsics** (`CameraDriver.intrinsics()`) are used directly; a ChArUco pass is only an optional refinement. | Factory intrinsics load on connect; reprojection error < 1 px on a check board. |
| FR-CAL-2 | The on-arm camera → TCP transform (hand-eye, eye-in-hand) shall be recovered with `cv2.calibrateHandEye` from 15–20 diverse poses. | Round-trip AX=XB residual reported; static marker localizes to < 3 mm across arm poses. |
| FR-CAL-3 | Both fixed cameras (`overview_cam`, `handover_cam`) shall be extrinsically calibrated into the world frame from fixed tags (`solvePnP`). | A world-anchored marker projects to < 3 px error in each fixed cam. |
| FR-CAL-4 | The fixed rig and the arm camera shall resolve to **one** shared world frame; metric scale is set by the 3D-printed ruler / known board. | Same physical point localized by both cameras agrees to < 5 mm. |
| FR-CAL-5 | Calibration artifacts shall persist under `calib/` (`intrinsics/`, `hand_eye.json`, `world_frame.json`, `extrinsics/`) and load without re-running. | Cold start loads cached calibration; no recalibration required. |

### 3.2 Detection & segmentation (FR-DET)

| ID | Requirement | Verified by |
|----|-------------|-------------|
| FR-DET-1 | The system shall detect and segment **tube**, **cap**, and **nozzle** in RGB(-D) frames from any calibrated camera. | Mask IoU ≥ 0.7 vs. hand-labeled frames on the demo set. |
| FR-DET-2 | Detection shall bootstrap tracking: the first-frame mask seeds the pose tracker (FR-POSE-1). | Tracker initializes from an auto-generated mask without manual clicks. |
| FR-DET-3 | Segmentation shall run in the background loop fast enough to re-seed on tracker loss without stalling the loop (see NFR-PERF). | Re-seed latency < 300 ms measured end-to-end. |
| FR-DET-4 | Prompts shall be text/class-driven (open-vocabulary) so new labware needs no retraining. | Adding "screw cap" as a class requires config only, no fine-tune. |

### 3.3 6-DoF pose estimation & tracking (FR-POSE)

| ID | Requirement | Verified by |
|----|-------------|-------------|
| FR-POSE-1 | Given a tube/cap CAD mesh + first-frame mask + depth, the system shall estimate initial 6-DoF pose and then **track** it frame-to-frame. | Tracked pose within 5 mm / 5° of a marker-based ground truth on the demo clip. |
| FR-POSE-2 | Tracking shall degrade gracefully: on low confidence it requests a re-detect (FR-DET-2) rather than emitting a bad pose. | Injected occlusion triggers re-init, not a silent wrong pose. |
| FR-POSE-3 | Pose confidence shall be published with every estimate and consumed by predicates and the recovery gate. | Confidence field present on every twin pose update. |
| FR-POSE-4 | For objects without a usable CAD mesh, a model-free fallback (BundleSDF-style) may be used at reduced rate for one-time localization. | Fallback documented; not on the real-time path. |

### 3.4 World-model state layer (FR-WM)

| ID | Requirement | Verified by |
|----|-------------|-------------|
| FR-WM-1 | Perception poses shall be fused into the existing `WorldModel` as corrective updates to entity poses; kinematics remain the fast always-on source for arm/gantry parents. | Twin pose = kinematics when no detection; snaps to perception when confident. |
| FR-WM-2 | Manipulation shall **reparent** entities (pick tube → parent = gripper; cap off → tube→gripper→dropzone) as already modeled in `core/worldmodel/entities.py`. | Reparent events logged; world pose continuous across reparent. |
| FR-WM-3 | Each dynamic entity shall keep a short **pose history** (ring buffer) with timestamps + source (kinematics/perception) + confidence. | History queryable; used for velocity/stability checks. |
| FR-WM-4 | Fusion shall reject stale or low-confidence perception (gating by covariance/confidence + max age) to avoid corrupting the twin. | Stale frame does not move the entity; logged as rejected. |
| FR-WM-5 | The twin shall be serializable to a snapshot (`entities.json`) and streamed over `/ws/state` for the UI. | Snapshot round-trips; UI reflects live poses. |

### 3.5 Background verification (FR-VER)

| ID | Requirement | Verified by |
|----|-------------|-------------|
| FR-VER-1 | A background verifier shall re-evaluate all active predicates at a fixed cadence (target 5–15 Hz), independent of workflow step boundaries (P1). | Verifier ticks logged at target rate during a run. |
| FR-VER-2 | Predicates shall be **twin queries** where possible: `cap_removed = cap.parent≠tube ∧ dist(cap,tube)>τ`; `tube_aligned = ‖tube.world − nozzle.target‖<τ`; `grasp_secure`, `aspiration_ok`. | Each predicate returns `{ok, confidence, detail}` (existing `VerificationResult`). |
| FR-VER-3 | Predicates may fuse **render-compare** (Kaolin) and driver telemetry (torque/gripper width/OT volume) with twin geometry for robustness. | `cap_removed` combines threads-visible render score + torque drop, not one signal. |
| FR-VER-4 | Each predicate shall emit a stable verdict with hysteresis (no chatter around the threshold). | Verdict does not oscillate under steady state with borderline confidence. |
| FR-VER-5 | Verdicts shall stream to the UI and be queryable by the orchestrator for pass/hold/recover decisions. | `/ws/state` (or `/ws/workflow`) carries verdicts; orchestrator reads them. |

### 3.6 Error recovery / closed-loop (FR-REC)

| ID | Requirement | Verified by |
|----|-------------|-------------|
| FR-REC-1 | A recovery controller shall engage **only** when a predicate fails or confidence < threshold (P2); it is not on the nominal path. | With all predicates passing, no closed-loop control runs (trace shows open-loop only). |
| FR-REC-2 | Recovery shall support at least: re-localize a moved/rotated entity, re-align tube→nozzle by visual servo, regrasp on slip. | Injected 10 mm tube offset is corrected to < 3 mm by the servo. |
| FR-REC-3 | Recovery shall be bounded (max N attempts, timeout) and on exhaustion shall **stop and request help** rather than continue blindly. | After N failed retries the workflow halts with a clear reason. |
| FR-REC-4 | On recovery success, control shall hand back to open-loop and the verifier shall confirm the predicate now passes. | Post-recovery predicate flips to pass; open-loop resumes. |
| FR-REC-5 | Every recovery episode (trigger, actions, outcome) shall be logged for the demo narrative and post-run review. | Recovery timeline reconstructable from logs. |

---

## 4. Non-functional requirements

| ID | Requirement |
|----|-------------|
| **NFR-PERF-1** | Background pose tracking of the 2–3 dynamic entities shall sustain ≥ 5 Hz on the available GPU; FoundationPose *tracking* (refine-only) is the design target (reported >120 FPS on Jetson Thor, far above need). Pose **init** and BundleSDF may be slower and off the hot path. |
| **NFR-PERF-2** | Verification cadence (FR-VER-1) shall not block the control thread; perception runs in its own async task / process and communicates via the twin. |
| **NFR-LAT-1** | Predicate-failure → recovery engagement latency shall be < 500 ms so recovery is timely. |
| **NFR-REL-1** | Loss of a camera or a dropped frame shall degrade to kinematics-only twin state and a `degraded` verifier status, never a crash. |
| **NFR-LICENSE-1** | All components shall be usable under **non-commercial** terms. Confirmed: OpenCV (Apache/BSD), SAM2 (Apache-2.0), Grounding DINO / Grounded-SAM (Apache-2.0), FoundationPose (NVIDIA Source Code License, non-commercial — the GitHub build), BundleSDF (NVIDIA, non-commercial), Kaolin (Apache-2.0 core; `kaolin.non_commercial` NSCL). If this ever goes commercial, FoundationPose must be swapped to the NGC/Isaac-ROS build under the NVIDIA Open Model License, YOLO (AGPL-3.0) reconsidered, and Kaolin `non_commercial` avoided. |
| **NFR-EXT-1** | Adding a new tracked entity or predicate shall require config + one class, not changes to the twin core (matches existing `VerificationAgent` ABC and `EntityKind`). |
| **NFR-OBS-1** | Perception overlays (masks, pose axes, render-compare diff) and predicate verdicts shall be inspectable in the UI for debugging and the demo. |

---

## 5. Component selection & rationale

| Concern | Primary choice | Why | Fallback / alternative | License (non-commercial) |
|--------|----------------|-----|------------------------|--------------------------|
| **Multi-cam calib + hand-eye** | **OpenCV** `calibrateHandEye` (TSAI/PARK) + ChArUco, `solvePnP` for fixed cams | Standard, robust, already stubbed in `core/calibration/pipeline.py`; nothing else needed for the shared frame | `multical` for multi-cam rig bundle-calibration | Apache/BSD ✓ |
| **Detect / segment tube & cap** | **Grounded-SAM 2** (Grounding DINO text prompts → SAM2 masks + video tracking) | Open-vocabulary (no retrain for new labware), SAM2 gives ~44 FPS real-time video masks with streaming memory — ideal to seed + re-seed the tracker | YOLO11 for a fast fixed-class detector (note AGPL-3.0) | Apache-2.0 ✓ (YOLO AGPL) |
| **6-DoF pose & tracking** | **FoundationPose** (CAD-model mode) | CAD meshes of tube/cap exist; init once then refine-only *tracking* is very fast and pairs naturally with Kaolin render-compare; best fit for "track the tube" | **BundleSDF** for objects with no CAD (model-free, but neural-field training ~6.7 s/round → not hot-path) | NVIDIA Source Code License, non-commercial ✓ |
| **Render-compare / geometry / verify** | **Kaolin** (differentiable camera + renderer, representation ops, SPC, Jupyter viewer) | Analysis-by-synthesis for `cap_removed` / pose refinement; geometry plumbing for CAD → twin | Open3D for point-cloud ops / TSDF | Apache-2.0 core; `non_commercial` NSCL ✓ |
| **World-model state layer** | **Own code** on the existing `WorldModel` scene graph | Entity registry, reparenting, pose history, confidence gating are domain-specific; the repo already models this well | — | project code |

---

## 6. Interfaces (contracts to keep parallel work merging)

- **Perception → Twin:** `wm.update_pose(entity_id, T_world, *, source="perception", confidence, stamp)` with gating (FR-WM-4). Kinematics uses the same call with `source="kinematics"`.
- **Verifier → Orchestrator/UI:** existing `VerificationResult{ok, confidence, detail, data}` per predicate, streamed on `/ws/state`; orchestrator polls latest verdict at step gates and the recovery gate subscribes continuously.
- **Recovery ↔ Motion:** recovery controller drives the same capability interfaces (`drivers/capabilities/arm.py`) as nominal motion; it never imports a vendor SDK (respects the layering in `docs/ARCHITECTURE.md`).
- **Persistence:** `calib/` for calibration artifacts; `entities.json` for twin snapshots (already produced by `core/calibration/pipeline.py::_freeze`).

---

## 7. Acceptance criteria (demo-level)

1. Cold start loads cached calibration; twin boots with the fixed rig + arm cam in one world frame (FR-CAL-5, FR-CAL-4).
2. With the tube in the rack, the background verifier reports `capped` and tracks the tube pose live in the UI at ≥ 5 Hz (P1, FR-POSE-1, FR-VER-1).
3. After the dual-arm uncap, `cap_removed` flips to pass using **both** render-compare and torque telemetry (FR-VER-3).
4. **Deliberate failure injection:** nudge the tube ~10 mm off the presented pose → `tube_aligned` fails → recovery servo re-aligns to < 3 mm → predicate flips to pass → open-loop resumes (P2, FR-REC-2, FR-REC-4).
5. Recovery exhaustion path: block the correction → after N attempts the workflow halts with a clear reason instead of aspirating wrong (FR-REC-3).

---

## 8. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Transparent / reflective tube defeats depth & segmentation | Diffuse lighting + polarizer; lean on render-compare (geometry) and marker-on-rack priors; matte tube for the demo. |
| FoundationPose GPU/setup cost eats hackathon time | Pre-bake the Docker image; validate on a recorded clip before live; marker-based pose is the always-available fallback (already in the twin design). |
| Background loop starves control | Run perception in a separate process; twin is the only shared state; cap the loop rate. |
| Confidence thresholds cause verdict chatter | Hysteresis + pose-history stability check (FR-VER-4, FR-WM-3). |
| Non-commercial license creep if productized | NFR-LICENSE-1 documents the exact swaps needed for a commercial build. |

---

## 9. CAD assets (tube / cap meshes)

Real CAD is in the repo under `assets/cad/tubes/`, registered in
`core/worldmodel/meshes.py` and wired into `core/worldmodel/definitions.py`
(`add_tube_with_cap(..., family=...)`, default **50 mL** — the primary demo tube).
Meshes are watertight + winding-consistent, low-poly, and authored in **millimetres**
(scale ×0.001 → metres for FoundationPose / the twin; `SCALE_MM_TO_M` in the registry).

| Mesh key | File | Ø diameter | Height/len | Triangles |
|----------|------|-----------|------------|-----------|
| `tube_50ml_base` | `tube_50ml_base.stl` | 28.0 mm | 112.4 mm | 2,958 |
| `tube_50ml_cap` | `tube_50ml_cap.stl` | 34.0 mm | 16.0 mm | 7,940 |
| `tube_15ml_base` | `tube_15ml_base.stl` | 15.3 mm | 119.3 mm | 2,002 |
| `tube_15ml_cap` | `tube_15ml_cap.stl` | 22.0 mm | 10.0 mm | 9,680 |

This satisfies FR-POSE-1 (CAD-mode pose) — no BundleSDF model-building needed for the
tube/cap. `Entity.mesh` now carries the registry key and is included in the twin snapshot.

## 10. Open questions

- ~~Depth source on the fixed rig~~ — **resolved:** all three cameras are Intel RealSense (RGB-D), so aligned metric depth + factory intrinsics are available on every viewpoint. FoundationPose RGB-D (NFR-PERF-1), depth-backed AprilTag/PnP, and per-camera point clouds are all unlocked.
- ~~Are vendor CAD meshes for the exact tube/cap available~~ — **resolved:** 15 mL + 50 mL tube/cap STLs supplied (§9).
- Tube-rack geometry: `WELL_PITCH` is provisionally set to 30 mm to clear the Ø28 mm 50 mL tube, but the real rack/nest CAD is still needed to fix well pitch and slot pose.
- Recovery authority: may the recovery controller command **both** arms + OT, or arms only, for the demo?
