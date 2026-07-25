# Digital twin & calibration

We maintain a live **digital twin** of the whole workspace: every relevant object is an
*entity* with a pose in a shared world frame and a small state machine. Verification is
then just querying the twin ("is the cap off the tube?", "is the tube under the nozzle?")
rather than bespoke per-step vision.

Two things feed the twin:
- **Kinematics** (fast, always-on): arm FK and OT gantry position give the poses of moving
  parts every tick.
- **Perception** (slower, corrective): the on-arm camera + ArUco markers + a 3D-printed
  metric ruler locate static instruments and consumables, and correct drift.

---

## 1. Entities to track

Everything below is a first-class entity with a 6-DoF pose (unless noted). "Dynamic" =
pose changes at runtime; "static" = fixed once calibrated.

| # | Entity | Kind | Static/Dyn | Pose obtained from | Parent frame | State tracked |
|---|--------|------|-----------|--------------------|--------------|---------------|
| 1 | World origin | `world` | static | Calibration board (origin + orientation) | — (root) | — |
| 2 | Calibration ruler/board | `calib_ruler` | static | Known 3D-printed geometry (sets **metric scale**) | world | — |
| 3 | Left xArm base | `arm_base` | static | Scan + ArUco on base / known mount | world | — |
| 4 | Right xArm base | `arm_base` | static | Scan + ArUco on base | world | — |
| 5 | Left TCP | `tcp` | dynamic | Forward kinematics | left base | — |
| 6 | Right TCP | `tcp` | dynamic | Forward kinematics | right base | — |
| 7 | Gripper / cap tool (per arm) | `tool` | dynamic | Fixed tool offset | its TCP | jaw width |
| 8 | On-arm camera | `camera` | dynamic | **Hand-eye** offset from TCP | its TCP | intrinsics |
| 9 | External camera(s) | `camera` | static | Extrinsic calibration to world | world | intrinsics |
| 10 | Opentrons base | `ot_base` | static | Scan + ArUco | world | — |
| 11 | OT deck origin | `deck` | static | Fixed offset from OT base | ot_base | — |
| 12 | Deck slots / carrier sites | `deck_slot` | static | Fixed CAD offsets (grid) | deck | occupied? |
| 13 | Tip box | `tip_box` | semi-static | Seated in a slot (verify by vision) | deck_slot | present? |
| 14 | Tip sites (grid in box) | `tip_site` | static | Fixed grid offsets | tip_box | tip present/empty |
| 15 | Tip | `tip` | consumable | At its site **or** attached to nozzle | tip_site → nozzle | fresh/used |
| 16 | Tube rack / nest | `tube_rack` | semi-static | Seated in slot / on table (ArUco) | deck_slot / world | present? |
| 17 | Wells / tube positions | `well` | static | Fixed grid offsets | tube_rack | occupied/empty |
| 18 | **Tube** | `tube` | dynamic | In a well **or** in a gripper | well → gripper | capped/uncapped, held_by, has_liquid |
| 19 | **Cap** (independent!) | `cap` | dynamic | On tube **or** in gripper **or** in dropzone | tube → gripper → dropzone | on/removed |
| 20 | Cap dropzone | `dropzone` | static | Taught / fixed | deck / world | occupied? |
| 21 | OT gantry / carriage | `gantry` | dynamic | OT kinematics | deck | — |
| 22 | **Pipette channel / nozzle tip** | `pipette_channel` | dynamic | Offset from gantry | gantry | has_tip, aspirated_volume |
| 23 | Table / work surface | `surface` | static | Calibration | world | — |

The three the demo hinges on — **tube, cap, and pipette nozzle** — are tracked as
*independent* entities so we can verify their relationship (cap off tube, nozzle in tube).

---

## 2. Relations — the transform tree

Poses are stored **relative to a parent** and composed up to the world frame. Manipulation
**reparents** entities (a pick changes a tube's parent from `well` to `gripper`; removing a
cap changes its parent from `tube` to `gripper` then `dropzone`). ArUco observations correct
poses; kinematics update the moving parents.

```
world (calibration board defines origin + metric scale)
├─ calib_ruler
├─ left_arm_base ── left_tcp ── {left_tool, on_arm_cam? }
├─ right_arm_base ── right_tcp ── right_tool ── on_arm_cam
├─ external_cam
├─ surface
└─ ot_base ── deck
   ├─ deck_slot[1..N]
   │  ├─ tip_box ── tip_site[grid] ── tip
   │  └─ tube_rack ── well[grid] ── tube ── cap
   ├─ dropzone
   └─ gantry ── pipette_channel ── (tip when attached)
```

Dynamic reparenting during the workflow:

- **Pick tube:** `tube.parent: well → right_tool`
- **Remove cap:** `cap.parent: tube → left_tool → dropzone`
- **Pick up tip:** `tip.parent: tip_site → pipette_channel`
- **Present tube:** tube world-pose driven by `right_tcp`; checked against `pipette_channel` target

See `docs/` mermaid diagrams for the visual transform tree and pipeline.

---

## 3. What's required to map the landscape in 3D

1. **Metric scale** — the 3D-printed ruler of known length resolves monocular scale ambiguity.
2. **World frame** — an ArUco board defines the world origin + orientation everything anchors to.
3. **Camera intrinsics** — per-camera calibration (charuco/checkerboard).
4. **Hand-eye extrinsics** — on-arm camera → TCP transform.
5. **Instrument extrinsics** — each `arm_base` and `ot_base` → world, recovered by orbiting
   the on-arm camera around the instrument (multi-view scan) with ArUco + ruler.
6. **Marker map** — ArUco ID → entity + marker-to-entity-origin offset + marker size.
7. **Known geometry (defs)** — deck slot layout, labware grids (tip box, tube rack), and
   tube/cap/tip dimensions, so grids don't need per-item detection.
8. **Scan procedure + package** — the on-arm camera orbit that produces reconstruction files;
   plugged in behind `calibration/scan.py::ScanAdapter` (see below).
9. **Persistence** — calibration artifacts on the filesystem: intrinsics, extrinsics,
   marker map, per-instrument reconstructions, and the resolved entity poses (`calib/`).
10. **Twin store + update loop** — fuse kinematics (fast) and perception (corrective) into
    entity poses; reparent on manipulation.

---

## 4. Calibration / initialization pipeline

Runs once at startup, before any workflow. Ordered because each step depends on the previous.

1. **Instrument init** — connect + home every driver (arms enable/home, OT home, cameras open).
2. **Camera intrinsics** — calibrate each camera (or load cached).
3. **Hand-eye** — on-arm camera → TCP (or load cached).
4. **World frame** — detect the ArUco board + ruler → fix world origin + metric scale.
5. **Arm↔arm** — both arm bases into the shared world frame.
6. **Locate instruments** — orbit the on-arm camera around the Opentrons (and racks); the
   scan package emits reconstruction files → resolve `ot_base` (and rack) poses in world.
7. **Register static geometry** — instantiate deck slots, tip box, tube rack, wells, tip
   sites from `definitions.py` under their parents.
8. **Detect consumables** — find tubes/caps/tips present; set initial states.
9. **Freeze twin** — persist artifacts to `calib/`; twin is now live for verification.

Artifacts on disk (`calib/`): `intrinsics/*.json`, `hand_eye.json`, `world_frame.json`,
`instruments/<id>/` (scan outputs), `marker_map.json`, `entities.json` (twin snapshot).

Verification then reads the twin: e.g. `cap_removed = cap.parent != tube and dist(cap,tube) > τ`;
`tube_aligned = ‖tube.world_pose − pipette_channel.target‖ < τ`; `tip_attached = tip.parent == nozzle`.
