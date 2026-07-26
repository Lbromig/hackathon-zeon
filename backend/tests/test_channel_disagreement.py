"""Contradicting channels must not be averaged into a pass.

The averaging rule in ``_fuse`` is only sound while the channels roughly concur.
When they do not, the mean describes neither of them, and it can carry a verdict
that every individual channel would have refused.

The specific hole these tests close: depth is the only channel that measures the
tube rather than a proxy for it, and a confident CAP_ON contributes 0.0. Two
proxies agreeing at 0.9 then average to exactly 0.600, which clears
PASS_THRESHOLD, so the agent reported the cap removed while the channel that
actually looked at the tube said it was still on.

No camera required. The vision channel is stubbed at ``_marker_centre``, which is
the same seam the module already uses to keep ``core/`` importable without cv2.
"""
from __future__ import annotations

import numpy as np
import pytest

from core.verification import agents as agents_mod
from core.verification.agents import (
    DISAGREEMENT_SPREAD,
    PASS_THRESHOLD,
    CapRemovedAgent,
    Evidence,
    _conflict,
)
from core.verification.depth_height import measure_region

D405_SCALE = 1e-4
ROI = (10, 10, 60, 60)
STANDOFF = 0.25
ARM = "right"
CAM = "overview_cam"


def depth_frame(distance_m: float, *, seed: int = 0):
    rng = np.random.default_rng(seed)
    metres = rng.normal(distance_m, 0.0025, size=(80, 80))
    return np.clip(metres / D405_SCALE, 0, 65535).astype("uint16")


@pytest.fixture
def reference():
    return measure_region(depth_frame(STANDOFF, seed=1), D405_SCALE, ROI)


def _effort(torque_nm: float) -> dict:
    return {ARM: {"effort": {"joints_torque": [0.0, 0.0, 0.0, 0.0, 0.0, torque_nm]}}}


def stub_marker_travel(monkeypatch, *, travel_px: float) -> None:
    """Make the vision channel report a given cap-marker displacement.

    ``_marker_centre`` is called for the before frame first and the after frame
    second, so a call counter is enough to place the marker in two spots.
    """
    calls = {"n": 0}

    def fake(frame, marker_id):
        calls["n"] += 1
        return (100.0, 100.0) if calls["n"] == 1 else (100.0 + travel_px, 100.0)

    monkeypatch.setattr(agents_mod, "_marker_centre", fake)


def evidence(reference, *, depth_distance_m: float, peak_nm: float, final_nm: float) -> Evidence:
    frame = np.zeros((1000, 1000, 3), dtype="uint8")
    return Evidence(
        frames={"depth": depth_frame(depth_distance_m, seed=2), CAM: frame},
        before_frames={CAM: frame.copy()},
        telemetry=_effort(final_nm),
        during=[_effort(peak_nm)],
        expected={
            "turning_arm": ARM,
            "overview_camera": CAM,
            "depth_scale": D405_SCALE,
            "cap_roi": ROI,
            "cap_reference": reference,
        },
    )


def test_confident_cap_on_is_not_outvoted_by_two_proxies(monkeypatch, reference):
    """The regression. Torque and vision both say removed, depth says still on."""
    stub_marker_travel(monkeypatch, travel_px=300.0)
    ev = evidence(reference, depth_distance_m=STANDOFF, peak_nm=2.0, final_nm=0.1)

    result = CapRemovedAgent().verify(ev)

    assert result.data["depth_verdict"] == "cap_on"
    assert result.data["channels"]["depth"] == 0.0
    assert result.data["channels"]["torque"] > 0.8
    assert result.data["channels"]["vision"] > 0.8
    assert result.ok is False
    assert result.confidence == 0.0
    assert "contradict" in result.detail
    assert result.data["disagreement"]["low"] == "depth"


def test_the_average_alone_would_have_passed(monkeypatch, reference):
    """Proves the hole was real rather than theoretical.

    The recorded fusion value is what the old code would have returned, and it
    has to clear the pass threshold for this regression to be worth guarding.
    """
    stub_marker_travel(monkeypatch, travel_px=300.0)
    ev = evidence(reference, depth_distance_m=STANDOFF, peak_nm=2.0, final_nm=0.1)

    result = CapRemovedAgent().verify(ev)

    assert result.data["disagreement"]["would_have_fused_to"] >= PASS_THRESHOLD


def test_agreeing_channels_still_fuse_and_pass(monkeypatch, reference):
    """The guard must not swallow the normal case: everything says removed."""
    stub_marker_travel(monkeypatch, travel_px=300.0)
    ev = evidence(
        reference, depth_distance_m=STANDOFF + 0.015, peak_nm=2.0, final_nm=0.1
    )

    result = CapRemovedAgent().verify(ev)

    assert result.data["depth_verdict"] == "cap_off"
    assert "disagreement" not in result.data
    assert result.ok is True
    assert result.confidence >= PASS_THRESHOLD


def test_one_channel_alone_is_never_a_contradiction(reference):
    """Depth by itself scores 0.0 and fails on confidence, not on conflict."""
    ev = Evidence(
        frames={"depth": depth_frame(STANDOFF, seed=3)},
        expected={
            "depth_scale": D405_SCALE,
            "cap_roi": ROI,
            "cap_reference": reference,
        },
    )

    result = CapRemovedAgent().verify(ev)

    assert set(result.data["channels"]) == {"depth"}
    assert "disagreement" not in result.data
    assert result.ok is False


def test_conflict_helper_boundaries():
    """Exactly at the spread is a conflict; just under it is not."""
    assert _conflict({"a": 0.9}) is None
    assert _conflict({}) is None
    assert _conflict({"a": 0.9, "b": 0.9 - DISAGREEMENT_SPREAD}) == ("a", "b")
    assert _conflict({"a": 0.9, "b": 0.9 - DISAGREEMENT_SPREAD + 0.01}) is None


def test_conflict_names_the_extreme_pair_of_three():
    got = _conflict({"torque": 0.95, "vision": 0.70, "depth": 0.0})
    assert got == ("torque", "depth")
