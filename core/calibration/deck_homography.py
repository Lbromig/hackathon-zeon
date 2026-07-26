"""Pixels to deck millimetres, with no depth and no intrinsics.

`ot_hand_eye` fits a full 3D rigid transform and needs the nozzle's position in
metres in the camera frame. That requires depth and factory intrinsics, which the
plain-UVC capture path does not provide: librealsense cannot claim these cameras
without root here, so `depth_m` and `camera_xyz` are null and the metric route is
closed.

It is not needed. The Opentrons deck is a plane, and a camera looking at a plane
induces a homography: eight parameters mapping image coordinates directly to deck
coordinates, recoverable from four or more correspondences, requiring no depth, no
intrinsics and no knowledge of where the camera is. For a fixed camera over a flat
workspace this is not a degraded substitute for the 3D fit, it is the right model.

The correspondences come from the same calibration procedure `ot_hand_eye` uses:
drive the OT to N known machine coordinates, observe the nozzle marker at each.
The difference is only that observations are pixels rather than metres.

What this deliberately cannot do:

- It gives x and y on the deck plane, never z. Height must come from the
  machine's own axis, not from here.
- It is valid only for the plane it was fitted on. A tube mouth 40 mm above the
  deck projects to a different pixel than a deck feature directly beneath it, and
  this model cannot tell them apart. Fit on the plane you intend to work in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

# Reprojection error above which a fit is not trusted. The OT's own positioning
# is not better than a millimetre, so a fit worse than this describes noise, or a
# mislabelled correspondence, rather than the geometry.
MAX_RMS_MM = 2.0

# Fewer than this and the fit is exactly determined, with no residual left to
# reveal a mistake. Four points can always be fitted perfectly, including four
# wrong ones, so a clean residual proves nothing. Five is the first count where
# the fit can be contradicted by its own data.
MIN_POINTS_FOR_TRUST = 5


@dataclass
class DeckFit:
    """An image-to-deck homography, with its own error bars."""

    matrix: np.ndarray                        # (3,3), image px -> deck mm
    rms_mm: float = 0.0                       # reprojection error on the fit data
    max_mm: float = 0.0                       # worst single point
    n_points: int = 0
    residuals_mm: list[float] = field(default_factory=list)

    @property
    def trustworthy(self) -> bool:
        """Whether this fit may be used to command motion.

        Deliberately conservative. A homography always produces a number, and an
        untrustworthy fit produces confident wrong millimetres, which on a
        machine with no endstops means a crash that every software signal reports
        as success.
        """
        return (
            self.n_points >= MIN_POINTS_FOR_TRUST
            and self.rms_mm <= MAX_RMS_MM
            and np.isfinite(self.matrix).all()
        )

    def why_not_trustworthy(self) -> str:
        if self.n_points < MIN_POINTS_FOR_TRUST:
            return (
                f"only {self.n_points} correspondences; {MIN_POINTS_FOR_TRUST} is the "
                "minimum at which the fit can be contradicted by its own data"
            )
        if not np.isfinite(self.matrix).all():
            return "fit produced non-finite values, so the geometry was degenerate"
        if self.rms_mm > MAX_RMS_MM:
            return f"reprojection RMS {self.rms_mm:.2f} mm exceeds {MAX_RMS_MM} mm"
        return ""

    def to_deck(self, uv: Sequence[float]) -> np.ndarray:
        """Map one image point to deck millimetres. Returns (2,)."""
        p = np.array([float(uv[0]), float(uv[1]), 1.0])
        q = self.matrix @ p
        if abs(q[2]) < 1e-12:
            # The point maps to the horizon of the fitted plane. There is no
            # finite deck coordinate for it, and returning a huge number would
            # look like a reachable target.
            raise ValueError(
                "image point projects to the plane's horizon; it is not on the deck"
            )
        return q[:2] / q[2]


def _collinear(points: np.ndarray, tol: float = 1e-6) -> bool:
    """True when points span less than two dimensions.

    A homography needs four points in general position. Collinear points leave
    the transform underdetermined across the plane while still admitting an exact
    fit along the line, so this cannot be caught by looking at the residual.
    """
    centred = points - points.mean(axis=0)
    if centred.shape[0] < 3:
        return True
    singular = np.linalg.svd(centred, compute_uv=False)
    return bool(singular[1] < tol * max(singular[0], 1.0))


def fit_deck_homography(
    image_points: Sequence[Sequence[float]],
    deck_mm: Sequence[Sequence[float]],
) -> DeckFit:
    """Fit image px -> deck mm from >= 4 correspondences.

    Solved by the standard DLT: each correspondence contributes two rows to a
    homogeneous system, and the solution is the right singular vector of the
    smallest singular value. Least squares over all points rather than a minimal
    four-point solve, so extra observations reduce error instead of being ignored.
    """
    img = np.asarray(image_points, dtype=float)
    deck = np.asarray(deck_mm, dtype=float)

    if img.shape[0] != deck.shape[0]:
        raise ValueError(
            f"{img.shape[0]} image points but {deck.shape[0]} deck points; "
            "correspondences must be paired"
        )
    if img.shape[0] < 4:
        raise ValueError(
            f"a homography needs at least 4 correspondences, got {img.shape[0]}"
        )
    if img.shape[1] != 2 or deck.shape[1] != 2:
        raise ValueError("both sides must be 2D points: image (u,v) and deck (x,y)")
    if _collinear(img) or _collinear(deck):
        raise ValueError(
            "correspondences are collinear, which leaves the transform "
            "underdetermined across the plane. Spread the calibration points over "
            "the working area rather than along a line."
        )

    # Normalise both sides. Pixel magnitudes are ~1e3 and millimetre magnitudes
    # ~1e2, and mixing scales that far apart makes the system badly conditioned.
    def _normalise(pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        centre = pts.mean(axis=0)
        spread = np.sqrt(((pts - centre) ** 2).sum(axis=1)).mean()
        scale = np.sqrt(2.0) / spread if spread > 1e-12 else 1.0
        T = np.array([[scale, 0, -scale * centre[0]],
                      [0, scale, -scale * centre[1]],
                      [0, 0, 1.0]])
        homo = np.hstack([pts, np.ones((pts.shape[0], 1))])
        return (T @ homo.T).T, T

    img_n, T_img = _normalise(img)
    deck_n, T_deck = _normalise(deck)

    rows = []
    for (u, v, _), (x, y, _) in zip(img_n, deck_n):
        rows.append([-u, -v, -1, 0, 0, 0, x * u, x * v, x])
        rows.append([0, 0, 0, -u, -v, -1, y * u, y * v, y])
    _, _, vt = np.linalg.svd(np.asarray(rows, dtype=float))
    H_n = vt[-1].reshape(3, 3)

    # Undo the normalisation so the result maps raw pixels to raw millimetres.
    H = np.linalg.inv(T_deck) @ H_n @ T_img
    if abs(H[2, 2]) > 1e-12:
        H = H / H[2, 2]

    fit = DeckFit(matrix=H, n_points=int(img.shape[0]))

    residuals = []
    for uv, target in zip(img, deck):
        try:
            residuals.append(float(np.linalg.norm(fit.to_deck(uv) - target)))
        except ValueError:
            residuals.append(float("inf"))
    fit.residuals_mm = residuals
    finite = [r for r in residuals if np.isfinite(r)]
    fit.rms_mm = float(np.sqrt(np.mean(np.square(finite)))) if finite else float("inf")
    fit.max_mm = max(finite) if finite else float("inf")
    return fit


def correction_mm(
    fit: DeckFit,
    observed_uv: Sequence[float],
    target_uv: Sequence[float],
) -> np.ndarray:
    """The relative OT move, in mm, that closes an error seen in the image.

    Both points are mapped to the deck and subtracted there, rather than
    subtracting in pixels and scaling. A homography is not a uniform scaling: the
    same pixel offset is a different number of millimetres depending on where in
    the frame it occurs, so a single mm-per-pixel factor is wrong everywhere
    except the point it was measured at.

    Returns (dx, dy) in deck millimetres. Relative on purpose: this machine has
    no endstops, so a relative move needs no trusted datum.
    """
    return fit.to_deck(target_uv) - fit.to_deck(observed_uv)
