"""Tests for the depth height check.

These run with no camera. Synthetic depth frames are built at a known standoff
with a known step, so the measurement can be checked against a number we chose
rather than against whatever the bench happened to be doing. If the arithmetic
is wrong here it is wrong on hardware too, and this is the cheaper place to find
out.

Noise is modelled at the D405's published close-range figure (RMS spatial noise
<= 1% of range, so 2.5 mm at 250 mm) so the confidence gating is exercised
against realistic data rather than clean synthetic planes.
"""
from __future__ import annotations

import numpy as np
import pytest

from core.verification.depth_height import (
    MIN_VALID_FRACTION,
    CapCheck,
    Verdict,
    compare,
    measure_region,
)

# The D405 scale. Deliberately not 1e-3: using the wrong one here would make
# these tests pass while hardware reads ten times off.
D405_SCALE = 1e-4

ROI = (10, 10, 60, 60)
STANDOFF_M = 0.25
SIGMA_M = 0.0025          # 1% of 250 mm, per the datasheet


def frame(distance_m: float, *, noise: float = SIGMA_M, dropout: float = 0.0,
          seed: int = 0, shape: tuple[int, int] = (80, 80)) -> np.ndarray:
    """A uint16 depth frame of a flat surface at `distance_m`."""
    rng = np.random.default_rng(seed)
    metres = rng.normal(distance_m, noise, size=shape)
    counts = np.clip(metres / D405_SCALE, 0, 65535).astype("uint16")
    if dropout > 0:
        holes = rng.random(shape) < dropout
        counts[holes] = 0      # librealsense writes 0 for "no reading"
    return counts


def test_measure_recovers_the_true_distance():
    stat = measure_region(frame(STANDOFF_M), D405_SCALE, ROI)
    assert stat.usable
    # Within a third of a millimetre of truth, from a median over 3600 px.
    assert stat.median_m == pytest.approx(STANDOFF_M, abs=0.0003)
    assert stat.valid_px == 60 * 60


def test_wrong_scale_would_be_caught_by_the_magnitude():
    """A 1e-3 scale reads ten times too far. This is the trap, pinned down."""
    stat = measure_region(frame(STANDOFF_M), 1e-3, ROI)
    assert stat.median_m == pytest.approx(2.5, abs=0.01)


def test_zero_or_negative_scale_is_rejected():
    for bad in (0.0, -1e-4):
        with pytest.raises(ValueError, match="get_depth_scale"):
            measure_region(frame(STANDOFF_M), bad, ROI)


def test_cap_off_detected_at_the_expected_delta():
    """15 mm of recession, the low end of the real cap range."""
    ref = measure_region(frame(STANDOFF_M, seed=1), D405_SCALE, ROI)
    obs = measure_region(frame(STANDOFF_M + 0.015, seed=2), D405_SCALE, ROI)
    result = compare(ref, obs)
    assert result.verdict is Verdict.CAP_OFF
    assert result.ok
    assert result.delta_mm == pytest.approx(15.0, abs=1.0)
    assert result.confidence > 0.5


def test_cap_still_on_when_nothing_moved():
    ref = measure_region(frame(STANDOFF_M, seed=3), D405_SCALE, ROI)
    obs = measure_region(frame(STANDOFF_M, seed=4), D405_SCALE, ROI)
    result = compare(ref, obs)
    assert result.verdict is Verdict.CAP_ON
    assert not result.ok


def test_ambiguous_movement_is_unknown_not_a_guess():
    """8 mm is too much for 'unchanged' and too little for a cap.

    This is the case that matters: the tube was nudged, or the region drifted
    off the mouth. Calling it either way would be a fabrication.
    """
    ref = measure_region(frame(STANDOFF_M, seed=5), D405_SCALE, ROI)
    obs = measure_region(frame(STANDOFF_M + 0.008, seed=6), D405_SCALE, ROI)
    result = compare(ref, obs)
    assert result.verdict is Verdict.UNKNOWN
    assert not result.ok
    assert result.confidence == 0.0


def test_too_many_dropouts_refuses_to_answer():
    """A mostly-empty region must not produce a verdict."""
    ref = measure_region(frame(STANDOFF_M, seed=7), D405_SCALE, ROI)
    obs = measure_region(
        frame(STANDOFF_M + 0.015, dropout=0.95, seed=8), D405_SCALE, ROI
    )
    assert not obs.usable
    assert obs.valid_fraction < MIN_VALID_FRACTION
    result = compare(ref, obs)
    assert result.verdict is Verdict.UNKNOWN
    assert "unusable" in result.detail


def test_moderate_dropouts_still_measure():
    """Depth is holey on shiny caps; 30% loss must not stop the check."""
    obs = measure_region(
        frame(STANDOFF_M, dropout=0.30, seed=9), D405_SCALE, ROI
    )
    assert obs.usable
    assert obs.median_m == pytest.approx(STANDOFF_M, abs=0.0005)


def test_empty_region_is_not_usable():
    stat = measure_region(np.zeros((80, 80), dtype="uint16"), D405_SCALE, ROI)
    assert not stat.usable
    assert stat.valid_px == 0
    assert compare(stat, stat).verdict is Verdict.UNKNOWN


def test_a_15mm_step_clears_the_noise_by_the_predicted_margin():
    """Sanity-check the physics claim the whole approach rests on.

    At 250 mm the D405's per-pixel sigma is about 2.5 mm, so a 15 mm step is
    roughly 6 sigma per pixel. If this ever fails, the approach needs revisiting
    rather than the threshold being loosened.
    """
    ref = measure_region(frame(STANDOFF_M, seed=10), D405_SCALE, ROI)
    obs = measure_region(frame(STANDOFF_M + 0.015, seed=11), D405_SCALE, ROI)
    noise_mm = max(ref.spread_mm, obs.spread_mm)
    assert abs(obs.median_m - ref.median_m) * 1000 / noise_mm > 3.0


def test_verdict_ok_is_only_true_for_cap_off():
    """UNKNOWN must never read as success anywhere downstream."""
    assert CapCheck(Verdict.CAP_OFF, 0.9).ok
    assert not CapCheck(Verdict.CAP_ON, 0.9).ok
    assert not CapCheck(Verdict.UNKNOWN, 0.0).ok
