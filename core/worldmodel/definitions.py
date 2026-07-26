"""Static geometry catalog — builds the initial twin skeleton.

Offsets/dimensions are PLACEHOLDERS (metres) to be replaced with taught poses and
real CAD/labware values during calibration. Grids are generated from these defs so we
don't detect every well/tip individually.
"""
from __future__ import annotations

from .entities import Entity, EntityKind, WorldModel, from_xyz_rpy, identity
from .meshes import dims_m

# --- labware geometry (metres) ---
TIP_BOX_COLS, TIP_BOX_ROWS, TIP_PITCH = 12, 8, 0.009      # 96 tips, 9mm pitch
# NOTE: WELL_PITCH must exceed the tube diameter of the chosen family
# (50 mL tube is Ø28 mm -> needs >~0.030 m pitch). Real rack CAD still pending
# (see docs/WORLD_MODEL_REQUIREMENTS.md open questions); default sized for 50 mL.
RACK_COLS, RACK_ROWS, WELL_PITCH = 6, 4, 0.030

# Tube/cap dimensions come from the real CAD meshes (assets/cad/tubes/), keyed by family.
TUBE_FAMILIES = {
    "50ml": {"tube": "tube_50ml_base", "cap": "tube_50ml_cap"},
    "15ml": {"tube": "tube_15ml_base", "cap": "tube_15ml_cap"},
}
DEFAULT_FAMILY = "50ml"                                   # primary demo tube

TUBE_DIMS = dims_m(TUBE_FAMILIES[DEFAULT_FAMILY]["tube"])  # metres, from mesh bbox
CAP_DIMS = dims_m(TUBE_FAMILIES[DEFAULT_FAMILY]["cap"])


def build_skeleton(*, arm_ids=("left", "right"), n_deck_slots=11) -> WorldModel:
    """Create the static frame tree. Poses are refined by calibration."""
    wm = WorldModel()
    wm.add(Entity("calib_ruler", EntityKind.CALIB_RULER, "3D-printed metric ruler", "world"))
    wm.add(Entity("surface", EntityKind.SURFACE, "Work surface", "world"))

    # arms: base -> tcp -> tool + on-arm camera
    for aid in arm_ids:
        wm.add(Entity(f"{aid}_base", EntityKind.ARM_BASE, f"{aid} xArm base", "world"))
        wm.add(Entity(f"{aid}_tcp", EntityKind.TCP, f"{aid} TCP", f"{aid}_base", static=False))
        wm.add(Entity(f"{aid}_tool", EntityKind.TOOL, f"{aid} gripper", f"{aid}_tcp", static=False))
    # four-camera rig, all resolved into the shared world frame:
    #  - gripper_cam rides the RIGHT arm (hand-eye) -> close-up manipulation / grasp
    #  - gripper_left_cam rides the LEFT arm, same role on the other side
    #  - overview_cam is world-fixed, sees both cells -> global twin + main UI feed
    #  - handover_cam is world-fixed on the arm->OT handover zone -> present / aspirate check
    # The two eye-in-hand cameras parent to their own TCP, so each moves with its arm and
    # its detections back-project through that arm's pose rather than a shared one.
    wm.add(Entity("gripper_cam", EntityKind.CAMERA, "Gripper camera (right arm)", "right_tcp", static=False))
    wm.add(Entity("gripper_left_cam", EntityKind.CAMERA, "Gripper camera (left arm)", "left_tcp", static=False))
    wm.add(Entity("overview_cam", EntityKind.CAMERA, "Overview camera", "world"))
    wm.add(Entity("handover_cam", EntityKind.CAMERA, "Handover camera", "world"))

    # Opentrons: base -> deck -> slots + gantry -> pipette channel
    wm.add(Entity("ot_base", EntityKind.OT_BASE, "Opentrons base", "world"))
    wm.add(Entity("deck", EntityKind.DECK, "OT deck", "ot_base"))
    for i in range(1, n_deck_slots + 1):
        wm.add(Entity(f"slot_{i}", EntityKind.DECK_SLOT, f"Deck slot {i}", "deck",
                      state={"occupied": False}))
    wm.add(Entity("gantry", EntityKind.GANTRY, "OT gantry", "deck", static=False))
    wm.add(Entity("nozzle", EntityKind.PIPETTE_CHANNEL, "Pipette channel", "gantry",
                  static=False, state={"has_tip": False, "aspirated_volume_ul": 0.0}))
    wm.add(Entity("dropzone", EntityKind.DROPZONE, "Cap dropzone", "deck",
                  state={"occupied": False}))
    return wm


def add_tip_box(wm: WorldModel, slot: str, box_id: str = "tipbox_1") -> None:
    wm.add(Entity(box_id, EntityKind.TIP_BOX, "Tip box", slot, state={"present": True}))
    for r in range(TIP_BOX_ROWS):
        for c in range(TIP_BOX_COLS):
            sid = f"{box_id}_{chr(65+r)}{c+1}"
            wm.add(Entity(sid, EntityKind.TIP_SITE, sid, box_id,
                          local=from_xyz_rpy(x=c * TIP_PITCH, y=r * TIP_PITCH),
                          state={"tip_present": True}))


def add_tube_rack(wm: WorldModel, slot: str, rack_id: str = "rack_1") -> None:
    wm.add(Entity(rack_id, EntityKind.TUBE_RACK, "Tube rack", slot, state={"present": True}))
    for r in range(RACK_ROWS):
        for c in range(RACK_COLS):
            wid = f"{rack_id}_{chr(65+r)}{c+1}"
            wm.add(Entity(wid, EntityKind.WELL, wid, rack_id,
                          local=from_xyz_rpy(x=c * WELL_PITCH, y=r * WELL_PITCH),
                          state={"occupied": False}))


def add_tube_with_cap(wm: WorldModel, well: str, tube_id: str,
                      family: str = DEFAULT_FAMILY) -> None:
    """A capped tube seated in a well; cap is an independent child of the tube.

    `family` selects the CAD family ("50ml" default, "15ml"); dims + mesh keys are
    taken from the real meshes so the twin, FoundationPose and render-compare agree.
    """
    fam = TUBE_FAMILIES[family]
    tube_mesh, cap_mesh = fam["tube"], fam["cap"]
    tube_dims, cap_dims = dims_m(tube_mesh), dims_m(cap_mesh)
    wm.add(Entity(tube_id, EntityKind.TUBE, tube_id, well, static=False,
                  dims=tube_dims, mesh=tube_mesh,
                  state={"capped": True, "held_by": None, "has_liquid": True, "family": family}))
    wm.add(Entity(f"{tube_id}_cap", EntityKind.CAP, f"{tube_id} cap", tube_id, static=False,
                  local=from_xyz_rpy(z=tube_dims["height"]), dims=cap_dims, mesh=cap_mesh,
                  state={"on": True, "held_by": None}))
    wm.get(well).state["occupied"] = True
