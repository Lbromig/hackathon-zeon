"""The offset solve: the number that terminates the loop, and its refusals.

The distinction these tests exist to protect is `None` versus `0.0` in
`residual_offset_mm`. "I cannot observe this axis" and "this axis is aligned" are opposite
statements, and a servo loop that confuses them converges on an axis it never measured. The
bench makes this concrete: the handover camera looks along the OT's Y axis, so Y is genuinely
unobservable from that view at a measured 0.06 px/mm against X's 2.07.
"""
from __future__ import annotations

import math

import pytest

from core.perception import offset as off


def tip(cam="handover_cam", px=(100.0, 100.0), t=None, method="tag_anchored"):
    return off.Feature(camera=cam, target="tip", point_px=px, t_cam=t, score=0.95,
                       method=method)


def tube(cam="handover_cam", px=(100.0, 100.0), t=None, method="tag_anchored"):
    return off.Feature(camera=cam, target="tube", point_px=px, t_cam=t, score=0.95,
                       method=method)


BENCH_GAINS = {"handover_cam": {"x": -2.07, "y": 0.06}}


# --- O1: the metric path -------------------------------------------------------------

def test_two_tag_poses_solve_by_vector_subtraction():
    """The primary path is a subtraction, so it cannot be mis-scaled by a stale gain."""
    s = off.solve([("handover_cam",
                    tip(t=(0.100, 0.200, 0.500)),
                    tube(t=(0.105, 0.210, 0.520)))])
    assert s.method == "tag_3d"
    assert s.residual_offset_mm["x"] == pytest.approx(5.0)
    assert s.residual_offset_mm["y"] == pytest.approx(10.0)
    assert s.residual_offset_mm["z"] == pytest.approx(20.0)
    assert s.magnitude_mm == pytest.approx(math.sqrt(25 + 100 + 400), abs=1e-3)
    assert s.observed_axes == ["x", "y", "z"], "3D gives every axis"


def test_the_metric_path_wins_over_a_pixel_only_view():
    """One 3D view beats fusing degraded ones (D15): better information, not more of it."""
    s = off.solve([
        ("gripper_left_cam", tip(cam="gripper_left_cam"), tube(cam="gripper_left_cam")),
        ("handover_cam", tip(t=(0.0, 0.0, 0.5)), tube(t=(0.002, 0.0, 0.5))),
    ], gains=BENCH_GAINS)
    assert s.method == "tag_3d"
    assert s.residual_offset_mm["x"] == pytest.approx(2.0)


def test_view_disagreement_is_reported_but_never_zeroes_the_result():
    """Advisory only (Q4): views that never agree must not keep a converged loop running."""
    s = off.solve([
        ("a", off.Feature("a", "tip", (0, 0), t_cam=(0.0, 0.0, 0.5)),
         off.Feature("a", "tube", (0, 0), t_cam=(0.010, 0.0, 0.5))),
        ("b", off.Feature("b", "tip", (0, 0), t_cam=(0.0, 0.0, 0.5)),
         off.Feature("b", "tube", (0.0, 0.0), t_cam=(0.014, 0.0, 0.5))),
    ])
    assert s.method == "tag_3d"
    assert s.residual_offset_mm["x"] == pytest.approx(12.0), "the mean, not a winner"
    assert s.view_disagreement_mm == pytest.approx(4.0)
    assert s.sigma_mm["x"] > off.MIN_SIGMA_MM, "disagreement must widen the uncertainty"


# --- O3: the degraded pixel path -----------------------------------------------------

def test_pixel_path_divides_by_the_measured_gain():
    """20 px of error at -2.07 px/mm is -9.66 mm, not 20 of anything."""
    s = off.solve([("handover_cam", tip(px=(100.0, 100.0)), tube(px=(120.0, 100.0)))],
                  gains=BENCH_GAINS)
    assert s.method == "axis_decoupled_jacobian"
    assert s.residual_offset_mm["x"] == pytest.approx(20.0 / -2.07, abs=1e-3)


def test_an_unobservable_axis_is_none_not_zero():
    """The bench's Y axis: 0.06 px/mm. Reporting 0.0 would read as 'already aligned'."""
    s = off.solve([("handover_cam", tip(px=(100.0, 100.0)), tube(px=(120.0, 130.0)))],
                  gains=BENCH_GAINS)
    assert s.residual_offset_mm["y"] is None
    assert s.residual_offset_mm["z"] is None, "no z gain configured for this view"
    assert s.observed_axes == ["x"]
    assert "y" not in s.sigma_mm, "no sigma at all: unknown, not merely imprecise"


def test_magnitude_covers_only_the_observed_axes():
    s = off.solve([("handover_cam", tip(px=(100.0, 100.0)), tube(px=(120.0, 900.0)))],
                  gains=BENCH_GAINS)
    assert s.magnitude_mm == pytest.approx(abs(20.0 / -2.07), abs=1e-3), (
        "the huge v error is on an axis nobody can see; it must not inflate the magnitude")


def test_a_weak_gain_gives_a_large_sigma():
    """O8: uncertainty degrades when an axis is poorly observed, per R-VIS-4."""
    strong = off.solve([("c", tip(cam="c", px=(0, 0)), tube(cam="c", px=(10, 0)))],
                       gains={"c": {"x": -8.0}})
    weak = off.solve([("c", tip(cam="c", px=(0, 0)), tube(cam="c", px=(10, 0)))],
                     gains={"c": {"x": -0.8}})
    assert weak.sigma_mm["x"] > strong.sigma_mm["x"] * 5


def test_no_gain_for_any_axis_is_a_refusal_not_a_zero_offset():
    s = off.solve([("unknown_cam", tip(cam="unknown_cam"), tube(cam="unknown_cam"))], gains={})
    assert s.method == "refused"
    assert s.refusal == "low_observability"
    assert all(v is None for v in s.residual_offset_mm.values())


# --- refusals ------------------------------------------------------------------------

def test_a_missing_detection_refuses():
    """The occluded-tube case. Absence must not become a solve against half the data."""
    s = off.solve([("handover_cam", tip(), None)], gains=BENCH_GAINS)
    assert s.method == "refused"
    assert s.refusal == "missing_detection"


def test_no_views_refuses():
    s = off.solve([])
    assert s.method == "refused" and s.refusal == "no_views"


def test_a_refusal_reports_no_sigma_at_all():
    """A refusal must not be readable as a confident zero, nor as a measured uncertainty.

    Also a wire-schema constraint: `OffsetOutputs.sigma_mm` rejects non-finite floats, so an
    `inf` sentinel would fail validation at the boundary rather than reaching the UI.
    """
    s = off.solve([("handover_cam", tip(), None)])
    assert s.sigma_mm == {}
    assert all(v is None for v in s.residual_offset_mm.values())


# --- O6: online gain adaptation ------------------------------------------------------

def test_gain_update_measures_what_a_move_actually_produced():
    gains: dict = {}
    g = off.update_gain(gains, "handover_cam", "x", commanded_mm=5.0, pixel_delta=(-10.3, 0.3))
    assert g == pytest.approx(-2.06)
    assert gains["handover_cam"]["x"] == pytest.approx(-2.06)


def test_a_move_that_produced_no_pixel_change_records_zero_observability():
    """Commanded 5 mm, image did not move: that is evidence of non-observability."""
    gains: dict = {}
    g = off.update_gain(gains, "handover_cam", "y", commanded_mm=5.0, pixel_delta=(-0.1, -0.3))
    assert g == 0.0
    assert gains["handover_cam"]["y"] == 0.0
    # And the solve must then refuse that axis rather than divide by almost nothing.
    s = off.solve([("handover_cam", tip(px=(0, 0)), tube(px=(0, 40)))], gains=gains)
    assert s.residual_offset_mm["y"] is None


def test_a_move_too_small_to_measure_returns_none():
    gains: dict = {"c": {"x": -2.0}}
    assert off.update_gain(gains, "c", "x", commanded_mm=0.05, pixel_delta=(0.1, 0.0)) is None
    assert gains["c"]["x"] == -2.0, "the old gain must survive an unusable probe"
