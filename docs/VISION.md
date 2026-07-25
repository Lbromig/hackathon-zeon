# Vision — Cooperative Uncap → Aspirate, verified

## Current refined vision

We prove that **two low-cost arms can cooperatively defeat a container a single arm can't —
unscrewing a sealed tube — and that a robot can *know* it worked before it acts on the
result.** One arm holds, the other ratchets the cap off; the holding arm then carries the
open tube to an Opentrons OT-One and presents it while the pipette aspirates. Every physical
step is gated by a verification agent that can **pass, retry, or stop-and-ask** — so the demo's
punch is not just the dexterity, it's the moment a deliberately injected failure is *caught*
and the system refuses to aspirate the wrong thing. Audience: hackathon judges scoring Track C
on **dexterity + physical verification**; the Opentrons hand-off is the narrative reveal that
lands both themes in one motion. The floor we will always be able to show is a snap-cap +
OT-nest aspirate; the target adds the dual-arm screw-cap and arm-held aspiration; the honest
differentiator over "another pick-and-place" is the closed **verify → retry** loop.
**Verification substrate is now marker-based, not learned:** a real AprilTag detector +
6-DoF pose (`core/perception/fiducials.py`) feeding a live twin is the pragmatic sensing
layer for verdicts — the heavy learned-perception stack (SAM 2 / FoundationPose / Kaolin)
stays a post-hackathon ambition, off the demo path. **As of this cycle the last infra gap is
closed:** the camera transport is mounted (`cameras.router` + `camera_hub`, detections on
`/ws/state`) and marker ids are the real printed stock (180–224). Nothing structural now stands
between the repo and a real verdict — what remains is a threshold on an already-computed pose and
one wired motion path, i.e. the two blockers that have persisted for two cycles: a real verifier
and a non-empty `_execute`. The vision is no longer bottlenecked by capability; it is bottlenecked
by choosing to spend a block on the scored half instead of more substrate.

---

## Critical review log (newest first)

### 2026-07-26T00:14Z — The infra gap closed; the scored gap didn't move (a wasted cycle for Track C's theme)

**Demo-readiness score: 3.5/10 for the *stated* PoC (verified uncap→aspirate) — flat vs last review; ~7/10 for a live teleop + streaming-UI show, up from ~6.** The headline number is unchanged **on purpose**: the one thing that would move it — a verifier that can return `ok=False` — was not touched. What improved is the demo's *supporting cast*, not its thesis.

**What changed since the last review.** A genuinely productive infra cycle: the **cameras router is mounted** (`main.py:67`, closing last review's #1 ADD item), backed by a 293-line worker-threaded `camera_hub`, with detections streaming on `/ws/state`; a **RealSense RGB-D driver** covers all three fixed cams (uncommitted WIP); the **teach layer is hardened and tested** (`test_teach_api.py`, 278 lines; `TeachPanel.vue`); the **workflow orchestrator streams** over `/ws/workflow` with a `/api/workflow/plan` endpoint; `MARKER_MAP` now uses **real** stock ids (`tag36h11` 180–224); and there's **docker packaging**, a **mock fleet**, and **motion-validation scripts** (`validate_motion.py`, `gripper_sequence.py`). This is real, useful engineering — a much better teleop/streaming demo than yesterday.

**What did NOT change — and it is, again, the whole ballgame.** All four `core/verification/agents.py` agents **still** `return VerificationResult(ok=True, confidence=0.0, detail="stub")`. `uncap_aspirate.py::_execute` **still** has every driver call commented out — and now demonstrably so does the agent path, since `agent/tools.py::Skill.run` just calls `uncap_aspirate._execute`. So after a full cycle the *verified* claim is exactly as unbacked as it was, and both orchestration paths still "pass" against empty actions.

**The single biggest threat — this cycle it's prioritization, not capability.** Last review the threat was "the verifier is a no-op but the substrate just landed." Now the substrate has sat complete for a full cycle and *nobody wrote the threshold*. The team had every input needed — marker pose (`entity_world_pose`), reachable detections (`/ws/state`), real motion primitives (`pick_place.plan/execute`) — and spent the cycle building around the two blockers instead of through them. `tube_aligned` is still one subtraction and a threshold; `cap_removed` is still "marker 224 gone or torque drop." The risk has shifted from *can we?* to *will we choose to?* — and every cycle that ends with `ok=True` hardcoded is a cycle proving motion (easy, unscored differentiator) and none of verification (hard, scored differentiator). A judge asking "what happens if the cap doesn't come off?" still gets no live answer.

**Refine scope for the time remaining.**
- **CUT (unchanged):** learned perception (SAM 2 / FoundationPose / Kaolin), background verifier, closed-loop recovery — zero code, keep as post-hackathon docs. Also **freeze infra**: cameras, teach, docker, and mock fleet are now good enough for the demo — further polishing them is displacement activity.
- **KEEP:** real single-arm motion via teach; the P0 agent loop as orchestrator; the hardcoded `PLAN`; fiducial detection + calibration twin + the now-live camera transport as the verification substrate.
- **ADD, in strict priority (same list as last cycle because it wasn't done):** (1) make **one** verifier real off the existing fiducial pose — `tube_aligned` (pose-error threshold) or `cap_removed` (marker-gone) — so the loop can genuinely fail; (2) wire `_execute` for the **floor** path (snap-cap → present → OT aspirate), calling the real `pick_place` primitives; (3) script **one deliberate failure injection** on that verifier for the reveal. Note the reordering: last cycle put "mount the router" first — that's done, so the verifier is now the top ADD. One real `ok=False` beats another 500 lines of infra.
- **DECIDE (Q-PRIORITY-1):** if the team is deliberately betting on the screw-cap dexterity ceiling over verification, say so — but that leaves the *scored* Track C theme on a slide while the demo proves the unscored one.

**Opposing view (steelman).** The infra wasn't wasted: a mounted camera hub + RealSense depth is exactly what a vision verifier consumes, and a rock-solid teleop/streaming UI is the stage the reveal will play on. If the verifier truly is "one subtraction," it can be done in the final hour with the substrate now fully in place — so front-loading the stage isn't irrational. Fair — but "one subtraction" left undone for two cycles is a pattern, not a schedule; treat the verifier as the *next* commit, not the *last* one.

**Verdict:** A strong infra cycle that widened the stage and didn't advance the plot. The repo has had everything needed to stop being theater for two cycles running. The next block has exactly one job: *make one verifier return a real `ok=False`.* Until then, Track C's scored half is a stub with excellent lighting.

### 2026-07-25T23:32Z — The substrate for a real verifier now exists; the verifier still doesn't

**Demo-readiness score: 3.5/10 for the *stated* PoC (verified uncap→aspirate); ~6/10 for a teleop-only "two real arms" show.** Up half a point from this morning — not because anything is *verified* yet, but because the pieces needed to make a verifier real landed on disk.

**What changed since the last review.** Di's perception work is no longer vaporware: `core/perception/fiducials.py` is a real AprilTag `tag36h11` detector with 6-DoF pose (`solvePnP` IPPE_SQUARE) and an `entity_world_pose()` that yields a corrective world pose (test: `test_fiducials.py`). The calibration pipeline (`/ws/calibrate`) now **runs end-to-end and publishes the twin** — it connects the fleet, registers rack + tip-box geometry, seeds a demo tube, and calls `twin.set_world`, so `get_world()` is no longer `None`. A cameras router + `camera_hub` (MJPEG stream + detections) were also written.

**What did NOT change — and it's the whole ballgame.** All four `core/verification/agents.py` agents still `return VerificationResult(ok=True, confidence=0.0, detail="stub")`, and `uncap_aspirate.py::_execute` still has every driver call commented out. So the headline claim — *verified* — remains **unbacked**, and the agent loop still walks the plan "passing" against empty actions.

**The single biggest threat — verification is still a no-op, but the excuse is gone.** Previously the honest defense was "we have no signal to verify from." That defense is dead: `fiducials.detect()` returns marker poses today, and `entity_world_pose()` turns them into a twin pose. `tube_aligned` could be *one function* — compare the detected present-pose to the expected pose, threshold the error. `cap_removed` could be "cap marker 224 no longer detected" or a gripper-torque drop. The gap is now pure wiring (a threshold + a subtraction), not research. Every hour that passes with `ok=True` hardcoded is an hour spent proving the *easy* half of Track C (motion) and none of the *scored* half (verification). Second-order risk: the cameras router that feeds any vision verifier **isn't mounted** in `main.py` (Q-CAMERAS-MOUNT) — a one-line omission silently blocking the whole vision path — and `MARKER_MAP` still uses example ids with `identity()` offsets (Q-FIDUCIAL-IDS), so twin poses are cosmetically populated but numerically placeholder.

**Refine scope for the time remaining.**
- **CUT (from the demo path):** the learned perception stack (SAM 2 / FoundationPose / Kaolin), the background verifier, and closed-loop recovery. Unchanged — zero code, no deps, no time. Doc them as the post-hackathon architecture.
- **KEEP:** real single-arm motion via the teach layer; the P0 agent loop as orchestrator; the hardcoded `PLAN`; and — newly promoted — **fiducial detection + the calibration twin as the verification substrate**. This is real, cheap, RGB-only, and already tested.
- **ADD, in strict priority order:** (1) **mount the cameras router** (`include_router(cameras.router)` — one line) so frames/detections are reachable; (2) wire `_execute` for the **floor** path so something moves autonomously end-to-end; (3) make **one** verifier real off the fiducial pose already implemented — `tube_aligned` (pose-error threshold) or `cap_removed` (marker-gone) — so the loop can genuinely fail; (4) script **one deliberate failure injection** on that verifier for the reveal. One real verdict from a marker beats four stubs and a slide.

**Opposing view (steelman).** If the two-arm *screw-cap* uncap is the judged wow-factor, Dale's dexterity is the higher-ceiling bet and stubs can be swapped late. Fair — but the marker verifier is now so cheap (a threshold on an existing pose) that skipping it is no longer a time trade-off, it's leaving the scored theme on the table. Sequence the floor + one honest verifier first as insurance, then spend surplus on the screw-cap ceiling.

**Verdict:** The tools to stop being theater are now in the repo. The next block is not "build perception" — it's *mount one router, subtract two poses, threshold the result.* If this cycle ends with even one verifier returning a real `ok=False`, the PoC crosses from plumbing to proof.

### 2026-07-25 — Verification is theater; the demo is currently below its own floor

**Demo-readiness score: 3/10 for the *stated* PoC (verified uncap→aspirate); ~6/10 for a teleop-only "look, two real arms" show.**

**What would actually run right now.** The real, on-disk assets are: the xArm driver on the
genuine `XArmAPI` (connect + move + gripper), a teach panel UI (jog / gripper / pose library),
a FastAPI backend that boots with REST + websockets, a `DeviceManager` with mock drivers, and a
**P0 agent loop** (`backend/app/agent/`) that observes→decides→executes→verifies over `/ws/agent`
using an offline rule-based policy. The world-model *entity model* and CAD tube/cap meshes exist.
So a live teleoperated single-arm demo is real today.

**Which rung is real?** *None of the autonomous ones.* The floor (snap-cap + OT-nest aspirate,
end-to-end) is **not** runnable: `uncap_aspirate.py::_execute` has every driver call commented out,
so the hero workflow moves nothing. Target and stretch are aspirational.

**The single biggest threat — verification is a no-op.** All four agents in
`core/verification/agents.py` return `VerificationResult(ok=True, confidence=0.0, detail="stub")`.
The verify→retry loop — the spine of the whole pitch and half of the Track C theme — **can never
fail and never retry.** Worse, this makes the P0 agent loop *look* successful: it walks the plan,
"verifies," and reports done against empty actions. A judge who asks "what happens if the cap
doesn't come off?" gets no answer today. This is more damaging than any missing motion, because it
hollows out the differentiator (verification) rather than a nice-to-have (a second arm).
Compounding it: `core/calibration` is all `TODO`, so `services/twin.get_world()` is `None` — the
twin the design wants to *query* for verdicts is never populated, and no perception dependencies
(SAM 2 / FoundationPose / Kaolin) are installed.

**Refine scope for the time remaining.**
- **CUT (from the demo path):** the full perception subsystem — Grounded-SAM 2, FoundationPose,
  Kaolin render-compare, the background verifier, and closed-loop recovery. Zero code, no deps,
  no time. Keep it in the docs as the post-hackathon architecture; do **not** stake the demo on it.
- **KEEP:** real single-arm motion via the teach layer; the P0 agent loop as the orchestrator; the
  hardcoded `PLAN` as the deterministic happy path.
- **ADD, in priority order:** (1) wire `_execute` for the **floor** path so *something* moves
  autonomously end-to-end (snap-cap uncap → present → OT aspirate); (2) make **one** verifier real
  — `cap_removed` from a gripper-torque drop or a trivial vision/ArUco cue is enough — so the loop
  can genuinely fail; (3) script **one deliberate failure injection** on that verifier for the
  reveal. One real end-to-end signal with one real verdict beats four stubs and a slide.

**Opposing view (steelman).** If the two-arm *screw-cap* uncap is the judged wow-factor, Dale's
dexterity work is the higher-variance, higher-ceiling bet and the stubs can be swapped late. Fair —
but sequence it safely: a *floor that runs with one honest verifier* is the insurance that turns a
risky dexterity attempt into a guaranteed-good demo instead of an all-or-nothing gamble.

**Verdict:** Impressive plumbing, but the PoC's headline claim — *verified* — is currently unbacked.
Spend the next block buying one real verdict and one real end-to-end motion, not more scaffolding.
