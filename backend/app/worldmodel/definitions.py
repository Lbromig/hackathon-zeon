"""Static geometry catalog — builds the initial twin skeleton.

Offsets/dimensions are PLACEHOLDERS (metres) to be replaced with taught poses and
real CAD/labware values during calibration. Grids are generated from these defs so we
don't detect every well/tip individually.
"""
from __future__ import annotations

from .entities import Entity, EntityKind, WorldModel, from_xyz_rpy, identity

# --- placeholder geometry (metres) ---
TIP_BOX_COLS, TIP_BOX_ROWS, TIP_PITCH = 12, 8, 0.009      # 96 tips, 9mm pitch
RACK_COLS, RACK_ROWS, WELL_PITCH = 6, 4, 0.018            # 24-tube rack
TUBE_DIMS = {"diameter": 0.010, "height": 0.043}
CAP_DIMS = {"diameter": 0.010, "height": 0.008}


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
    # on-arm camera rides the right arm; external camera is world-fixed
    wm.add(Entity("on_arm_cam", EntityKind.CAMERA, "On-arm camera", "right_tcp", static=False))
    wm.add(Entity("external_cam", EntityKind.CAMERA, "External camera", "world"))

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


def add_tube_with_cap(wm: WorldModel, well: str, tube_id: str) -> None:
    """A capped tube seated in a well; cap is an independent child of the tube."""
    wm.add(Entity(tube_id, EntityKind.TUBE, tube_id, well, static=False, dims=TUBE_DIMS,
                  state={"capped": True, "held_by": None, "has_liquid": True}))
    wm.add(Entity(f"{tube_id}_cap", EntityKind.CAP, f"{tube_id} cap", tube_id, static=False,
                  local=from_xyz_rpy(z=TUBE_DIMS["height"]), dims=CAP_DIMS,
                  state={"on": True, "held_by": None}))
    wm.get(well).state["occupied"] = True
