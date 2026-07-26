"""Vision-guided gantry motion.

A simulated bench: a known homography maps deck millimetres to pixels, a fake gantry
integrates the jogs it is given, and the observer reports where the carriage now
appears. No camera and no robot.

The convergence tests are the easy half. The ones that matter are the refusals,
because this machine has no endstops and no current sensing: a sign error, a stalled
axis and a lost marker all produce a controller that reports success while the
carriage goes somewhere else, and the loop's only defence is to notice the error is
not shrinking and stop.
"""
from __future__ import annotations

import numpy as np
import pytest

from core.calibration.deck_homography import fit_deck_homography
from core.motion.visual_servo import (
    DEFAULT_CALIBRATION_OFFSETS_MM,
    MAX_STEP_MM,
    Outcome,
    collect_calibration,
    servo_to_target,
)

# Deck millimetres -> pixels. Perspective, so the mapping is genuinely projective.
DECK_TO_PX = np.array([
    [3.1, 0.12, 300.0],
    [-0.09, 3.05, 240.0],
    [0.0006, 0.0004, 1.0],
])


def project(xy) -> tuple[float, float]:
    q = DECK_TO_PX @ np.array([xy[0], xy[1], 1.0])
    return (float(q[0] / q[2]), float(q[1] / q[2]))


class FakeGantry:
    """Integrates jogs. Optionally stalls an axis or inverts one."""

    def __init__(self, x=0.0, y=0.0, stall: str | None = None, invert: str | None = None):
        self.x, self.y = x, y
        self.stall, self.invert = stall, invert
        self.commands: list[tuple[str, float]] = []

    def jog(self, axis: str, delta_mm: float) -> None:
        self.commands.append((axis, delta_mm))
        if axis == self.stall:
            return                     # accepted, acknowledged, went nowhere
        if axis == self.invert:
            delta_mm = -delta_mm       # calibration sign error
        if axis == "X":
            self.x += delta_mm
        elif axis == "Y":
            self.y += delta_mm


class Eyes:
    """Sees the carriage. `blind_after` simulates the marker leaving view."""

    def __init__(self, gantry: FakeGantry, blind_after: int | None = None, noise_px=0.0, seed=0):
        self.g, self.blind_after, self.noise = gantry, blind_after, noise_px
        self.n = 0
        self.rng = np.random.default_rng(seed)

    def observe(self):
        self.n += 1
        if self.blind_after is not None and self.n > self.blind_after:
            return None
        uv = project((self.g.x, self.g.y))
        if self.noise:
            uv = (uv[0] + self.rng.normal(0, self.noise), uv[1] + self.rng.normal(0, self.noise))
        return uv


@pytest.fixture
def fit():
    """Calibrate exactly as the bench procedure would: drive offsets, observe."""
    g = FakeGantry()
    img, deck = collect_calibration(g, Eyes(g), DEFAULT_CALIBRATION_OFFSETS_MM)
    f = fit_deck_homography(img, deck)
    assert f.trustworthy, f.why_not_trustworthy()
    return f


def test_calibration_procedure_produces_a_trustworthy_fit(fit):
    assert fit.n_points >= 5
    assert fit.rms_mm < 0.5


def test_it_drives_the_carriage_onto_the_target(fit):
    g = FakeGantry()
    target = project((22.0, -16.0))
    result = servo_to_target(fit, g, Eyes(g), target)
    assert result.outcome is Outcome.ARRIVED, result.detail
    assert result.ok
    # Arrived physically, not just numerically.
    assert (g.x, g.y) == pytest.approx((22.0, -16.0), abs=1.5)


def test_it_converges_under_detection_noise(fit):
    g = FakeGantry()
    target = project((14.0, 9.0))
    result = servo_to_target(fit, g, Eyes(g, noise_px=1.0, seed=5), target)
    assert result.outcome is Outcome.ARRIVED, result.detail


def test_an_inverted_axis_is_caught_before_it_runs_away(fit):
    """The collision case.

    A sign error in the calibration drives the carriage away from the target. There
    are no endstops, so nothing else notices. The loop must see the error grow and
    stop.
    """
    g = FakeGantry(invert="X")
    target = project((20.0, 0.0))
    result = servo_to_target(fit, g, Eyes(g), target)
    assert result.outcome is Outcome.NOT_CONVERGING
    assert not result.ok
    assert "not progress" in result.detail
    # It must give up quickly rather than grinding through the whole budget.
    assert len(g.commands) <= 6


def test_a_stalled_axis_is_caught(fit):
    """A stall is silent: steps are issued, the move completes, nothing happens."""
    g = FakeGantry(stall="Y")
    target = project((0.0, 25.0))
    result = servo_to_target(fit, g, Eyes(g), target)
    assert result.outcome is Outcome.NOT_CONVERGING
    assert not result.ok


def test_losing_the_marker_stops_the_loop(fit):
    g = FakeGantry()
    target = project((18.0, 12.0))
    result = servo_to_target(fit, g, Eyes(g, blind_after=2), target)
    assert result.outcome is Outcome.LOST_SIGHT
    assert "stale" in result.detail


def test_an_absurdly_far_target_refuses_rather_than_traversing(fit):
    """Beyond the reach bound, a human decides. Nothing is commanded."""
    g = FakeGantry()
    result = servo_to_target(fit, g, Eyes(g), project((400.0, 300.0)))
    assert result.outcome is Outcome.STEP_TOO_LARGE
    assert not result.moved
    assert g.commands == []


def test_a_distant_target_is_reached_in_clamped_steps(fit):
    """The behaviour a naive step limit would have broken.

    40 mm is far more than one step, so the loop must approach it in bounded
    increments rather than refusing or lunging.
    """
    g = FakeGantry()
    result = servo_to_target(fit, g, Eyes(g), project((40.0, 30.0)))
    assert result.outcome is Outcome.ARRIVED, result.detail
    assert (g.x, g.y) == pytest.approx((40.0, 30.0), abs=1.5)
    # Every commanded jog stayed inside the safety limit.
    assert all(abs(d) <= MAX_STEP_MM + 1e-9 for _, d in g.commands)
    assert len(result.steps) > 2          # genuinely took several


def test_an_untrustworthy_fit_commands_nothing(fit):
    """Refusal must be total: no move at all, not a smaller move."""
    g = FakeGantry()
    four = fit_deck_homography(
        [[300, 240], [340, 240], [340, 280], [300, 280]],
        [[0, 0], [12, 0], [12, 12], [0, 12]],
    )
    assert not four.trustworthy
    result = servo_to_target(four, g, Eyes(g), project((10.0, 10.0)))
    assert result.outcome is Outcome.REFUSED
    assert not result.moved
    assert g.commands == []


def test_already_on_target_commands_nothing(fit):
    g = FakeGantry(x=8.0, y=5.0)
    result = servo_to_target(fit, g, Eyes(g), project((8.0, 5.0)))
    assert result.outcome is Outcome.ARRIVED
    assert not result.moved


def test_calibration_skips_stops_it_cannot_see_rather_than_guessing(fit):
    g = FakeGantry()
    img, deck = collect_calibration(g, Eyes(g, blind_after=3), DEFAULT_CALIBRATION_OFFSETS_MM)
    assert len(img) == len(deck) == 3
    # The gantry still completed the whole pattern; only observation was lost.
    assert len(g.commands) == 2 * len(DEFAULT_CALIBRATION_OFFSETS_MM)


def test_the_default_offsets_are_not_collinear():
    """A raster's first row is collinear, and the fit refuses collinear input.

    Discovering that at the end of a slow physical procedure is the failure this
    default exists to prevent.
    """
    g = FakeGantry()
    img, deck = collect_calibration(g, Eyes(g), DEFAULT_CALIBRATION_OFFSETS_MM)
    fit_deck_homography(img, deck)          # must not raise


def test_every_step_is_recorded_for_audit(fit):
    g = FakeGantry()
    result = servo_to_target(fit, g, Eyes(g), project((15.0, -10.0)))
    assert result.steps
    assert all(s.carriage_uv is not None for s in result.steps)
    assert result.steps[0].error_mm > result.steps[-1].error_mm
