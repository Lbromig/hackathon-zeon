"""Classical-CV detection of un-tagged round labware (FR-DET, T1).

Tubes, caps and empty wells read as circles from above, so a Hough-circle pass finds them
without any tag or learned model. On a RealSense we also back-project each circle centre
through the aligned depth to a metric 3-D point (camera frame) and estimate the real
diameter — enough to guess tube vs cap vs well and to feed the twin a position.

This complements the two reliable sources (AprilTag for identity/pose, twin-projection for
known geometry): it catches the things that carry no tag and aren't in the twin yet.

Output matches what the camera hub publishes: a normalized polygon (approximating the
circle), a normalized centre, an optional `camera_xyz`, and a coarse `kind`. cv2 is imported
lazily so the module still imports on a box without OpenCV (detection just returns nothing).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

# Real-diameter buckets (metres) -> coarse kind. Overlapping labware sizes make this a
# hint, not an identity claim (tags own identity); it only labels the highlight.
_DIAM_BUCKETS = (
    (0.030, "cap"),     # 50 mL cap Ø34 / large disc
    (0.019, "tube"),    # 50 mL tube Ø28 opening
    (0.0, "well"),      # smaller circle: 15 mL / well / tip
)


@dataclass
class Shape:
    kind: str
    polygon: list[list[float]]
    center: list[float]
    source: str = "cv"
    radius_px: float = 0.0
    diameter_m: float | None = None
    camera_xyz: list[float] | None = None
    confidence: float = 0.5

    def as_detection_kwargs(self) -> dict:
        return {"kind": self.kind, "polygon": self.polygon, "center": self.center,
                "source": self.source, "camera_xyz": self.camera_xyz,
                "confidence": self.confidence}


def _sample_depth(depth, u: float, v: float, win: int = 2) -> float | None:
    """Median of the valid (non-zero) depths in a small window around (u, v)."""
    if depth is None:
        return None
    h, w = depth.shape[:2]
    ui, vi = int(round(u)), int(round(v))
    if not (0 <= ui < w and 0 <= vi < h):
        return None
    patch = depth[max(0, vi - win):vi + win + 1, max(0, ui - win):ui + win + 1]
    vals = patch[patch > 0]
    return float(np.median(vals)) if vals.size else None


class ShapeDetector:
    def __init__(self, intrinsics: dict | None = None, *,
                 min_radius_px: int = 8, max_radius_px: int = 160,
                 param2: int = 32, max_circles: int = 16) -> None:
        self.intr = intrinsics
        self.min_r = min_radius_px
        self.max_r = max_radius_px
        self.param2 = param2                 # higher -> fewer false circles
        self.max_circles = max_circles

    def detect(self, frame_bgr, depth=None) -> list[Shape]:
        if cv2 is None or frame_bgr is None:
            return []
        h, w = frame_bgr.shape[:2]
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY) if frame_bgr.ndim == 3 else frame_bgr
        gray = cv2.medianBlur(gray, 5)
        circles = cv2.HoughCircles(
            gray, cv2.HOUGH_GRADIENT, dp=1.2, minDist=max(self.min_r * 2, 20),
            param1=100, param2=self.param2, minRadius=self.min_r, maxRadius=self.max_r)
        if circles is None:
            return []
        out: list[Shape] = []
        for x, y, r in np.round(circles[0]).astype(float)[: self.max_circles]:
            out.append(self._to_shape(float(x), float(y), float(r), w, h, depth))
        return out

    # --- helpers -------------------------------------------------------------
    def _to_shape(self, x, y, r, w, h, depth) -> Shape:
        poly = [[float(np.clip((x + r * np.cos(a)) / w, 0, 1)),
                 float(np.clip((y + r * np.sin(a)) / h, 0, 1))]
                for a in np.linspace(0, 2 * np.pi, 16, endpoint=False)]
        d = _sample_depth(depth, x, y)
        xyz = self._camera_point(x, y, d)
        diam = self._diameter_m(r, d)
        return Shape(
            kind=self._kind(diam),
            polygon=[[round(px, 4), round(py, 4)] for px, py in poly],
            center=[round(x / w, 4), round(y / h, 4)],
            radius_px=round(r, 1),
            diameter_m=None if diam is None else round(diam, 4),
            camera_xyz=xyz,
            confidence=0.5,
        )

    def _camera_point(self, u, v, depth_m) -> list[float] | None:
        i = self.intr
        if not depth_m or not i or not i.get("fx") or not i.get("fy"):
            return None
        return [round((u - i["cx"]) * depth_m / i["fx"], 4),
                round((v - i["cy"]) * depth_m / i["fy"], 4),
                round(depth_m, 4)]

    def _diameter_m(self, r_px, depth_m) -> float | None:
        i = self.intr
        if not depth_m or not i or not i.get("fx"):
            return None
        return 2.0 * r_px * depth_m / i["fx"]

    @staticmethod
    def _kind(diameter_m) -> str:
        if diameter_m is None:
            return "circle"
        for thresh, name in _DIAM_BUCKETS:
            if diameter_m >= thresh:
                return name
        return "circle"
