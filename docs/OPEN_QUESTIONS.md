# Open questions

Running, de-duplicated, prioritized list owned by the PM reanalyze pass. Blockers /
critical-path first. Each item: **ID**, question, **status**, owner, and one-line impact.
Status ∈ `OPEN` · `NEW-this-run` · `ANSWERED`. Answered items are kept (not deleted) with
evidence, then archived below once stale.

_Last reanalyzed: 2026-07-25 (UTC)._

## Active (critical path first)

| ID | Question | Status | Owner | Impact |
|----|----------|--------|-------|--------|
| Q-VERIFY-1 | All four `core/verification/agents.py` agents return `ok=True, confidence=0.0, detail="stub"`. Which **single** verifier do we make real first, and from what signal (gripper-torque drop / simple vision / ArUco-on-cap)? | NEW-this-run | Di | **Top blocker.** Until one verifier can actually fail, the verify→retry loop is a no-op and the "physical verification" half of Track C is unproven. |
| Q-EXEC-1 | `backend/app/workflows/uncap_aspirate.py::_execute` has every driver call commented out (TODO). Who wires the **floor path** (snap-cap uncap → present → OT aspirate) with taught poses + volumes? | NEW-this-run | Lukas / Dale | **Blocker.** Nothing moves autonomously end-to-end; the agent loop currently "passes" against empty actions. |
| Q-OT-1 | OT-One driver exists but there's no git/on-disk evidence of a real action. Does it connect over serial (`/dev/ttyACM0`) and aspirate a known volume? | OPEN | Lukas | Floor rung depends on a real aspirate. |
| Q-CAM-1 | Camera driver present but real capture is unverified, and depth source is undecided — RealSense per fixed cam, or depth only on the arm cam? | OPEN | Di | Any real vision verifier (Q-VERIFY-1) needs frames; FoundationPose (if ever used) prefers RGB-D. |
| Q-CALIB-1 | `core/calibration/pipeline.py` is all `TODO`; the twin is never populated (`services/twin.get_world()` is `None`). What is the **minimum** calibration for the demo — taught poses only, or a real shared world frame? | NEW-this-run | Dale / Di | Arm-held "present" pose accuracy and any twin-query verifier depend on this. |
| Q-BRANCH-1 | HEAD is on `agent-loop-p0` (no upstream) with heavy uncommitted WIP, while the intended feature branch is `initial-setup-and-repo-structure` (checked out in a separate worktree, tracks origin). Where should work converge before freeze? | NEW-this-run | Lukas | Merge/push risk; PM doc commits currently land on `agent-loop-p0`, not the tracked branch. |
| Q-CLAUDE-1 | `ClaudePolicy` is gated on `ANTHROPIC_API_KEY`; `RuleBasedPolicy` is the default. Is the *agent-driven* (vs rule-based) decision loop in-scope for the demo narrative, or is rule-based the demo path with Claude as a stretch? | NEW-this-run | Lukas | Scope + LLM-latency risk vs narrative punch. |
| Q-RACK-1 | Real tube-rack / nest CAD is still missing; `WELL_PITCH` is provisionally 30 mm to clear the Ø28 mm 50 mL tube. | OPEN | Dale | Fixes present/aspirate alignment; provisional pitch may not match the physical rack. |
| Q-RECOVERY-1 | May the recovery controller command **both** arms + OT, or arms only, for the demo? | OPEN | Team | Bounds the recovery behavior scope (moot until recovery is built — see planned status). |
| Q-DEMO-1 | What is the deliberate **failure-injection** cue for the demo (which step, what perturbation), given verifiers are stubbed? Real punch needs one verifier that can fail on cue. | NEW-this-run | Team | The Track C narrative ("verify → retry / stop") is only compelling if a failure is shown; depends on Q-VERIFY-1. |

## Resolved / stale

| ID | Question | Status | Evidence |
|----|----------|--------|----------|
| Q-CAD-1 | Are vendor CAD meshes for the exact tube/cap available? | ANSWERED (2026-07-25) | 15 mL + 50 mL tube/cap STLs supplied under `assets/cad/tubes/`, registered in `core/worldmodel/meshes.py`, wired into `definitions.py::add_tube_with_cap` (default 50 mL). Satisfies FR-POSE-1 without BundleSDF. |
| Q-CONFIG-PATH | Where is the fleet declared? (doc said `backend/app/core/config.py`) | ANSWERED (2026-07-25) | Post-refactor it is `core/config.py` (`DEFAULT_FLEET`); `PROJECT_PLAN.md` corrected this run. |
