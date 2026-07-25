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

---

## Critical review log (newest first)

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
