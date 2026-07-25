# ZEON Systems integration

ZEON is a robotics/lab-automation platform for **designing, simulating, and running**
workflows on real hardware. You author **skills** (single robot actions in Python),
chain them into **workflows** (visual graphs), capture **worlds** (the 3D scene with
**objects** + **anchors** from a shared mesh database), and run the same project in the
**cloud simulator** or on **real hardware**. Versioning is built in (`sync`), driven from a
**CLI** (`zeon new/project/auth/mesh-database/sync`) and a Web IDE.

Source: https://readme.zeonsystems.app/docs — Skill runtime API, "Objects, anchors, and the
world model", "Arm motion and the gripper", Key concepts.

## Why this matters for us

ZEON already provides the hard parts we'd otherwise build: inverse kinematics + Cartesian
motion, a world model with objects/anchors, carry-aware planning, anchor snapping for exact
placement, and a **cloud sim** so we can develop the whole workflow before touching hardware.
Our design maps onto it almost 1:1.

### Concept mapping

| Ours | ZEON equivalent |
|------|-----------------|
| `drivers/capabilities/*` (arm/liquid/camera) + workflow steps | **Skills** (`from execution.execution_functions import *`) |
| `workflows/uncap_aspirate.py` orchestrator | **Workflow** graph chaining skills |
| `worldmodel/` digital twin (entities + poses + reparenting) | **World** + **objects** + **anchors**; `attach/detach_object_from_arm`; `snap_object_*` |
| `calibration/` (ArUco + ruler + scan) | **Worlds/World Builder**, **anchors**, **Perception**, anchor snapping |
| our device fleet config | **Hardware Setup** + sim/real toggle per run |
| git branch / push | ZEON **sync** (cloud-canonical version control) |

## How ZEON satisfies the safe pick/place requirements

The requirements in `docs/CAPABILITY_pick_place.md` map directly onto ZEON runtime calls:

| Requirement | ZEON mechanism |
|-------------|----------------|
| FR1 grasp at correct width | `load_object_anchor` → `width`; `set_gripper(arm, width)` |
| FR2 exact placement pose | `snap_object_anchor_to_world_pose(id, anchor, xyz, wxyz)` pins the tube's anchor onto the target |
| FR3 APPROACH_ABOVE before final approach | `anchor_preapproach(anchor, default_standoff)` returns the standoff point along the anchor's −Z |
| FR3/FR5 vertical final approach & retreat | `move_arm` to pre-approach, then to the anchor `xyz`, reusing the anchor `rpy` (no lateral) |
| FR4 lateral only at safe height | our planner sequences the XY move at `SAFE_TRANSIT_Z` between the two pre-approach points |
| SI1/SI2 never flip (anti-spill) | hold `orientation` constant at the grasp `rpy` across all held moves; guard rejects any `rpy` > MAX_TILT from it (see `motion/pick_place.py::assert_upright`) |
| SI3 carry-aware planning | `attach_object_to_arm` after grasp, `detach_object_from_arm` on release |
| SI4 abort-to-safe | on violation, `move_arm` straight up to `safe_z` at the current XY, keeping `rpy` |

ZEON's own docs warn that motion calls (`move_arm`, `set_gripper`) command hardware
immediately and nothing confirms a move is safe — which is exactly why our **upright guard**
and **pre-flight waypoint check** run *before* every commanded move.

## Integration options

**A · Build natively on ZEON (recommended for the demo).** Implement each capability as a
ZEON skill, wire them into a workflow, and run in the cloud sim first, then real hardware.
We get IK, world model, and sim for free. Our `motion/pick_place.py` planner + guard port
directly on top of `move_arm`/`set_gripper` (a ~10-line `Mover`).

**B · Keep our FastAPI/Vue stack, use ZEON as the execution + world layer.** Our
`ArmDriver`/`LiquidHandlerDriver` implementations delegate to ZEON runtime calls; our
`WorldModel` is hydrated from `get_object_pose`/`load_object_anchor`. More glue, but keeps
our UI/verification services. Good post-hackathon.

**Recommendation:** A for the 24h build (fastest path to a sim-proven demo), keep our
verification agents + UI as a thin layer reading ZEON's world model. Our repo's abstraction
(capabilities, twin, verify loop) stays valid either way — ZEON becomes the execution backend.

## Reference: safe pick/place as a ZEON skill

```python
from execution.execution_functions import *
import numpy as np

def _rot(rpy):
    r, p, y = rpy
    cr, sr, cp, sp, cy, sy = np.cos(r), np.sin(r), np.cos(p), np.sin(p), np.cos(y), np.sin(y)
    return np.array([[cy*cp, cy*sp*sr-sy*cr, cy*sp*cr+sy*sr],
                     [sy*cp, sy*sp*sr+cy*cr, sy*sp*cr-cy*sr],
                     [-sp,   cp*sr,          cp*cr]])

def _upright_ok(rpy, grasp_rpy, max_tilt_deg=20.0):
    R = _rot(grasp_rpy).T @ _rot(rpy)
    ang = np.arccos(np.clip((np.trace(R)-1)/2, -1, 1))
    return ang <= np.deg2rad(max_tilt_deg)

def robotic_code(tube, target, arm="right_arm", safe_z=0.25, standoff=0.05):
    """Pick `tube` and place it exactly on `target`, never inverting it."""
    grasp = load_object_anchor(tube.id, "grasp_vertical")
    r = grasp["rpy"]                                  # grasp orientation held throughout
    assert _upright_ok(r, r)                          # sanity

    # PICK: safe height -> above -> down -> grasp -> straight up -> safe height
    move_arm(arm, [grasp["xyz"][0], grasp["xyz"][1], safe_z], r)
    move_arm(arm, anchor_preapproach(grasp, default_standoff=standoff), r)
    set_gripper(arm, grasp["width"] + 0.02)
    move_arm(arm, grasp["xyz"], r)
    set_gripper(arm, grasp["width"])
    attach_object_to_arm(tube.id, arm)
    move_arm(arm, anchor_preapproach(grasp, default_standoff=standoff), r)
    move_arm(arm, [grasp["xyz"][0], grasp["xyz"][1], safe_z], r)

    # TRANSIT (lateral at safe height) + PLACE at the exact target pose
    site = load_object_anchor(target.id, "site")
    move_arm(arm, [site["xyz"][0], site["xyz"][1], safe_z], r)     # FR4
    move_arm(arm, anchor_preapproach(site, default_standoff=standoff), r)
    assert _upright_ok(r, r)                                       # SI1/SI2
    move_arm(arm, site["xyz"], r)                                  # keep tube upright (r)
    set_gripper(arm, grasp["width"] + 0.02)
    detach_object_from_arm(tube.id)
    snap_object_anchor_to_world_pose(tube.id, "grasp_vertical", site["xyz"], site["wxyz"])  # FR2
    move_arm(arm, anchor_preapproach(site, default_standoff=standoff), r)
    move_arm(arm, [site["xyz"][0], site["xyz"][1], safe_z], r)
```

## Getting started (CLI, sim-first)

```bash
zeon auth login
zeon new my-lab           # or: zeon project ...
# author skills/ + workflows/ + worlds/ ; pull labware objects from the mesh database
zeon sync                 # commit + push to the cloud
# run in the cloud simulator from the main app, then on real hardware (same project)
```

Our uncap→aspirate workflow becomes a ZEON **workflow** chaining skills
(`safe_pick_place`, `uncap`, `present_to_pipette`, `aspirate`), each verified against the
world model — validated in sim, then run on the two xArms + Opentrons.
