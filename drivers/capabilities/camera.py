"""Capability interface for cameras used by the verification agents."""
from __future__ import annotations

from abc import abstractmethod
from typing import Any

from ..base import InstrumentDriver, InstrumentKind


class CameraDriver(InstrumentDriver):
    kind = InstrumentKind.CAMERA

    @abstractmethod
    def capture(self) -> "Any":
        """Return a single frame as an HxWx3 BGR numpy array."""

    @abstractmethod
    def capture_jpeg(self, quality: int = 85) -> bytes:
        """Return a single frame encoded as JPEG bytes (for the UI / API)."""
