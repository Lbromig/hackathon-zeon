"""Simplifying a hand-guided recording into replayable waypoints.

Simplification is the whole value here: 10 Hz of hand-guiding produces hundreds of
near-identical poses, and replaying those raw would take minutes and stutter at every
one. These tests pin the two properties that matter — the shape of the route survives,
and the endpoints are never lost.
"""
from __future__ import annotations

import math

import pytest

from core.motion import path_teach as pt


def _line(n: int, start=(0.0,) * 6, end=(60.0, 30.0, -45.0, 0.0, 15.0, 0.0)):
    """n samples evenly along a straight line in joint space."""
    return [[s + (e - s) * i / (n - 1) for s, e in zip(start, end)] for i in range(n)]


def test_a_straight_sweep_collapses_to_its_endpoints():
    """A long straight travel is two waypoints, however densely it was sampled."""
    simplified = pt.simplify(_line(200))
    assert len(simplified) == 2
    assert simplified[0] == pytest.approx(_line(200)[0])
    assert simplified[-1] == pytest.approx(_line(200)[-1])


def test_a_bend_is_kept():
    """RDP must not straighten a route that actually turns a corner."""
    out = _line(60, end=(60.0, 0, 0, 0, 0, 0))
    back = _line(60, start=(60.0, 0, 0, 0, 0, 0), end=(60.0, 60.0, 0, 0, 0, 0))
    simplified = pt.simplify(out + back)
    assert len(simplified) >= 3, "the corner was flattened away"
    corner = [wp for wp in simplified
              if wp[0] == pytest.approx(60.0, abs=2) and wp[1] == pytest.approx(0.0, abs=2)]
    assert corner, f"no waypoint near the corner in {simplified}"


def test_endpoints_always_survive():
    """A route that stops short of where the operator stopped is useless."""
    samples = _line(80)
    samples += [samples[-1]] * 20            # operator paused before releasing the button
    simplified = pt.simplify(samples)
    assert simplified[0] == pytest.approx(samples[0])
    assert simplified[-1] == pytest.approx(samples[-1])


def test_a_stationary_hand_produces_two_waypoints_not_hundreds():
    """Resting on the arm yields a cloud of near-identical readings."""
    jitter = [[0.1 * math.sin(i), 0.1 * math.cos(i), 0, 0, 0, 0] for i in range(300)]
    assert len(pt.simplify(jitter)) <= 2


def test_deadband_removes_micro_motion_but_keeps_real_motion():
    coarse = pt.simplify(_line(200), deadband_deg=20.0, rdp_tol_deg=0.0)
    fine = pt.simplify(_line(200), deadband_deg=0.1, rdp_tol_deg=0.0)
    assert len(coarse) < len(fine)


def test_tolerance_trades_fidelity_for_waypoint_count():
    zigzag = []
    for i in range(120):
        zigzag.append([i * 0.5, 10.0 * math.sin(i / 4), 0, 0, 0, 0])
    loose = pt.simplify(zigzag, rdp_tol_deg=20.0)
    tight = pt.simplify(zigzag, rdp_tol_deg=0.5)
    assert len(loose) < len(tight)


def test_empty_recording_yields_nothing():
    assert pt.simplify([]) == []


def test_simplify_handles_a_very_long_recording_without_recursing():
    """RDP is iterative on purpose — a stack overflow while saving loses the path."""
    assert len(pt.simplify(_line(20_000))) >= 2


def test_validate_rejects_a_path_too_short_to_replay():
    with pytest.raises(pt.PathError, match="at least"):
        pt.validate([[0.0] * 6], axis_count=6)


def test_validate_rejects_a_wrong_axis_count():
    with pytest.raises(pt.PathError, match="joint values"):
        pt.validate([[0.0] * 7, [1.0] * 7], axis_count=6)


def test_validate_rejects_non_finite_joints():
    with pytest.raises(pt.PathError, match="non-finite"):
        pt.validate([[0.0] * 6, [float("nan")] + [0.0] * 5], axis_count=6)


def test_validate_rejects_an_absurdly_long_path():
    with pytest.raises(pt.PathError, match="cap"):
        pt.validate([[float(i)] * 6 for i in range(pt.MAX_WAYPOINTS + 1)], axis_count=6)


def test_length_reports_total_joint_travel():
    p = pt.TaughtPath(name="p", device_id="right",
                      waypoints=[[0.0] * 6, [3.0, 4.0, 0, 0, 0, 0]])
    assert p.length_deg == pytest.approx(5.0)


def test_blend_radius_is_bounded_by_the_shortest_segment():
    """The controller rejects a radius longer than the track, so one tight corner
    caps blending for the whole route."""
    wps = [[0.0] * 6, [10.0] + [0.0] * 5, [12.0] + [0.0] * 5, [40.0] + [0.0] * 5]
    r = pt.max_blend_radius(wps)          # shortest segment is 2 deg
    assert 0 < r <= 2.0


def test_no_blend_radius_without_a_corner():
    """Two points are a straight line — nothing to round."""
    assert pt.max_blend_radius([[0.0] * 6, [10.0] + [0.0] * 5]) == 0.0
    assert pt.max_blend_radius([]) == 0.0


def test_blend_radius_scales_with_the_path():
    tight = pt.max_blend_radius([[0.0] * 6, [5.0] + [0.0] * 5, [10.0] + [0.0] * 5])
    open_ = pt.max_blend_radius([[0.0] * 6, [50.0] + [0.0] * 5, [100.0] + [0.0] * 5])
    assert open_ > tight


def test_rerunning_simplify_at_a_coarser_tolerance_removes_more():
    """This is what the Thin button does to an already-saved path."""
    zig = [[i * 1.0, 8.0 * math.sin(i / 3), 0, 0, 0, 0] for i in range(120)]
    once = pt.simplify(zig, rdp_tol_deg=3.0)
    twice = pt.simplify(once, deadband_deg=0.0, rdp_tol_deg=25.0)
    assert len(twice) < len(once)
    # endpoints must survive repeated thinning
    assert twice[0] == pytest.approx(once[0])
    assert twice[-1] == pytest.approx(once[-1])
