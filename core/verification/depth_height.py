"""Metric height measurement from a depth frame.

Cap on versus cap off is a height question. The mouth of a capped tube sits
roughly 15 to 20 mm above the mouth of an uncapped one, and that difference is
geometry: it does not depend on lighting, on a printed marker surviving a wet
bench, or on the object not being shiny. That makes it the most robust signal
available for this check, and it is the reason the depth camera is worth the
trouble at all.

The rules this module follows, all of them learned the expensive way:

- The depth scale comes from the device, never from a constant. The D405 uses
  1e-4 m per count where the rest of the D400 series uses 1e-3. A hardcoded
  scale makes every distance ten times wrong, and wrong in a direction that
  still looks plausible.
- Statistics are robust, not mean-based. Depth frames have dropouts, and a
  single zero-filled hole drags a mean far more than it should.
- A measurement with too few valid pixels is not a measurement. It returns
  UNKNOWN. A verifier that guesses when it cannot see is the failure mode this
  whole layer exists to prevent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Fraction of pixels in the region that must carry a real depth reading before
# a measurement is trusted. Depth dropouts cluster on exactly the surfaces that
# matter here (shiny caps, clear plastic), so this is deliberately demanding.
MIN_VALID_FRACTION = 0.35

# Below this many pixels the robust statistics stop meaning anything, whatever
# the fraction says. A 10 mm cap disc at 250 mm on a D405 is roughly 28 px
# across, so several hundred pixels is a realistic floor rather than a generous
# one.
MIN_VALID_PIXELS = 150


class Verdict(str, Enum):
    """What a height comparison concluded.

    UNKNOWN is a first-class outcome, not an error. The caller is expected to
    stop or ask for help rather than treat it as either of the other two.
    """

    CAP_ON = "cap_on"
    CAP_OFF = "cap_off"
    UNKNOWN = "unknown"


@dataclass
class HeightStat:
    """Robust depth statistics over one region of a frame."""

    median_m: float = 0.0
    p25_m: float = 0.0
    p75_m: float = 0.0
    valid_px: int = 0
    total_px: int = 0
    spread_mm: float = 0.0        # interquartile range, the noise estimate

    @property
    def valid_fraction(self) -> float:
        return self.valid_px / self.total_px if self.total_px else 0.0

    @property
    def usable(self) -> bool:
        return (
            self.valid_px >= MIN_VALID_PIXELS
            and self.valid_fraction >= MIN_VALID_FRACTION
        )

    def describe(self) -> str:
        if not self.total_px:
            return "no region sampled"
        return (
            f"median {self.median_m * 1000:.1f} mm, IQR {self.spread_mm:.1f} mm, "
            f"{self.valid_px}/{self.total_px} valid ({self.valid_fraction:.0%})"
        )


@dataclass
class CapCheck:
    """The result of comparing a live measurement against a reference."""

    verdict: Verdict
    confidence: float                       # 0..1
    delta_mm: float = 0.0                   # positive means further from camera
    detail: str = ""
    reference: HeightStat = field(default_factory=HeightStat)
    observed: HeightStat = field(default_factory=HeightStat)

    @property
    def ok(self) -> bool:
        """True only for a confident CAP_OFF. UNKNOWN is never a pass."""
        return self.verdict is Verdict.CAP_OFF


def measure_region(
    depth_raw: Any,
    depth_scale: float,
    roi: tuple[int, int, int, int],
) -> HeightStat:
    """Robust depth statistics over `roi` = (x, y, w, h), in metres.

    `depth_raw` is the camera's uint16 depth image and `depth_scale` its metres
    per count, exactly as `get_depth_scale()` reports it. Passing metres in
    directly is not supported on purpose: it is the step where a wrong scale
    silently enters, so the conversion happens here where it can be reviewed.
    """
    import numpy as np

    if depth_scale <= 0:
        raise ValueError(
            "depth_scale must be positive and must come from the device "
            "(get_depth_scale()). The D405 reports 1e-4, not the 1e-3 used by "
            "the rest of the D400 series."
        )

    x, y, w, h = roi
    patch = np.asarray(depth_raw)[y : y + h, x : x + w]
    total = int(patch.size)
    if total == 0:
        return HeightStat()

    # Zero is librealsense's "no reading here", not a surface at the lens.
    valid = patch[patch > 0].astype("float64") * depth_scale
    if valid.size == 0:
        return HeightStat(total_px=total)

    p25, median, p75 = (float(v) for v in np.percentile(valid, [25, 50, 75]))
    return HeightStat(
        median_m=median,
        p25_m=p25,
        p75_m=p75,
        valid_px=int(valid.size),
        total_px=total,
        spread_mm=(p75 - p25) * 1000.0,
    )


def compare(
    reference: HeightStat,
    observed: HeightStat,
    expected_delta_mm: float = 15.0,
    tolerance_mm: float = 5.0,
) -> CapCheck:
    """Decide cap on or cap off from two height measurements.

    `reference` is the capped tube, measured during setup. `observed` is now.
    Removing the cap uncovers a surface further from the camera, so the observed
    median should grow by about `expected_delta_mm`.

    Confidence is the margin over the measurement's own noise, not a number
    invented to look decisive. When the two readings are separated by little
    more than their own spread, the answer is UNKNOWN.
    """
    if not reference.usable:
        return CapCheck(
            Verdict.UNKNOWN, 0.0,
            detail=f"reference unusable: {reference.describe()}",
            reference=reference, observed=observed,
        )
    if not observed.usable:
        return CapCheck(
            Verdict.UNKNOWN, 0.0,
            detail=f"observed unusable: {observed.describe()}",
            reference=reference, observed=observed,
        )

    delta_mm = (observed.median_m - reference.median_m) * 1000.0

    # Noise floor: combine both spreads. Comparing a delta against a single
    # frame's spread understates the uncertainty of a difference.
    noise_mm = max(1.0, (reference.spread_mm + observed.spread_mm) / 2.0)
    margin = abs(delta_mm) / noise_mm

    if delta_mm >= expected_delta_mm - tolerance_mm and margin >= 2.0:
        return CapCheck(
            Verdict.CAP_OFF, min(1.0, margin / 6.0), delta_mm,
            f"surface receded {delta_mm:.1f} mm, {margin:.1f}x noise",
            reference, observed,
        )

    if abs(delta_mm) <= tolerance_mm:
        return CapCheck(
            Verdict.CAP_ON, min(1.0, 1.0 - abs(delta_mm) / max(tolerance_mm, 1e-6)),
            delta_mm,
            f"surface unchanged within {tolerance_mm:.0f} mm, cap still on",
            reference, observed,
        )

    # Moved, but not by the amount a cap accounts for. Something happened that
    # this check does not model: the tube shifted, the arm nudged it, the region
    # is off target. Saying "cap off" here would be a guess.
    return CapCheck(
        Verdict.UNKNOWN, 0.0, delta_mm,
        f"moved {delta_mm:.1f} mm, which does not match a cap "
        f"({expected_delta_mm:.0f} +/- {tolerance_mm:.0f} mm). "
        "Check the region is on the tube mouth and that nothing was bumped.",
        reference, observed,
    )
