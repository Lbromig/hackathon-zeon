"""Tie the cameras to the OT-One: camera frame <-> OT machine frame.

This is the missing link between perception and liquid-handler motion. Vision can
already recover where a tube is (`core/perception/fiducials.py` returns a 4x4
`T_cam_marker`), and the OT can already be commanded to machine coordinates
(`OpentronsDriver.move_to_machine`). What was absent is the single transform
between those two frames, so a detection could never become a move.

Two stages, and the second one matters more than it looks.

**Stage 1, open loop.** Put a marker on the nozzle. Drive the OT to N known
machine coordinates, observe the marker at each, and fit the rigid transform that
maps OT coordinates to camera coordinates. A detection can then be converted into
a target and commanded directly. This lands within a few mm — limited by the fit
residual, the marker mounting, and the machine's own repeatability.

**Stage 2, closed loop.** Re-observe the nozzle after moving, compute the residual
error, and jog *relatively* until it is under tolerance.

That second stage is also the only crash detection this machine will ever have.
There are no endstops and no current sensing, so a stalled stepper still has its
steps issued, the move completes in the predicted time, and the counter keeps
counting — the machine coordinate silently drifts from physical reality with every
software signal reporting success. Vision re-observing the nozzle is what catches
it. So positioning and verification are the same loop here, not two features.

Y is the interesting case. It cannot be homed (`G28.2 Y` searches for an endstop
that never reports and grinds), so its counter has no physical reference at all.
But it does not need one: the camera measures nozzle Y directly, so Y closes
purely visually. Vision supplies the missing datum rather than working around it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

import numpy as np


class NozzleObserver(Protocol):
    """Anything that can report where the nozzle is, in camera coordinates.

    Deliberately narrow so the real detector and a mock are interchangeable: the
    whole loop is testable with no camera attached. A real implementation wraps
    `FiducialDetector.detect()` and returns the translation of `T_cam_marker` for
    the nozzle marker's id.
    """

    def observe(self) -> np.ndarray | None:
        """Nozzle position in camera frame as (3,) metres, or None if not seen."""
        ...


@dataclass
class HandEyeFit:
    """The fitted OT-machine -> camera transform, with its own error bars."""

    rotation: np.ndarray            # (3,3)
    translation: np.ndarray         # (3,)
    rms_residual_m: float           # fit quality: how well the model explains the data
    max_residual_m: float
    n_points: int
    scale_check: float              # observed/commanded distance ratio; should be ~1.0

    def apply(self, ot_xyz: np.ndarray) -> np.ndarray:
        """Map an OT machine coordinate (metres) into camera coordinates."""
        return self.rotation @ np.asarray(ot_xyz, dtype=float) + self.translation

    def invert(self, cam_xyz: np.ndarray) -> np.ndarray:
        """Map a camera coordinate back into OT machine coordinates (metres)."""
        return self.rotation.T @ (np.asarray(cam_xyz, dtype=float) - self.translation)

    @property
    def trustworthy(self) -> bool:
        """Whether this fit is good enough to command motion from.

        Thresholds are deliberately strict: a 2 mm RMS fit will place a pipette
        2 mm from where it was asked to go, and the scale check catches the
        classic unit error (a depth scale off by 10x still produces a plausible
        looking fit).
        """
        return (
            self.n_points >= 4
            and self.rms_residual_m <= 0.002
            and abs(self.scale_check - 1.0) <= 0.05
        )

    def describe(self) -> str:
        verdict = "usable" if self.trustworthy else "NOT trustworthy"
        return (
            f"hand-eye fit over {self.n_points} points: "
            f"rms {self.rms_residual_m * 1000:.2f} mm, "
            f"max {self.max_residual_m * 1000:.2f} mm, "
            f"scale {self.scale_check:.4f} -> {verdict}"
        )


def fit_rigid_transform(ot_points: Sequence[Sequence[float]],
                        cam_points: Sequence[Sequence[float]]) -> HandEyeFit:
    """Least-squares rigid transform (Kabsch) mapping OT points to camera points.

    Rigid on purpose: rotation and translation only, no scale. Both frames are
    metric, so a fitted scale would be hiding a unit error rather than modelling
    anything real. `scale_check` reports the ratio instead, so the error surfaces
    as a number to look at rather than being silently absorbed.
    """
    src = np.asarray(ot_points, dtype=float)
    dst = np.asarray(cam_points, dtype=float)
    if src.shape != dst.shape or src.ndim != 2 or src.shape[1] != 3:
        raise ValueError("ot_points and cam_points must both be (N,3) and equal length")
    n = src.shape[0]
    if n < 3:
        raise ValueError(f"need at least 3 correspondences to fit a transform, got {n}")

    src_c, dst_c = src.mean(axis=0), dst.mean(axis=0)
    P, Q = src - src_c, dst - dst_c

    # Degenerate geometry check. COLLINEAR points cannot pin down a rotation: the
    # spin about the shared axis is unconstrained, so the fit would look clean
    # while being wrong in a way that only shows up as a crash.
    #
    # Coplanar is NOT degenerate here — three non-collinear points already
    # determine a rigid transform uniquely, and any three points are planar by
    # definition, so requiring rank 3 would reject valid calibration sets.
    # Spreading points over all three axes still helps conditioning under noise,
    # which is why the default offsets do; it is just not a correctness
    # requirement.
    if np.linalg.matrix_rank(P, tol=1e-9) < 2:
        raise ValueError(
            "the commanded points are collinear, so the rotation about that axis "
            "is underdetermined. Spread the calibration points over at least two "
            "axes."
        )

    H = P.T @ Q
    U, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    R = Vt.T @ np.diag([1.0, 1.0, d]) @ U.T   # the diag prevents a reflection
    t = dst_c - R @ src_c

    residuals = np.linalg.norm((R @ src.T).T + t - dst, axis=1)
    src_spread = float(np.linalg.norm(P, axis=1).sum())
    dst_spread = float(np.linalg.norm(Q, axis=1).sum())
    scale = dst_spread / src_spread if src_spread > 1e-12 else float("nan")

    return HandEyeFit(
        rotation=R,
        translation=t,
        rms_residual_m=float(np.sqrt((residuals ** 2).mean())),
        max_residual_m=float(residuals.max()),
        n_points=n,
        scale_check=scale,
    )


# Machine coordinates to visit during calibration, in mm, as (X, Y, Z) offsets
# from wherever the machine starts. Spread over all three axes on purpose: a
# collinear set is genuinely degenerate and gets refused, and while a flat set is
# mathematically sufficient, depth error dominates when every point sits at the
# same height. Varying Z is what makes the fit robust rather than merely valid.
DEFAULT_CALIBRATION_OFFSETS_MM: tuple[tuple[float, float, float], ...] = (
    (0.0, 0.0, 0.0),
    (60.0, 0.0, 0.0),
    (60.0, 40.0, 0.0),
    (0.0, 40.0, 0.0),
    (30.0, 20.0, 25.0),
    (90.0, 20.0, 10.0),
    (30.0, 60.0, 15.0),
)


@dataclass
class CalibrationRun:
    """Paired observations collected while driving the OT around."""

    ot_mm: list[list[float]] = field(default_factory=list)
    cam_m: list[list[float]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def add(self, ot_mm: Sequence[float], cam_m: Sequence[float]) -> None:
        self.ot_mm.append([float(v) for v in ot_mm])
        self.cam_m.append([float(v) for v in cam_m])

    def fit(self) -> HandEyeFit:
        """Fit, converting the OT side from mm to metres so both frames match."""
        ot_m = (np.asarray(self.ot_mm, dtype=float) / 1000.0).tolist()
        return fit_rigid_transform(ot_m, self.cam_m)


def servo_correction_mm(fit: HandEyeFit,
                        observed_cam_m: np.ndarray,
                        target_cam_m: np.ndarray) -> np.ndarray:
    """The relative OT move, in mm, that would close a camera-frame error.

    Rotating the error into OT axes rather than assuming the frames are aligned
    is the whole point: a camera mounted at any angle produces an error whose
    axes do not correspond to the machine's.
    """
    error_cam = np.asarray(target_cam_m, dtype=float) - np.asarray(observed_cam_m, dtype=float)
    return (fit.rotation.T @ error_cam) * 1000.0


__all__ = [
    "NozzleObserver",
    "HandEyeFit",
    "CalibrationRun",
    "fit_rigid_transform",
    "servo_correction_mm",
    "DEFAULT_CALIBRATION_OFFSETS_MM",
]
