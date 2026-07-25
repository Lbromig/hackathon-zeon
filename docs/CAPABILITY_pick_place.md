# Capability requirements — safe tube pick & place

The manipulation primitive the whole demo rests on: pick a tube with the gripper and place
it at an **exact location and pose**, **never inverting it** (spilling is a critical flaw),
always moving through **safe waypoints**.

## Functional requirements

- **FR1 · Grasp.** Pick a tube by its body with the gripper at the grasp width defined by the
  object/anchor. Grasp is firm enough not to slip, not so hard it crushes.
- **FR2 · Exact placement.** Place the tube so its placement anchor lands on the specified
  world-frame target within tolerance: position ≤ `PLACE_POS_TOL` (default 2 mm), orientation
  ≤ `PLACE_ROT_TOL` (default 3°).
- **FR3 · Waypoint discipline.** Every pick and place follows this ordered path, and the exact
  reverse on exit:

  ```
  current →  SAFE_TRANSIT_Z  →  APPROACH_ABOVE  →  FINAL_APPROACH (grasp/place)
                                     ↑                     │
                                     └──── retreat ────────┘  (straight up)
  ```
  - **SAFE_TRANSIT_Z** — a user-defined medium height; all lateral (XY) travel happens here.
  - **APPROACH_ABOVE** — a point a standoff distance directly above the pick/drop point,
    same orientation as the grasp.
  - **FINAL_APPROACH** — a pure vertical descent from APPROACH_ABOVE to the grasp/place pose.
- **FR4 · Lateral only at safe height.** No XY traversal except at `SAFE_TRANSIT_Z`. Never
  cut across the deck at low height.
- **FR5 · Vertical final approach & retreat.** The final approach and the post-grasp/post-place
  retreat are pure ±Z (no lateral component) so neighbours aren't knocked.

## Safety invariants (critical)

- **SI1 · Upright invariant (anti-spill).** While a tube is grasped, its opening axis must stay
  within `MAX_TILT` (default 20°) of world **+Z** at *every* commanded waypoint **and** along
  interpolated paths. The tube is **never** inverted. A plan that would violate this is refused.
- **SI2 · No flip in orientation.** Orientation is held constant (top-down, cap up) across
  pick → transit → place. No wrist motion rotates the tube past `MAX_TILT`.
- **SI3 · Carry-aware.** After grasping, register the tube as attached to the tool so motion
  planning accounts for it; detach on release. (Also reparents the entity in the digital twin.)
- **SI4 · Abort-to-safe.** On any violation or failure, stop and retreat **vertically** to
  APPROACH_ABOVE then SAFE_TRANSIT_Z — never tilt — and request help. Never drop or continue.

## Parameters (config)

| Param | Meaning | Default |
|-------|---------|---------|
| `SAFE_TRANSIT_Z` | user-defined medium transit height (world Z, m) | required |
| `APPROACH_STANDOFF` | height above pick/drop for APPROACH_ABOVE (m) | 0.05 |
| `PLACE_POS_TOL` / `PLACE_ROT_TOL` | placement tolerance | 2 mm / 3° |
| `GRASP_WIDTH` | gripper opening to grasp (m) | from anchor |
| `MAX_TILT_DEG` | max tube tilt from +Z while held | 20° |
| `SPEED_TRANSIT` / `SPEED_FINE` | transit vs final-approach speed | — |

## Pre/postconditions & verification (per phase → ties to the twin + verify agents)

| Phase | Precondition | Postcondition (verified) |
|-------|--------------|--------------------------|
| Pick | source well occupied; path clear | gripper width in grasp band; tube attached; tube tilt ≤ MAX_TILT |
| Transit | tube attached | tilt ≤ MAX_TILT at every waypoint (else SI4) |
| Place | at APPROACH_ABOVE over target | tube at target ≤ tol; upright; gripper released; tube detached; well/site marked occupied |

Verification reads the digital twin (`docs/DIGITAL_TWIN.md`): e.g. `upright = angle(tube.+Z, world.+Z) ≤ MAX_TILT`; `placed = ‖tube.anchor − target‖ ≤ PLACE_POS_TOL`.
