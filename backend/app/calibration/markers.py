"""ArUco marker map: which fiducial is glued to which entity, and the fixed
transform from the marker to that entity's origin.

Populate as markers are physically attached. Sizes in metres.
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

# entity <- marker associations (placeholders; fill in real ids + offsets)
MARKER_MAP: dict[int, MarkerSpec] = {
    10: MarkerSpec(10, "left_base", 0.03),
    11: MarkerSpec(11, "right_base", 0.03),
    20: MarkerSpec(20, "ot_base", 0.04),
    30: MarkerSpec(30, "rack_1", 0.02),
    31: MarkerSpec(31, "tipbox_1", 0.02),
    # per-tube / per-cap markers can be added dynamically as consumables are placed
}


def spec_for(marker_id: int) -> MarkerSpec | None:
    return MARKER_MAP.get(marker_id)
