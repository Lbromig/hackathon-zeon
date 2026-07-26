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
    """Cap off? Measured as a height delta at the tube mouth.

    Uncapping uncovers a surface roughly 15 to 20 mm further from the camera.
    That is geometry, so unlike a threads-visible classifier it does not depend
    on lighting, on a marker surviving a wet bench, or on the cap not being
    shiny. See core/verification/depth_height.py.

    Needs two things in evidence:

        evidence.frames["depth"]        uint16 depth image from the camera
        evidence.telemetry["depth_scale"]   metres per count, from the device
        evidence.telemetry["cap_roi"]       (x, y, w, h) at the tube mouth
        evidence.telemetry["cap_reference"] HeightStat captured while capped

    Anything missing returns not-ok with zero confidence. This agent never
    reports success because it had nothing to look at.
    """
    name = "cap_removed"

    def verify(self, evidence: Evidence) -> VerificationResult:
        from .depth_height import HeightStat, compare, measure_region

        depth = evidence.frames.get("depth")
        scale = evidence.telemetry.get("depth_scale")
        roi = evidence.telemetry.get("cap_roi")
        reference = evidence.telemetry.get("cap_reference")

        missing = [
            label
            for label, value in (
                ("depth frame", depth),
                ("depth_scale", scale),
                ("cap_roi", roi),
                ("cap_reference", reference),
            )
            if value is None
        ]
        if missing:
            return VerificationResult(
                ok=False, confidence=0.0,
                detail=f"cannot verify, missing: {', '.join(missing)}",
            )
        if not isinstance(reference, HeightStat):
            return VerificationResult(
                ok=False, confidence=0.0,
                detail="cap_reference must be a HeightStat captured while capped",
            )

        try:
            observed = measure_region(depth, float(scale), tuple(roi))  # type: ignore[arg-type]
        except Exception as exc:
            return VerificationResult(
                ok=False, confidence=0.0, detail=f"measurement failed: {exc}"
            )

        check = compare(reference, observed)
        return VerificationResult(
            ok=check.ok,
            confidence=check.confidence,
            detail=check.detail,
            data={
                "verdict": check.verdict.value,
                "delta_mm": round(check.delta_mm, 2),
                "observed": observed.describe(),
                "valid_fraction": round(observed.valid_fraction, 3),
            },
        )


class GraspSecureAgent(VerificationAgent):
    """Grasp secure? gripper width within expected band + tube present in frame."""
    name = "grasp_secure"

    def verify(self, evidence: Evidence) -> VerificationResult:
        return VerificationResult(
            ok=False, confidence=0.0,
            detail="not implemented; fails closed so an unwritten check "
                   "cannot report success",
        )


class TubeAlignedAgent(VerificationAgent):
    """Tube presented at the pose the pipette expects (pose check before OT moves)."""
    name = "tube_aligned"

    def verify(self, evidence: Evidence) -> VerificationResult:
        return VerificationResult(
            ok=False, confidence=0.0,
            detail="not implemented; fails closed so an unwritten check "
                   "cannot report success",
        )


class AspirationAgent(VerificationAgent):
    """Aspiration happened? liquid level drop in tube / OT volume report."""
    name = "aspiration_ok"

    def verify(self, evidence: Evidence) -> VerificationResult:
        return VerificationResult(
            ok=False, confidence=0.0,
            detail="not implemented; fails closed so an unwritten check "
                   "cannot report success",
        )


AGENTS: dict[str, VerificationAgent] = {
    a.name: a for a in (
        CapRemovedAgent(), GraspSecureAgent(), TubeAlignedAgent(), AspirationAgent(),
    )
}
