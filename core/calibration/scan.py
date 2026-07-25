"""Integration point for the on-arm-camera scan package.

The camera mounted on the arm orbits an instrument capturing multi-view frames; the
scan package reconstructs the object and returns its pose in the world frame. Drop the
real package behind `ScanAdapter` — everything upstream is package-agnostic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np

from ..worldmodel.entities import Transform, from_xyz_rpy


class OrbitPlanner:
    """Generate on-arm camera poses that orbit a target point (for the scan)."""

    def __init__(self, radius_m: float = 0.25, height_m: float = 0.15, n: int = 12):
        self.radius, self.height, self.n = radius_m, height_m, n

    def poses_around(self, center_xyz) -> list[Transform]:
        cx, cy, cz = center_xyz
        poses = []
        for k in range(self.n):
            a = 2 * np.pi * k / self.n
            x, y = cx + self.radius * np.cos(a), cy + self.radius * np.sin(a)
            # camera looks inward toward the center; yaw points at target
            poses.append(from_xyz_rpy(x=x, y=y, z=cz + self.height, yaw=a + np.pi))
        return poses


class ScanAdapter(ABC):
    """Wrap the reconstruction package here."""

    @abstractmethod
    def run_scan(self, instrument_id: str, frames_dir: Path) -> Path:
        """Run reconstruction over captured frames; return the artifacts dir."""

    @abstractmethod
    def load_pose(self, artifacts_dir: Path) -> Transform:
        """Read the reconstructed object's pose in the world frame."""


class PlaceholderScanAdapter(ScanAdapter):
    """No-op stand-in until the real package is wired in. Returns identity pose."""

    def run_scan(self, instrument_id: str, frames_dir: Path) -> Path:
        out = frames_dir / "reconstruction"
        out.mkdir(parents=True, exist_ok=True)
        (out / "NOTE.txt").write_text(
            f"TODO: plug real scan package here for {instrument_id}. "
            "It should consume frames + ArUco + ruler and emit an object pose."
        )
        return out

    def load_pose(self, artifacts_dir: Path) -> Transform:
        # TODO: parse the package's output (e.g. transform.json) into a 4x4.
        return from_xyz_rpy()
