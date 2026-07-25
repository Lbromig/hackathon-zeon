"""AI verification agents — the "did it actually work?" layer.

Each agent takes evidence (camera frames + driver telemetry) and returns a
VerificationResult. The orchestrator calls these after each physical step and
decides pass / retry / stop.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Evidence:
    frames: dict[str, Any] = field(default_factory=dict)   # camera_id -> frame/bytes
    telemetry: dict[str, Any] = field(default_factory=dict)  # driver.status() snapshots


@dataclass
class VerificationResult:
    ok: bool
    confidence: float
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class VerificationAgent(ABC):
    name: str

    @abstractmethod
    def verify(self, evidence: Evidence) -> VerificationResult: ...


class CapRemovedAgent(VerificationAgent):
    """Cap off? torque drop on the turning arm + threads visible in frame."""
    name = "cap_removed"

    def verify(self, evidence: Evidence) -> VerificationResult:
        # TODO: vision (threads/cap-gone classifier) + force telemetry fusion.
        return VerificationResult(ok=True, confidence=0.0, detail="stub")


class GraspSecureAgent(VerificationAgent):
    """Grasp secure? gripper width within expected band + tube present in frame."""
    name = "grasp_secure"

    def verify(self, evidence: Evidence) -> VerificationResult:
        return VerificationResult(ok=True, confidence=0.0, detail="stub")


class TubeAlignedAgent(VerificationAgent):
    """Tube presented at the pose the pipette expects (pose check before OT moves)."""
    name = "tube_aligned"

    def verify(self, evidence: Evidence) -> VerificationResult:
        return VerificationResult(ok=True, confidence=0.0, detail="stub")


class AspirationAgent(VerificationAgent):
    """Aspiration happened? liquid level drop in tube / OT volume report."""
    name = "aspiration_ok"

    def verify(self, evidence: Evidence) -> VerificationResult:
        return VerificationResult(ok=True, confidence=0.0, detail="stub")


AGENTS: dict[str, VerificationAgent] = {
    a.name: a for a in (
        CapRemovedAgent(), GraspSecureAgent(), TubeAlignedAgent(), AspirationAgent(),
    )
}
