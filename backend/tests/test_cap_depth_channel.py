"""The depth channel inside CapRemovedAgent.

These cover how the channel composes with the existing torque and vision
channels, not the measurement arithmetic itself, which test_depth_height.py
already pins. No camera required.

The property that matters most: an unreadable depth region must not be able to
veto the other two channels. Contributing a zero would do exactly that, because
the channels are averaged, so an inconclusive depth read has to abstain instead.
"""
from __future__ import annotations

import numpy as np
import pytest

from core.verification.agents import PASS_THRESHOLD, CapRemovedAgent, Evidence
from core.verification.depth_height import measure_region

D405_SCALE = 1e-4
ROI = (10, 10, 60, 60)
STANDOFF = 0.25


def depth_frame(distance_m: float, *, dropout: float = 0.0, seed: int = 0):
    rng = np.random.default_rng(seed)
    metres = rng.normal(distance_m, 0.0025, size=(80, 80))
    counts = np.clip(metres / D405_SCALE, 0, 65535).astype("uint16")
    if dropout:
        counts[rng.random((80, 80)) < dropout] = 0
    return counts


@pytest.fixture
def reference():
    return measure_region(depth_frame(STANDOFF, seed=1), D405_SCALE, ROI)


def evidence_with(frame, reference) -> Evidence:
    return Evidence(
        frames={"depth": frame},
        expected={
            "depth_scale": D405_SCALE,
            "cap_roi": ROI,
            "cap_reference": reference,
        },
    )


def test_depth_alone_can_carry_the_verdict(reference):
    """15 mm of recession, no torque and no marker available."""
    ev = evidence_with(depth_frame(STANDOFF + 0.015, seed=2), reference)
    result = CapRemovedAgent().verify(ev)
    assert result.data["depth_verdict"] == "cap_off"
    assert result.data["depth_delta_mm"] == pytest.approx(15.0, abs=1.0)
    assert set(result.data["channels"]) == {"depth"}


def test_cap_still_on_scores_zero_not_absent(reference):
    """A confident CAP_ON must actively count against, not abstain."""
    ev = evidence_with(depth_frame(STANDOFF, seed=3), reference)
    result = CapRemovedAgent().verify(ev)
    assert result.data["depth_verdict"] == "cap_on"
    assert result.data["channels"]["depth"] == 0.0
    assert result.ok is False


def test_inconclusive_depth_abstains_rather_than_vetoing(reference):
    """The load-bearing property.

    A mostly-empty depth region is UNKNOWN. It must drop out of the fusion
    entirely, because contributing a 0.0 would halve a two-channel average and
    let an unreadable region overrule channels that did observe something.
    """
    ev = evidence_with(depth_frame(STANDOFF + 0.015, dropout=0.95, seed=4), reference)
    result = CapRemovedAgent().verify(ev)
    assert "depth" not in result.data.get("channels", {})
    assert result.data["depth_verdict"] == "unknown"
    assert "inconclusive" in result.detail


def test_missing_reference_is_silent_not_fatal():
    """No reference taken is the normal case before setup. Stay quiet."""
    ev = Evidence(frames={"depth": depth_frame(STANDOFF)})
    result = CapRemovedAgent().verify(ev)
    # Falls through to "no usable evidence" because nothing else was supplied,
    # but the depth channel is the reason named, not a crash.
    assert result.ok is False
    assert "no depth reference" in result.detail


def test_a_wrong_type_reference_does_not_raise(reference):
    ev = evidence_with(depth_frame(STANDOFF + 0.015), {"not": "a HeightStat"})
    result = CapRemovedAgent().verify(ev)
    assert result.ok is False
    assert "HeightStat" in result.detail


def test_ambiguous_movement_does_not_pass(reference):
    """8 mm matches neither cap-on nor cap-off, so it must not vote yes."""
    ev = evidence_with(depth_frame(STANDOFF + 0.008, seed=5), reference)
    result = CapRemovedAgent().verify(ev)
    assert result.data["depth_verdict"] == "unknown"
    assert result.ok is False
    assert result.confidence < PASS_THRESHOLD
