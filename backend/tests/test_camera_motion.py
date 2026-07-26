"""The vision channel must not read a camera move as the cap coming off.

Cap-marker displacement is the whole vision channel, and displacement is
ambiguous: a camera knocked on its mount, or a bench somebody leaned on, moves
the cap marker across the frame exactly as removing the cap does. On a shared
hackathon table that is not a hypothetical.

When a static datum marker is supplied its displacement is subtracted, which is
common-mode rejection: motion shared with a fixed reference is not the cap
moving. When no datum is supplied the channel still reports, but says in its
notes that it is unreferenced, because silently treating an unreferenced reading
as proof is the failure this layer exists to prevent.

No cv2 and no camera. ``_marker_centre`` is stubbed at the same seam the module
already uses to stay importable without OpenCV.
"""
from __future__ import annotations

import numpy as np
import pytest

from core.verification import agents as agents_mod
from core.verification.agents import CapRemovedAgent, Evidence

CAM = "overview_cam"
CAP_ID = CapRemovedAgent.CAP_MARKER_ID
DATUM_ID = 181  # markers.MARKER_MAP: right_base, a fixed part of the rig

# 1000x1000 frames, so the diagonal is about 1414 px. MOVE_LO/MOVE_HI are
# fractions of that diagonal, which is what keeps the channel resolution
# independent.
FRAME_SHAPE = (1000, 1000, 3)


@pytest.fixture
def frames():
    """Two distinct frame objects, so a stub can tell before from after."""
    return np.zeros(FRAME_SHAPE, dtype="uint8"), np.zeros(FRAME_SHAPE, dtype="uint8")


def stub_centres(monkeypatch, before, after, layout: dict) -> None:
    """Place markers explicitly per (frame, marker id).

    ``layout`` maps ("before"|"after", marker_id) to a centre, or to None for a
    marker that is not visible in that frame.
    """
    def fake(frame, marker_id):
        which = "before" if frame is before else "after"
        return layout.get((which, marker_id))

    monkeypatch.setattr(agents_mod, "_marker_centre", fake)


def evidence(before, after, *, datum_id: int | None) -> Evidence:
    expected = {"turning_arm": "right", "overview_camera": CAM}
    if datum_id is not None:
        expected["datum_marker_id"] = datum_id
    return Evidence(
        frames={CAM: after},
        before_frames={CAM: before},
        expected=expected,
    )


def test_a_camera_move_does_not_read_as_the_cap_moving(monkeypatch, frames):
    """Cap and datum translate together, so nothing moved relative to the rig."""
    before, after = frames
    stub_centres(monkeypatch, before, after, {
        ("before", CAP_ID): (100.0, 100.0),
        ("after", CAP_ID): (400.0, 100.0),      # 300 px of apparent travel
        ("before", DATUM_ID): (600.0, 600.0),
        ("after", DATUM_ID): (900.0, 600.0),    # the same 300 px
    })

    result = CapRemovedAgent().verify(evidence(before, after, datum_id=DATUM_ID))

    assert result.data["vision_datum_referenced"] is True
    assert result.data["cap_marker_travel"] == pytest.approx(0.0, abs=1e-9)
    assert result.data["datum_marker_travel"] > 0.15
    assert result.data["channels"]["vision"] == 0.0
    assert result.ok is False


def test_real_cap_movement_still_registers_against_a_static_datum(monkeypatch, frames):
    before, after = frames
    stub_centres(monkeypatch, before, after, {
        ("before", CAP_ID): (100.0, 100.0),
        ("after", CAP_ID): (400.0, 100.0),
        ("before", DATUM_ID): (600.0, 600.0),
        ("after", DATUM_ID): (600.0, 600.0),    # rig held still
    })

    result = CapRemovedAgent().verify(evidence(before, after, datum_id=DATUM_ID))

    assert result.data["vision_datum_referenced"] is True
    assert result.data["datum_marker_travel"] == pytest.approx(0.0, abs=1e-9)
    assert result.data["channels"]["vision"] == 1.0
    assert result.ok is True


def test_only_the_differential_part_counts(monkeypatch, frames):
    """Partial common motion is removed, not all of it and not none of it.

    Cap travels 185 px while the rig travels 100 px, so 85 px is genuinely the
    cap. Against a 1414 px diagonal that is 0.060, which lands mid-ramp between
    MOVE_LO 0.02 and MOVE_HI 0.10 and must therefore score 0.5 rather than
    saturating.
    """
    before, after = frames
    stub_centres(monkeypatch, before, after, {
        ("before", CAP_ID): (100.0, 100.0),
        ("after", CAP_ID): (285.0, 100.0),
        ("before", DATUM_ID): (600.0, 600.0),
        ("after", DATUM_ID): (700.0, 600.0),
    })

    result = CapRemovedAgent().verify(evidence(before, after, datum_id=DATUM_ID))

    assert result.data["cap_marker_travel"] == pytest.approx(0.060, abs=0.002)
    assert result.data["channels"]["vision"] == pytest.approx(0.5, abs=0.03)


def test_no_datum_supplied_is_reported_as_unreferenced(monkeypatch, frames):
    """The channel still works, but it says what it cannot rule out."""
    before, after = frames
    stub_centres(monkeypatch, before, after, {
        ("before", CAP_ID): (100.0, 100.0),
        ("after", CAP_ID): (400.0, 100.0),
    })

    result = CapRemovedAgent().verify(evidence(before, after, datum_id=None))

    assert result.data["vision_datum_referenced"] is False
    assert "datum_marker_travel" not in result.data
    assert "unreferenced" in result.detail
    assert result.data["channels"]["vision"] == 1.0


def test_a_configured_datum_that_is_not_visible_is_flagged(monkeypatch, frames):
    """Asking for a datum and not finding one must not pass silently."""
    before, after = frames
    stub_centres(monkeypatch, before, after, {
        ("before", CAP_ID): (100.0, 100.0),
        ("after", CAP_ID): (400.0, 100.0),
        ("before", DATUM_ID): None,
        ("after", DATUM_ID): None,
    })

    result = CapRemovedAgent().verify(evidence(before, after, datum_id=DATUM_ID))

    assert result.data["vision_datum_referenced"] is False
    assert f"datum marker {DATUM_ID} not tracked" in result.detail
    assert "unreferenced" in result.detail
