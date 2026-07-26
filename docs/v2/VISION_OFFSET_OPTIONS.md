# Options: making the tip→tube offset robust

**The problem.** The design solves a 3-DoF offset (dx, dy, dz in mm) from 2D pixel measurements via a
measured per-camera image jacobian `J` (2×3, px/mm). Inverting an ill-conditioned `J` amplifies
measurement noise: a camera yawed ~45° makes +x and +z project almost identically, so the restricted
2×2 submatrix has condition ≈ 76 and a **3 px detection error becomes ~20 mm of commanded motion** —
while the residual stays 0 and the reported confidence stays high, because a 2×2 system with 2
unknowns fits any input exactly. See [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md) B2.

There are two independent things to fix: **stop producing confidently wrong numbers**, and **stop
needing an accurate jacobian in the first place**. Options below are grouped accordingly, then
combined into a recommendation.

---

## A. Structural — remove the ill-conditioning instead of tolerating it

### O1 — Solve tag poses directly (PnP); delete the jacobian entirely ⭐
`core/perception/fiducials.py` **already** solves a full 6-DoF tag pose per detection
(`T_cam_marker`, via `SOLVEPNP_IPPE_SQUARE`, [fiducials.py:94-170](../../core/perception/fiducials.py#L94)).
With a flat tag on the pipette carriage **and** a flat tag on the tube assembly — which you have now
approved — the offset is a **3D vector subtraction in one camera's frame**:

```
d = (T_cam_tube · p_tube_top) − (T_cam_carriage · p_tip_bottom)
```

No jacobian, no conditioning problem, no per-camera calibration to go stale, and **z comes out
directly** rather than being the poorly observed axis. Both tags in one frame means the camera's own
pose is irrelevant — errors common to both cancel.

- **Needs:** camera intrinsics (one-time per camera; a ChArUco pass, or RealSense factory values which
  the driver already reports), and the fixed offsets from each tag to the geometric feature it stands
  for (tag→tip-bottom, tag→tube-rim), measured once with calipers.
- **Costs:** intrinsics become a real prerequisite (P-7, currently listed as *not* required).
- **Precision:** dominated by tag size. A 20 mm tag is noisy at range; **40–50 mm flat tags** put PnP
  range error in the 1–2 mm band at these distances.
- **Verdict:** this is the option that makes the problem go away rather than managing it. It also
  *reduces* scope: `jacobian.py` and the 6-jog calibration script are deleted.

### O2 — Fix the camera geometry
The root cause is placement. Two views ~90° apart make the stacked 4×3 system well conditioned by
construction. Mounting decision, not math; highest leverage per unit effort.
- **Needs:** the handover camera looking down-ish and the gripper camera side-on, which is roughly the
  case already in the reference frames.
- **Verdict:** do it regardless of which solver ships. Free robustness.

### O3 — Decouple the axes: each camera controls only what it observes well
Don't ask one solve for 3 DoF. Take **x,y from the top-down view** (where they are directly observable
and z is nearly unobservable) and **z from the side view**. Command each axis from the camera that
sees it.
- **Verdict:** simple, physically honest, and a natural fallback when only one tag is visible. Keep as
  the degraded path.

### O4 — Measure more precisely rather than tolerating amplification
Use the tag's **four corners** (8 measurements) instead of its centre (2); average over N frames;
subpixel refinement is already enabled (`CORNER_REFINE_SUBPIX`). Takes 3 px to well under 1 px, so
even a badly conditioned solve degrades to ~2–5 mm instead of 20 mm.
- **Verdict:** cheap, compounding, do it always.

---

## B. Algorithmic — safety nets, for when calibration is stale or wrong anyway

### O5 — Damped least squares (Tikhonov / Levenberg) ⭐
Replace `(JᵀWJ)⁻¹JᵀW` with `(JᵀWJ + λI)⁻¹JᵀW`. The ill-conditioned direction is *suppressed* instead
of amplified: bias traded for variance. The standard robotics answer to a near-singular jacobian.
- **Costs:** one parameter; slower convergence along poorly observed directions.
- **Verdict:** always on. It is three lines and it removes the 20 mm failure outright.

### O6 — Online gain adaptation / Broyden update ⭐
Don't trust `J`'s magnitude. Command a small step, **measure the pixel change it actually produced**,
and update the effective jacobian (secant/Broyden) or just a scalar gain. The loop then converges
despite a 2× wrong jacobian, and **diverges visibly and immediately on a sign error** instead of
walking the head into the deck.
- **Verdict:** the single best answer to "the jacobian is stale, or measured at a different
  resolution, or on a camera that has since been swapped" — which is the failure your hub/cable churn
  makes likely. Also makes the loop self-calibrating enough that a wrong jacobian is a slow run rather
  than a crash.

### O7 — Conditioning gate with refusal
SVD the restricted submatrix; refuse (`low_observability`) unless σ_min clears a floor and cond ≤ ~10.
- **Verdict:** necessary but *not sufficient* — refusing is not converging. Ship it as the guard, not
  as the fix.

### O8 — Report real uncertainty, per axis
From the fused solve, `Σ = (JᵀWJ)⁻¹` with `W = diag(1/σ_px²)`; publish `sigma_mm` per axis. This is
the brief's "confidence" in defensible form: it degrades correctly with conditioning, with a missing
view, and with a poor detection — unlike the residual, which is structurally zero.
- **Verdict:** required by R-VIS-4 whatever else ships.

### O9 — Bound the per-iteration command, and detect no-progress
Clamp each commanded step; abort if N iterations do not reduce the offset.
- **Verdict:** already in the plan (D14/R-VIS-7). It limits blast radius; it does not fix accuracy.

---

## Recommendation

**Ship O1 as primary, with O5 + O6 + O7 + O8 + O9 as the always-on frame, and O3 as the degraded
path. Do O2 and O4 regardless.**

Concretely:

| Layer | What |
|---|---|
| Primary solver | **O1** — PnP on two 40–50 mm flat tags → 3D offset by subtraction. No jacobian |
| Fallback (one tag visible / occluded) | **O3** axis decoupling, driven by a jacobian, with **O5** damping and **O6** online gain adaptation |
| Always on | **O4** 4-corner + multi-frame averaging · **O7** conditioning refusal · **O8** per-axis `sigma_mm` · **O9** clamp + no-progress abort |
| Bench | **O2** near-orthogonal views · 40–50 mm **flat** tags (P-2b) |

Why this ordering: O1 removes the failure mode instead of bounding it, uses code that already exists
and is bench-proven, and gets *cheaper* the more you lean on fiducials — which is the direction you
have already chosen. The jacobian path survives only as the occluded-tag fallback, where O5/O6 keep it
safe.

**The one thing this changes in the plan:** camera **intrinsics move from "not required" to a
prerequisite** (P-7). That is a one-time per-camera ChArUco pass, or the RealSense factory intrinsics
the driver already exposes ([realsense.py:74-79](../../drivers/camera/realsense.py#L74)) — cheap, and
it buys metric 3D from a single view, which is worth far more than it costs.

**What gets deleted if O1 is adopted:** `core/perception/jacobian.py`, `scripts/calibrate_lh_jacobian.py`,
`data/camera_jacobians.json` and the fingerprint-binding machinery around it (D18) shrink to
"intrinsics per camera, bound to camera identity" — which the camera-identity work (R-CAM-6) provides
anyway.

## Fallback if intrinsics prove impractical

Then O1 is unavailable and the jacobian path is primary. In that case the *minimum* set is
**O5 + O6 + O7 + O8 + O9 + O4**, and O6 becomes load-bearing rather than a safety net — it is the only
one of those that survives a jacobian measured on a camera that has since been swapped.
