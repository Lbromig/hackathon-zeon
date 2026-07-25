# Open questions

Running, de-duplicated, prioritized list owned by the PM reanalyze pass. Blockers /
critical-path first. Each item: **ID**, question, **status**, owner, and one-line impact.
Status ∈ `OPEN` · `NEW-this-run` · `ANSWERED`. Answered items are kept (not deleted) with
evidence, then archived below once stale.

_Last reanalyzed: 2026-07-25T23:32Z._

## Active (critical path first)

| ID | Question | Status | Owner | Impact |
|----|----------|--------|-------|--------|
| Q-VERIFY-1 | All four `core/verification/agents.py` agents still return `ok=True, confidence=0.0, detail="stub"`. Which **single** verifier do we make real first, and from what signal? The substrate now exists — `fiducials.py` gives marker 6-DoF pose and `entity_world_pose()`, so `tube_aligned` (compare detected present-pose to expected) or `cap_removed` (cap marker 224 disappears / gripper-torque drop) are cheap. | OPEN | Di | **Top blocker.** Until one verifier can actually fail, the verify→retry loop is a no-op and the "physical verification" half of Track C is unproven. The "no signal" excuse is gone — pose + telemetry are on disk. |
| Q-EXEC-1 | `backend/app/workflows/uncap_aspirate.py::_execute` still has every driver call commented out (TODO). Who wires the **floor path** (snap-cap uncap → present → OT aspirate) with taught poses + volumes? | OPEN | Lukas / Dale | **Blocker.** Nothing moves autonomously end-to-end; the agent loop currently "passes" against empty actions. |
| Q-CAMERAS-MOUNT | `backend/app/api/cameras.py` (MJPEG `/api/cameras/{id}/stream` + `/detections`) and `services/camera_hub.py` exist, but `main.py` never `include_router(cameras.router)` — so all camera endpoints are unreachable. | NEW-this-run | Di / Lukas | **One-line fix, high leverage.** The camera-overlay demo and any real vision verifier (Q-VERIFY-1, Q-CAM-1) are dead until this router is mounted. |
| Q-OT-1 | OT-One driver exists but `connect()`/`_send()` are still `TODO` (transport not opened) — no git/on-disk evidence of a real aspirate over serial (`/dev/ttyACM0`). | OPEN | Lukas | Floor rung depends on a real aspirate. |
| Q-CALIB-1 | Calibration now runs end-to-end and publishes the twin, **but** `hand_eye` / `world_frame` / `arm_to_arm` / `locate_instruments` are still `TODO` (PlaceholderScanAdapter), so twin poses are placeholders. What is the **minimum** real calibration for the demo — taught poses only, or a real shared world frame from the fiducial board? | OPEN (was partly answered) | Dale / Di | Arm-held "present" accuracy and any twin-query verifier depend on real poses, not the placeholder scan. |
| Q-FIDUCIAL-IDS | `core/calibration/markers.py::MARKER_MAP` uses **example** ids (180–186, 224) with `identity()` marker→entity offsets ("replace offsets with measured values"), and the world-frame board ids `(0,1,2,3)` are **not** in the printed tag36h11 stock (180–224). | NEW-this-run | Di / Dale | Fiducial-derived twin poses will be wrong until offsets are measured and real board tags assigned — blocks trusting perception for verification. |
| Q-CAM-1 | Camera capture path now exists (`camera_hub` + MJPEG), but depth source is still undecided — RealSense per fixed cam, or depth only on the arm cam? | OPEN | Di | Fiducial pose works RGB-only; FoundationPose (if ever used) prefers RGB-D. |
| Q-BRANCH-1 | HEAD is on `agent-loop-p0` (**still no upstream**, and not present on `origin` — remote has `main`, `initial-setup-and-repo-structure`, `feat/ot-one-serial-driver`, `fix/land-tip-pickup`, backups). Where should work converge before freeze? | OPEN | Lukas | Merge/push risk. **PM doc commits cannot be pushed:** a plain `git push` fails with "no upstream", and PM policy forbids creating a remote branch / switching. Commit `32e9eca` (and any this run) stay local until a human sets an upstream or cherry-picks the doc commits onto a tracked branch. |
| Q-CLAUDE-1 | `ClaudePolicy` is gated on `ANTHROPIC_API_KEY`; `RuleBasedPolicy` is the default. Is the *agent-driven* (vs rule-based) decision loop in-scope for the demo narrative, or is rule-based the demo path with Claude as a stretch? | OPEN | Lukas | Scope + LLM-latency risk vs narrative punch. |
| Q-DEMO-1 | What is the deliberate **failure-injection** cue for the demo (which step, what perturbation)? The Track C "verify → retry / stop" punch is only compelling if a failure is shown on cue — depends on Q-VERIFY-1. | OPEN | Team | Narrative-critical; needs one verifier that can fail. |
| Q-RACK-1 | Real tube-rack / nest CAD still missing; `WELL_PITCH` provisionally 30 mm to clear the Ø28 mm 50 mL tube. | OPEN | Dale | Fixes present/aspirate alignment; provisional pitch may not match the physical rack. |

## Resolved / stale

| ID | Question | Status | Evidence |
|----|----------|--------|----------|
| Q-CAD-1 | Are vendor CAD meshes for the exact tube/cap available? | ANSWERED (2026-07-25) | 15 mL + 50 mL tube/cap STLs under `assets/cad/tubes/`, registered in `core/worldmodel/meshes.py`, wired into `definitions.py::add_tube_with_cap` (default 50 mL). Satisfies FR-POSE-1 without BundleSDF. |
| Q-CONFIG-PATH | Where is the fleet declared? (doc said `backend/app/core/config.py`) | ANSWERED (2026-07-25) | Post-refactor it is `core/config.py` (`DEFAULT_FLEET`); `PROJECT_PLAN.md` corrected. |
| Q-RECOVERY-1 | May the recovery controller command both arms + OT, or arms only? | DEFERRED — not on demo critical path | Recovery controller is unbuilt (no code); revisit only if closed-loop recovery re-enters the demo scope. |
