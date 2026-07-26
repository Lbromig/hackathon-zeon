"""The OT <-> camera transform, tested against a simulated camera.

A mock observer stands in for the real detector: it applies a KNOWN rotation and
translation to whatever the OT was commanded, so the test can assert the fit
recovers that exact transform. That is the property worth pinning — if the fit
cannot recover a transform it was handed, it will not recover a real one either.

The failure modes tested here are the ones that produce a *plausible looking*
result rather than an obvious error: degenerate point geometry that leaves the
rotation underdetermined, and a unit/scale error that a scale-fitting solver would
silently absorb.
"""
from __future__ import annotations

import numpy as np
import pytest

from core.calibration.ot_hand_eye import (
    DEFAULT_CALIBRATION_OFFSETS_MM,
    CalibrationRun,
    fit_rigid_transform,
    servo_correction_mm,
)


def _rotation(rx: float, ry: float, rz: float) -> np.ndarray:
    cx, sx, cy, sy, cz, sz = (
        np.cos(rx), np.sin(rx), np.cos(ry), np.sin(ry), np.cos(rz), np.sin(rz)
    )
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


class MockCamera:
    """A camera at a known pose, optionally noisy or mis-scaled."""

    def __init__(self, R: np.ndarray, t: np.ndarray,
                 noise_m: float = 0.0, scale: float = 1.0, seed: int = 0) -> None:
        self.R, self.t, self.noise_m, self.scale = R, t, noise_m, scale
        self.rng = np.random.default_rng(seed)

    def observe_at(self, ot_m: np.ndarray) -> np.ndarray:
        seen = self.R @ (np.asarray(ot_m, dtype=float) * self.scale) + self.t
        if self.noise_m:
            seen = seen + self.rng.normal(0.0, self.noise_m, size=3)
        return seen


def _run(cam: MockCamera, offsets_mm=DEFAULT_CALIBRATION_OFFSETS_MM) -> CalibrationRun:
    run = CalibrationRun()
    for off in offsets_mm:
        ot_mm = np.asarray(off, dtype=float)
        run.add(ot_mm, cam.observe_at(ot_mm / 1000.0))
    return run


def test_fit_recovers_a_known_transform_exactly():
    R = _rotation(0.10, -0.25, 1.10)
    t = np.array([0.32, -0.11, 0.58])
    fit = _run(MockCamera(R, t)).fit()

    assert fit.rms_residual_m < 1e-9, fit.describe()
    assert np.allclose(fit.rotation, R, atol=1e-9)
    assert np.allclose(fit.translation, t, atol=1e-9)
    assert fit.trustworthy, fit.describe()


def test_fit_survives_realistic_detection_noise():
    """Half a millimetre of jitter must still yield a usable fit."""
    R, t = _rotation(0.05, 0.2, -0.7), np.array([0.1, 0.2, 0.4])
    fit = _run(MockCamera(R, t, noise_m=0.0005, seed=7)).fit()

    assert fit.trustworthy, fit.describe()
    assert fit.rms_residual_m < 0.002, fit.describe()
    # Recovered rotation should still be close to truth despite the noise.
    assert np.linalg.norm(fit.rotation - R) < 0.05


def test_collinear_points_are_refused_not_silently_fitted():
    """Points along one axis cannot pin down a rotation.

    A solver that returned a fit here would be reporting confidence it does not
    have, and the unconstrained axis would only reveal itself as a crash.
    """
    R, t = _rotation(0.0, 0.0, 0.3), np.zeros(3)
    straight = tuple((x, 0.0, 0.0) for x in (0.0, 20.0, 40.0, 60.0, 80.0))
    with pytest.raises(ValueError, match="collinear|degenerate"):
        _run(MockCamera(R, t), offsets_mm=straight).fit()


def test_a_scale_error_is_surfaced_rather_than_absorbed():
    """A 10x unit slip must fail the trust check, not vanish into the fit.

    This is the depth-scale trap: the D405 uses 1e-4 m per count where the rest
    of the D400 series uses 1e-3. A scale-fitting solver would absorb it and
    every commanded position would be wrong but plausible.
    """
    R, t = _rotation(0.0, 0.0, 0.0), np.zeros(3)
    fit = _run(MockCamera(R, t, scale=10.0)).fit()

    assert not fit.trustworthy, fit.describe()
    assert fit.scale_check > 5.0, f"scale error not visible: {fit.scale_check}"


def test_too_few_points_is_not_trustworthy():
    R, t = _rotation(0.1, 0.1, 0.1), np.array([0.1, 0.0, 0.2])
    three = ((0.0, 0.0, 0.0), (50.0, 0.0, 0.0), (0.0, 50.0, 20.0))
    fit = _run(MockCamera(R, t), offsets_mm=three).fit()
    assert fit.n_points == 3
    assert not fit.trustworthy, "3 points fits, but is below the confidence bar"


def test_servo_correction_closes_the_error_in_one_step():
    """The correction must be expressed in OT axes, not camera axes."""
    R, t = _rotation(0.2, -0.4, 0.9), np.array([0.2, 0.1, 0.3])
    cam = MockCamera(R, t)
    fit = _run(cam).fit()

    ot_now = np.array([0.030, 0.020, 0.010])      # metres
    ot_target = np.array([0.055, 0.041, 0.018])
    observed, target = cam.observe_at(ot_now), cam.observe_at(ot_target)

    move_mm = servo_correction_mm(fit, observed, target)
    expected_mm = (ot_target - ot_now) * 1000.0
    assert np.allclose(move_mm, expected_mm, atol=1e-6), f"{move_mm} vs {expected_mm}"


def test_servo_loop_converges_under_noise():
    """Iterating the correction must shrink the error, not oscillate."""
    R, t = _rotation(0.1, 0.3, -0.5), np.array([0.15, -0.05, 0.45])
    cam = MockCamera(R, t, noise_m=0.0003, seed=11)
    fit = _run(cam).fit()

    ot = np.array([0.010, 0.010, 0.005])
    ot_target = np.array([0.080, 0.055, 0.030])
    target_cam = cam.observe_at(ot_target)

    first = None
    for _ in range(6):
        err = float(np.linalg.norm(cam.observe_at(ot) - target_cam))
        if first is None:
            first = err
        ot = ot + servo_correction_mm(fit, cam.observe_at(ot), target_cam) / 1000.0

    final = float(np.linalg.norm(cam.observe_at(ot) - target_cam))
    assert final < first, f"did not converge: {first:.4f} -> {final:.4f}"
    assert final < 0.002, f"residual too large after servo: {final*1000:.2f} mm"


def test_apply_and_invert_round_trip():
    R, t = _rotation(0.3, 0.2, 0.1), np.array([0.4, 0.5, 0.6])
    fit = _run(MockCamera(R, t)).fit()
    p = np.array([0.021, 0.037, 0.014])
    assert np.allclose(fit.invert(fit.apply(p)), p, atol=1e-9)
