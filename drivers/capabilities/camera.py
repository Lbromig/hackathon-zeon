"""Capability interface for cameras used by the verification agents."""
from __future__ import annotations

from abc import abstractmethod
from typing import Any

from ..base import InstrumentDriver, InstrumentKind


class CameraDriver(InstrumentDriver):
    kind = InstrumentKind.CAMERA
    has_depth: bool = False   # RGB-D cameras (RealSense) override to True

    @abstractmethod
    def capture(self) -> "Any":
        """Return a single frame as an HxWx3 BGR numpy array."""

    @abstractmethod
    def capture_jpeg(self, quality: int = 85) -> bytes:
        """Return a single frame encoded as JPEG bytes (for the UI / API)."""

    # --- optional RGB-D extensions (default: unsupported; RGB-D drivers override) ---
    def capture_depth(self) -> "Any":
        """Depth map aligned to the colour frame, HxW float32 in **metres**."""
        raise NotImplementedError(f"{self.device_id}: no depth stream")

    def capture_rgbd(self) -> "tuple[Any, Any]":
        """(colour BGR, depth metres) from the same aligned frameset."""
        return self.capture(), self.capture_depth()

    def intrinsics(self) -> "dict[str, Any] | None":
        """Pinhole intrinsics {fx, fy, cx, cy, width, height, coeffs}. None if unknown.
        RGB-D cameras report factory intrinsics, so no ChArUco pass is needed for them."""
        return None
