"""Tag size registry: how big, in metres, the tag with a given id physically is.

Salvaged from `core/calibration/markers.py` when the calibration pipeline and the digital
twin were deleted. **Every world-model field is gone on purpose** — there is no
`entity_id`, no `T_marker_to_entity`, no world-board anchor. A tag no longer names a twin
entity; it is a pose source for the offset solve (R-VIS-12) and a label in the camera
overlay, and that needs exactly one fact per id: its edge length.

Why the size matters and why it is per-id: `solvePnP` scales the whole pose by the object
model you hand it. Get the size wrong by 2x and every distance the detector reports is
wrong by 2x, silently and with a perfectly plausible-looking pose. So a size is a measured
bench fact, not a default to be guessed per call site.

Physical stock on this bench is AprilTag `tag36h11` **@ 20 mm**. The ids below are the ones
actually *observed* across `temp/captures/` (GAP_ANALYSIS §3.1 finding 7) — the previous
map listed 180-186 and 224, of which several were never on the bench and 224 was a
placeholder for a round cap sticker that no frame ever detected. Unknown ids fall back to
`DEFAULT_TAG_SIZE_M` rather than raising: an unregistered tag in view is a detection worth
reporting, it just cannot be trusted for metric range.
"""
from __future__ import annotations

from dataclasses import dataclass

# 20 mm, printed on the sticker stock. Also the fallback for an unregistered id.
DEFAULT_TAG_SIZE_M = 0.020

# The larger flat tags R-VIS-13 calls for (40-50 mm) on the pipette carriage and the
# tube/gripper assembly. Register their ids here with this size once P-2b mounts them —
# the ids are not yet assigned, and inventing them would look like configuration.
# Note the hard constraint from GAP_ANALYSIS §3.1 finding 4: a tag wrapped around the
# cylindrical tube is NOT detected even at ~60 px and clearly legible, because curvature
# breaks the square-quad fit. Flat surfaces only.
SERVO_TAG_SIZE_M = 0.045


@dataclass(frozen=True)
class MarkerSpec:
    marker_id: int
    size_m: float


# Ids measured across every `temp/captures/` startup snapshot on 2026-07-25/26 (P-5).
# 225 is the one on the tube/gripper assembly in the handover frame — the detection that
# makes the fiducial-first offset solve viable (D27, D30). The rest are deck rail, table
# and base markers. Extend this when a tag is added to the bench; it is data, not code.
BENCH_TAG_IDS: tuple[int, ...] = (
    180, 181, 183, 184, 185, 188, 189, 191, 202, 203, 218, 219, 225, 227,
)

MARKER_SIZES_M: dict[int, float] = {mid: DEFAULT_TAG_SIZE_M for mid in BENCH_TAG_IDS}


def spec_for(marker_id: int) -> MarkerSpec | None:
    """The registered spec for an id, or None when the id is not on the bench list."""
    size = MARKER_SIZES_M.get(marker_id)
    return None if size is None else MarkerSpec(marker_id, size)


def size_for(marker_id: int, default: float = DEFAULT_TAG_SIZE_M) -> float:
    """Edge length in metres, falling back to the printed stock size."""
    return MARKER_SIZES_M.get(marker_id, default)
