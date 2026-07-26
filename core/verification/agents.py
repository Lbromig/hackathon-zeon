"""AI verification agents — the "did it actually work?" layer.

Each agent takes evidence (the digital-twin snapshot + camera frames + driver
telemetry) and returns a VerificationResult. The orchestrator/agent engine calls
these after each physical step and decides pass / retry / stop.

Design: predicates are **twin queries first** (geometry we already track), fused with
driver **telemetry** (gripper width, torque, aspirated volume) when available. This keeps
them deterministic and unit-testable without hardware, and degrades gracefully — a missing
signal lowers confidence rather than crashing. Vision fusion (render-compare) can be layered
on later without changing this interface.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..worldmodel import EntityKind, WorldModel

# --- tuning (metres) ---------------------------------------------------------
ALIGN_TAU_M = 0.015     # tube presented "under" the nozzle within this distance
CAP_SEP_TAU_M = 0.020   # cap counts as removed once this far from the tube


@dataclass
class Evidence:
    world: WorldModel | None = None                          # live twin snapshot
    frames: dict[str, Any] = field(default_factory=dict)     # camera_id -> frame/bytes
    telemetry: dict[str, Any] = field(default_factory=dict)  # device_id -> status() dict


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


# --- twin helpers ------------------------------------------------------------
def _active_tube(wm: WorldModel):
    """The tube being manipulated: prefer one held by a tool, else the only tube."""
    tubes = wm.by_kind(EntityKind.TUBE)
    if not tubes:
        return None
    held = [t for t in tubes if _parent_kind(wm, t) == EntityKind.TOOL]
    if len(held) == 1:
        return held[0]
    return tubes[0] if len(tubes) == 1 else None


def _cap_of(wm: WorldModel, tube_id: str):
    caps = [c for c in wm.by_kind(EntityKind.CAP) if c.id == f"{tube_id}_cap"]
    return caps[0] if caps else None


def _parent_kind(wm: WorldModel, entity) -> EntityKind | None:
    if entity.parent is None or entity.parent not in wm.entities:
        return None
    return wm.get(entity.parent).kind


def _missing_world(name: str) -> VerificationResult:
    return VerificationResult(ok=False, confidence=0.0, detail=f"{name}: no twin available")


# --- agents ------------------------------------------------------------------
class CapRemovedAgent(VerificationAgent):
    """Cap off? It has been reparented off the tube and moved clear.
    Fused with a torque drop on the turning arm when telemetry provides it."""
    name = "cap_removed"

    def verify(self, ev: Evidence) -> VerificationResult:
        wm = ev.world
        if wm is None:
            return _missing_world(self.name)
        tube = _active_tube(wm)
        cap = _cap_of(wm, tube.id) if tube else None
        if tube is None or cap is None:
            return VerificationResult(False, 0.0, "no tube/cap in twin")
        reparented = cap.parent != tube.id
        sep = wm.distance(cap.id, tube.id) if reparented else 0.0
        ok = reparented and sep > CAP_SEP_TAU_M
        conf = 0.6 if ok else 0.0
        # telemetry bonus: torque drop reported by the turning arm
        if ok and any(isinstance(t, dict) and t.get("torque_drop") for t in ev.telemetry.values()):
            conf = min(1.0, conf + 0.4)
        return VerificationResult(ok, conf,
            f"cap parent={cap.parent}, sep={sep*1000:.0f}mm",
            {"separation_m": sep, "reparented": reparented})


class GraspSecureAgent(VerificationAgent):
    """Grasp secure? The tube is reparented onto a gripper/tool, and (if reported)
    the gripper width sits in the expected band for the tube diameter."""
    name = "grasp_secure"

    def verify(self, ev: Evidence) -> VerificationResult:
        wm = ev.world
        if wm is None:
            return _missing_world(self.name)
        tube = _active_tube(wm)
        if tube is None:
            return VerificationResult(False, 0.0, "no tube in twin")
        held = _parent_kind(wm, tube) == EntityKind.TOOL
        conf = 0.6 if held else 0.0
        detail = f"tube parent={tube.parent}"
        # telemetry bonus: gripper width within [0.6*d, 1.05*d] of tube diameter
        d = tube.dims.get("diameter")
        widths = [t.get("gripper_width") for t in ev.telemetry.values()
                  if isinstance(t, dict) and t.get("gripper_width") is not None]
        if held and d and widths:
            w = widths[0]
            if 0.6 * d <= w <= 1.05 * d:
                conf = min(1.0, conf + 0.4)
                detail += f", width={w*1000:.0f}mm ok"
            else:
                conf = max(0.0, conf - 0.3)
                detail += f", width={w*1000:.0f}mm out of band"
        return VerificationResult(held, conf, detail, {"held": held})


class TubeAlignedAgent(VerificationAgent):
    """Tube presented where the pipette expects it: within ALIGN_TAU of the nozzle."""
    name = "tube_aligned"

    def verify(self, ev: Evidence) -> VerificationResult:
        wm = ev.world
        if wm is None:
            return _missing_world(self.name)
        tube = _active_tube(wm)
        nozzles = wm.by_kind(EntityKind.PIPETTE_CHANNEL)
        if tube is None or not nozzles:
            return VerificationResult(False, 0.0, "no tube/nozzle in twin")
        dist = wm.distance(tube.id, nozzles[0].id)
        ok = dist < ALIGN_TAU_M
        # linear confidence: 1.0 at 0, 0 at 2*tau
        conf = max(0.0, 1.0 - dist / (2 * ALIGN_TAU_M)) if ok else 0.0
        return VerificationResult(ok, conf,
            f"tube-nozzle dist={dist*1000:.0f}mm (tau={ALIGN_TAU_M*1000:.0f})",
            {"distance_m": dist})


class AspirationAgent(VerificationAgent):
    """Aspiration happened? Positive aspirated volume from the OT (telemetry) or the
    nozzle entity's tracked state."""
    name = "aspiration_ok"

    def verify(self, ev: Evidence) -> VerificationResult:
        vol = None
        for t in ev.telemetry.values():
            if isinstance(t, dict) and t.get("aspirated_volume_ul") is not None:
                vol = t["aspirated_volume_ul"]
                break
        if vol is None and ev.world is not None:
            noz = ev.world.by_kind(EntityKind.PIPETTE_CHANNEL)
            if noz:
                vol = noz[0].state.get("aspirated_volume_ul")
        if vol is None:
            return VerificationResult(False, 0.0, "no volume reported")
        ok = vol > 0
        return VerificationResult(ok, 0.9 if ok else 0.0,
            f"aspirated {vol} uL", {"aspirated_volume_ul": vol})


AGENTS: dict[str, VerificationAgent] = {
    a.name: a for a in (
        CapRemovedAgent(), GraspSecureAgent(), TubeAlignedAgent(), AspirationAgent(),
    )
}
