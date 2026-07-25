"""Fiducial marker map: which tag is glued to which entity, and the fixed
transform from the marker to that entity's origin.

Physical stock is **AprilTag tag36h11 @ 20 mm**, IDs 180-224 (confirmed from the lab
photos; detection lives in core/perception/fiducials.py). Assign entities to real stock
IDs below — the placeholder ids (10, 11, ...) are NOT in the printed set. Sizes in metres.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..worldmodel.entities import Transform, identity


@dataclass
class MarkerSpec:
    marker_id: int
    entity_id: str
    size_m: float
    T_marker_to_entity: Transform = field(default_factory=identity)  # marker frame -> entity origin


# The board that defines the world frame + (with the ruler) metric scale.
WORLD_BOARD_IDS: tuple[int, ...] = (0, 1, 2, 3)

# entity <- marker associations. Use real stock ids (tag36h11 180-224); ids below are
# EXAMPLES using the low end of the stock — replace offsets with measured values.
MARKER_MAP: dict[int, MarkerSpec] = {
    180: MarkerSpec(180, "left_base", 0.02),
    181: MarkerSpec(181, "right_base", 0.02),
    182: MarkerSpec(182, "ot_base", 0.02),
    183: MarkerSpec(183, "rack_1", 0.02),
    186: MarkerSpec(186, "tipbox_1", 0.02),
    224: MarkerSpec(224, "tube_1_cap", 0.02),   # the round sticker seen on the cap
    # per-tube / per-cap markers can be added dynamically as consumables are placed
}


def spec_for(marker_id: int) -> MarkerSpec | None:
    return MARKER_MAP.get(marker_id)
