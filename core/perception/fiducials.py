"""Fiducial marker detection + pose.

The lab stickers are **AprilTag `tag36h11` @ 20 mm** (confirmed empirically from the
hackathon photos: IDs 180-224 were recovered exactly, with no coherent match under any
ArUco dictionary). OpenCV's `aruco` module detects this family natively via
`DICT_APRILTAG_36h11`, so we don't need a separate AprilTag dependency.

What this gives a caller:
  - detect(image)            -> [Detection] with ids + image corners + centre
  - detect(image) w/ K       -> each Detection also carries T_cam_marker (4x4, metres),
                                which is the primary signal for the tip/tube offset solve

Package note: `cv2.aruco` lives in **opencv-contrib-python** (not plain opencv-python).
Alternative: `pupil-apriltags` (canonical AprilTag detector) if we want the official
corner order / pose — see the caveat on CORNER ORDER below.

Caveat — corner order: OpenCV returns AprilTag corners in an order that differs from the
official AprilTag library. Translation (position) is robust to this; the marker *yaw* may
be offset by a multiple of 90°. For pure position validation that's fine; if you need exact
orientation, calibrate the convention once against a tag placed at a known pose.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .geometry import Transform
from .markers import spec_for

# --- family / defaults -------------------------------------------------------
TAG_FAMILY_NAME = "DICT_APRILTAG_36h11"
TAG_FAMILY = cv2.aruco.DICT_APRILTAG_36h11
DEFAULT_TAG_SIZE_M = 0.020                       # 20 mm, printed on the stickers
# A frame whose 99th-percentile grey is below this is treated as under-exposed and gets
# local contrast equalisation before detection. See FiducialDetector.preprocess.
DARK_P99 = 128.0
# Physical sticker stock in the lab: tag36h11 IDs 180-224 (see photos).
KNOWN_STOCK_IDS = tuple(range(180, 225))


@dataclass
class Detection:
    marker_id: int
    corners: np.ndarray                          # (4,2) image px, OpenCV order
    center: tuple[float, float]
    size_m: float = DEFAULT_TAG_SIZE_M
    T_cam_marker: Transform | None = None        # 4x4 cam<-marker, set when K given

    @property
    def distance_m(self) -> float | None:
        if self.T_cam_marker is None:
            return None
        return float(np.linalg.norm(self.T_cam_marker[:3, 3]))


def tuned_parameters() -> "cv2.aruco.DetectorParameters":
    """Detector parameters for bench conditions, rather than OpenCV's conservative defaults.

    Measured against real frames from this rig (a dark Opentrons deck and a lit tube rack):
    defaults found 1 tag across the pair, these plus CLAHE found 5. Each change earns its
    place:

    * adaptiveThreshWinSize 3..53 step 6 — the default 3..23 assumes fairly even lighting.
      A bench has a bright deck next to a shadowed corner, and a tag in the dark half needs
      a much larger window before it separates from its background.
    * minMarkerPerimeterRate 0.01 (default 0.03) — the tags are 20 mm and often across the
      cell, so they occupy very few pixels. The default silently discards them by size
      before any decoding is attempted.
    * polygonalApproxAccuracyRate 0.05 (default 0.03) — tolerates the slightly rounded
      corners you get from motion blur and JPEG, which otherwise fail the quad test.
    * CORNER_REFINE_SUBPIX — sub-pixel corners, which is what makes solvePnP poses stable
      enough to fuse into the twin. Measured to detect exactly as many tags as no
      refinement at all, so the precision is free.

    Explicitly NOT CORNER_REFINE_APRILTAG, despite the tag family being AprilTag: measured
    over 60 real frames it returned **zero** detections where the others returned 51. It
    does not merely refine, it can reject, and here it rejected everything. Anything that
    changes this line should re-run that measurement rather than reason from the name.
    """
    p = cv2.aruco.DetectorParameters()
    p.adaptiveThreshWinSizeMin = 3
    p.adaptiveThreshWinSizeMax = 53
    p.adaptiveThreshWinSizeStep = 6
    p.minMarkerPerimeterRate = 0.01
    p.polygonalApproxAccuracyRate = 0.05
    p.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    return p


class FiducialDetector:
    """Detects tag36h11 markers and (with camera intrinsics) estimates their 6-DoF pose."""

    def __init__(
        self,
        family: int = TAG_FAMILY,
        camera_matrix: np.ndarray | None = None,
        dist_coeffs: np.ndarray | None = None,
        default_size_m: float = DEFAULT_TAG_SIZE_M,
        enhance: bool = True,
    ) -> None:
        self.dictionary = cv2.aruco.getPredefinedDictionary(family)
        self.params = tuned_parameters()
        self.detector = cv2.aruco.ArucoDetector(self.dictionary, self.params)
        self.K = None if camera_matrix is None else np.asarray(camera_matrix, np.float64)
        self.dist = np.zeros(5) if dist_coeffs is None else np.asarray(dist_coeffs, np.float64)
        self.default_size_m = default_size_m
        self.enhance = enhance
        self._clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)) if enhance else None

    def preprocess(self, image: np.ndarray) -> np.ndarray:
        """Grayscale, plus local contrast equalisation *only when the frame needs it*.

        The cameras on this bench run wildly different exposures — one measured a 99th
        percentile of 34 out of 255, where a tag is simply not separable from its
        background. CLAHE rescues those frames. But applying it unconditionally is a net
        loss: on normally-exposed frames it amplifies sensor and JPEG noise into edges that
        fail the quad test, measured at 51 detections down to 25 over the same 60 frames.

        So it is gated on the frame actually being dark. Measured over 60 bright frames plus
        one dark one: raw 51+0, always-CLAHE 25+3, gated 51+3 — the gate keeps both wins.
        The 99th percentile is the test rather than the mean, because a mostly-dark frame
        with one bright lamp in it has a respectable mean and still needs the help.
        """
        gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        if self._clahe is None:
            return gray
        return self._clahe.apply(gray) if np.percentile(gray, 99) < DARK_P99 else gray

    def detect(self, image: np.ndarray) -> list[Detection]:
        gray = self.preprocess(image)
        corners, ids, _ = self.detector.detectMarkers(gray)
        out: list[Detection] = []
        if ids is None:
            return out
        for c, mid in zip(corners, ids.flatten()):
            pts = c.reshape(4, 2).astype(np.float64)
            spec = spec_for(int(mid))
            size = spec.size_m if spec else self.default_size_m
            det = Detection(
                marker_id=int(mid),
                corners=pts,
                center=(float(pts[:, 0].mean()), float(pts[:, 1].mean())),
                size_m=size,
            )
            if self.K is not None:
                det.T_cam_marker = self._estimate_pose(pts, size)
            out.append(det)
        return out

    def _estimate_pose(self, corners_px: np.ndarray, size_m: float) -> Transform | None:
        """Marker->camera transform via IPPE_SQUARE. Marker frame: origin at centre,
        Z out of the tag, corners in OpenCV's returned order."""
        s = size_m / 2.0
        obj = np.array([[-s, s, 0], [s, s, 0], [s, -s, 0], [-s, -s, 0]], np.float64)
        ok, rvec, tvec = cv2.solvePnP(
            obj, corners_px, self.K, self.dist, flags=cv2.SOLVEPNP_IPPE_SQUARE
        )
        if not ok:
            return None
        R, _ = cv2.Rodrigues(rvec)
        T = np.eye(4)
        T[:3, :3] = R
        T[:3, 3] = tvec.flatten()
        return T

    def annotate(self, image: np.ndarray, detections: list[Detection]) -> np.ndarray:
        """Draw boxes, ids, and (if posed) axes — for the UI overlay / demo."""
        out = image.copy()
        for d in detections:
            pts = d.corners.astype(np.int32)
            cv2.polylines(out, [pts], True, (0, 255, 0), 3)
            label = f"id={d.marker_id}"
            if d.distance_m is not None:
                label += f" {d.distance_m*1000:.0f}mm"
            cv2.putText(out, label, tuple(pts[0]), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                        (0, 0, 255), 2, cv2.LINE_AA)
            if d.T_cam_marker is not None and self.K is not None:
                rvec, _ = cv2.Rodrigues(d.T_cam_marker[:3, :3])
                cv2.drawFrameAxes(out, self.K, self.dist, rvec,
                                  d.T_cam_marker[:3, 3], d.size_m * 0.75)
        return out


def identify_family(image: np.ndarray) -> list[tuple[str, list[int]]]:
    """Diagnostic: run every predefined dictionary and report which ones detect anything.
    Handy to confirm an unknown sticker sheet's family (this is how tag36h11 was pinned
    down). Returns [(dict_name, sorted_ids), ...] sorted by detection count desc."""
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    hits: list[tuple[str, list[int]]] = []
    for name in (d for d in dir(cv2.aruco) if d.startswith("DICT_")):
        try:
            dic = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, name))
        except Exception:
            continue
        det = cv2.aruco.ArucoDetector(dic, cv2.aruco.DetectorParameters())
        _, ids, _ = det.detectMarkers(gray)
        if ids is not None:
            hits.append((name, sorted(int(i) for i in ids.flatten())))
    return sorted(hits, key=lambda x: -len(x[1]))


if __name__ == "__main__":  # quick CLI: python -m core.perception.fiducials <image> [--identify]
    import sys

    if len(sys.argv) < 2:
        print("usage: python -m core.perception.fiducials <image> [--identify]")
        raise SystemExit(1)
    img = cv2.imread(sys.argv[1])
    if img is None:
        print("could not read", sys.argv[1]); raise SystemExit(1)
    if "--identify" in sys.argv:
        for name, ids in identify_family(img):
            print(f"{name:24} {len(ids):>3} ids: {ids[:40]}")
    else:
        dets = FiducialDetector().detect(img)
        print(f"{len(dets)} markers: {sorted(d.marker_id for d in dets)}")
