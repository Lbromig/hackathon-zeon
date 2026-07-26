"""AI verification agents — the "did it actually work?" layer.

Each agent takes evidence (camera frames + driver telemetry) and returns a
VerificationResult. The orchestrator calls these after each physical step and
decides pass / retry / stop.

Two rules hold for every agent here:

**Fail closed.** Absent or unreadable evidence is *not* a pass. These agents used
to return ``ok=True, confidence=0.0`` when they had nothing to go on, which meant
a disconnected camera and a successful step were indistinguishable to the
orchestrator — every step reported green while the cell did nothing. An agent
that cannot see returns ``ok=False`` and says why.

**Fusion beats a single channel.** Where two independent channels exist (joint
effort and vision), agreement is worth more than either alone, so a lone channel
is capped below what two agreeing channels can reach. The Lite 6 has no
force/torque sensor and the SDK exposes no gripper force, so "effort" here means
per-joint torque and servo current from the controller's rich report — see
``XArmDriver._read_effort``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# Confidence at or above this is a pass.
PASS_THRESHOLD = 0.60
# A single channel can never exceed this, so one signal alone cannot outrank
# two that agree.
SINGLE_CHANNEL_CAP = 0.70


@dataclass
class Evidence:
    """What the orchestrator observed around one step.

    ``telemetry`` is the snapshot *after* the step. ``before`` and ``during`` are
    what make a *change* measurable: a torque reading on its own says nothing,
    because "high torque" is indistinguishable from "cap still stuck" without the
    unscrewing peak to compare it against.
    """
    frames: dict[str, Any] = field(default_factory=dict)          # camera_id -> frame
    telemetry: dict[str, Any] = field(default_factory=dict)       # device_id -> status()
    before: dict[str, Any] = field(default_factory=dict)          # device_id -> status()
    before_frames: dict[str, Any] = field(default_factory=dict)   # camera_id -> frame
    during: list[dict[str, Any]] = field(default_factory=list)    # samples while running
    expected: dict[str, Any] = field(default_factory=dict)        # step params/targets


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


# --- shared helpers --------------------------------------------------------

def _ramp(value: float, lo: float, hi: float) -> float:
    """Map ``value`` onto 0..1 across the ``lo``..``hi`` band."""
    if hi <= lo:
        return 1.0 if value >= hi else 0.0
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


def _effort(snapshot: dict[str, Any], device_id: str) -> dict[str, list[float]] | None:
    """The effort block for one device out of a {device_id: status()} snapshot."""
    dev = snapshot.get(device_id)
    if not isinstance(dev, dict):
        return None
    eff = dev.get("effort")
    return eff if isinstance(eff, dict) else None


def _wrist_effort(snapshot: dict[str, Any], device_id: str) -> float | None:
    """Magnitude of the wrist joint's torque — the joint that turns the cap.

    The wrist is the last *real* joint. The driver already trims the report's
    fixed 7-slot array to the arm's axis count, so ``[-1]`` is J6 on a 6-axis arm
    rather than the trailing unused slot, which always reads 0.0.
    """
    eff = _effort(snapshot, device_id)
    if not eff:
        return None
    torque = eff.get("joints_torque") or []
    if not torque:
        return None
    return abs(float(torque[-1]))


def _fuse(channels: dict[str, float]) -> float:
    """Combine per-channel confidences. One channel alone is capped."""
    if not channels:
        return 0.0
    if len(channels) == 1:
        return min(next(iter(channels.values())), SINGLE_CHANNEL_CAP)
    return sum(channels.values()) / len(channels)


def _marker_centre(frame: Any, marker_id: int) -> tuple[float, float] | None:
    """Image-space centre of one marker, or None if it isn't visible.

    cv2 is imported lazily: core/ must stay importable (and unit-testable) on a
    box with no OpenCV, and a missing detector has to read as "no vision channel"
    rather than taking down the whole verification pass.
    """
    if frame is None:
        return None
    try:
        from ..perception.fiducials import FiducialDetector
    except Exception:
        return None
    try:
        for det in FiducialDetector().detect(frame):
            if det.marker_id == marker_id:
                return det.center
    except Exception:
        return None
    return None


def _frame_diagonal(frame: Any) -> float | None:
    shape = getattr(frame, "shape", None)
    if not shape or len(shape) < 2:
        return None
    h, w = float(shape[0]), float(shape[1])
    return (h * h + w * w) ** 0.5


# --- agents ----------------------------------------------------------------

class CapRemovedAgent(VerificationAgent):
    """Cap off? Torque collapse on the turning arm + the cap marker having moved.

    Effort channel: unscrewing loads the wrist, and that load *collapses* the
    moment the threads let go. We compare the peak seen while the step ran
    against the reading after it, so the pass condition is a drop, not a level.

    Vision channel: the cap carries a fiducial (calibration.markers, id 224).
    Against a static overview camera, the cap coming off shows up as that marker
    translating across the image. Pixel displacement normalised by the frame
    diagonal keeps this resolution-independent and needs no intrinsics.
    """
    name = "cap_removed"

    # Below this the wrist was never meaningfully loaded, so there is no
    # "unscrewing" to have finished — a drop from noise to noise proves nothing.
    MIN_UNSCREW_TORQUE_NM = 0.5
    DROP_LO, DROP_HI = 0.40, 0.80          # fraction of peak torque shed
    MOVE_LO, MOVE_HI = 0.02, 0.10          # marker travel, fraction of frame diagonal
    CAP_MARKER_ID = 224                    # markers.MARKER_MAP: tube_1_cap

    def verify(self, evidence: Evidence) -> VerificationResult:
        arm = evidence.expected.get("turning_arm", "right")
        cam = evidence.expected.get("overview_camera", "overview_cam")
        channels: dict[str, float] = {}
        data: dict[str, Any] = {"turning_arm": arm}
        notes: list[str] = []

        # --- effort channel ---
        final = _wrist_effort(evidence.telemetry, arm)
        samples = [t for t in (_wrist_effort(s, arm) for s in evidence.during) if t is not None]
        peak = max(samples) if samples else _wrist_effort(evidence.before, arm)
        if final is None or peak is None:
            notes.append(f"no wrist torque for {arm!r}")
        elif peak < self.MIN_UNSCREW_TORQUE_NM:
            notes.append(
                f"wrist never loaded (peak {peak:.2f} Nm < {self.MIN_UNSCREW_TORQUE_NM} Nm) "
                "— no unscrewing to confirm"
            )
            data["torque_peak_nm"] = peak
        else:
            drop = (peak - final) / peak
            channels["torque"] = _ramp(drop, self.DROP_LO, self.DROP_HI)
            data |= {"torque_peak_nm": peak, "torque_final_nm": final, "torque_drop": drop}

        # --- vision channel ---
        before_c = _marker_centre(evidence.before_frames.get(cam), self.CAP_MARKER_ID)
        after_c = _marker_centre(evidence.frames.get(cam), self.CAP_MARKER_ID)
        diag = _frame_diagonal(evidence.frames.get(cam))
        if before_c and after_c and diag:
            travel = (
                (after_c[0] - before_c[0]) ** 2 + (after_c[1] - before_c[1]) ** 2
            ) ** 0.5 / diag
            channels["vision"] = _ramp(travel, self.MOVE_LO, self.MOVE_HI)
            data["cap_marker_travel"] = travel
        else:
            notes.append(f"cap marker {self.CAP_MARKER_ID} not tracked on {cam!r}")

        if not channels:
            return VerificationResult(
                False, 0.0, "no usable evidence: " + "; ".join(notes), data)

        confidence = _fuse(channels)
        data["channels"] = channels
        detail = ", ".join(f"{k}={v:.2f}" for k, v in channels.items())
        if notes:
            detail += " (" + "; ".join(notes) + ")"
        return VerificationResult(confidence >= PASS_THRESHOLD, confidence, detail, data)


class GraspSecureAgent(VerificationAgent):
    """Grasp secure? Gripper closed onto the tube, not past it or short of it.

    A gripper that closed on nothing runs to its fully-closed width; a real grasp
    is held open by the tube's diameter. So the check is that the reported width
    sits in a band around the expected diameter — much smaller means the tube was
    missed or dropped, much larger means it was never gripped.
    """
    name = "grasp_secure"

    WIDTH_TOL_M = 0.004  # 4 mm either side of the expected tube diameter

    def verify(self, evidence: Evidence) -> VerificationResult:
        arm = evidence.expected.get("holding_arm", "right")
        expected_m = evidence.expected.get("grasp_width_m")
        dev = evidence.telemetry.get(arm)
        width = dev.get("gripper_width_m") if isinstance(dev, dict) else None

        if width is None:
            return VerificationResult(
                False, 0.0, f"no gripper width reported by {arm!r} — cannot confirm a grasp",
                {"holding_arm": arm})
        if expected_m is None:
            return VerificationResult(
                False, 0.0,
                "no expected grasp width supplied, so any width would 'pass' — refusing",
                {"holding_arm": arm, "width_m": width})

        error = abs(float(width) - float(expected_m))
        confidence = min(1.0 - _ramp(error, 0.0, self.WIDTH_TOL_M), SINGLE_CHANNEL_CAP)
        return VerificationResult(
            error <= self.WIDTH_TOL_M and confidence >= PASS_THRESHOLD,
            confidence,
            f"width {float(width) * 1000:.1f} mm vs expected {float(expected_m) * 1000:.1f} mm "
            f"(tol ±{self.WIDTH_TOL_M * 1000:.0f} mm)",
            {"holding_arm": arm, "width_m": float(width),
             "expected_m": float(expected_m), "error_m": error},
        )


class TubeAlignedAgent(VerificationAgent):
    """Tube presented where the pipette expects it (pose check before the OT moves).

    A pose-tolerance check against an explicitly supplied target. The
    twin-anchored version arrives with the teachpoint work; until a target is
    passed in there is nothing to compare against, and inventing one here would
    be the same lie the stub told.
    """
    name = "tube_aligned"

    POS_TOL_MM = 3.0

    def verify(self, evidence: Evidence) -> VerificationResult:
        arm = evidence.expected.get("holding_arm", "right")
        target = evidence.expected.get("present_pose_mm")
        dev = evidence.telemetry.get(arm)
        pose = dev.get("pose") if isinstance(dev, dict) else None

        if not isinstance(pose, dict):
            return VerificationResult(
                False, 0.0, f"no live pose from {arm!r} — cannot confirm alignment",
                {"holding_arm": arm})
        if not isinstance(target, dict):
            return VerificationResult(
                False, 0.0,
                "no expected present pose supplied — refusing to pass on no target",
                {"holding_arm": arm})

        try:
            error = sum(
                (float(pose[a]) - float(target[a])) ** 2 for a in ("x", "y", "z")
            ) ** 0.5
        except (KeyError, TypeError, ValueError) as e:
            return VerificationResult(
                False, 0.0, f"pose/target not comparable: {e}", {"holding_arm": arm})

        confidence = min(1.0 - _ramp(error, 0.0, self.POS_TOL_MM), SINGLE_CHANNEL_CAP)
        return VerificationResult(
            error <= self.POS_TOL_MM and confidence >= PASS_THRESHOLD,
            confidence,
            f"TCP {error:.2f} mm from the presented pose (tol {self.POS_TOL_MM:.0f} mm)",
            {"holding_arm": arm, "error_mm": error},
        )


class AspirationAgent(VerificationAgent):
    """Aspiration happened? The OT's own plunger/volume report is the ground truth.

    A vision level-drop estimate restates the same fact far less precisely for
    tubes this narrow, so we check the reported volume against what was asked for.
    """
    name = "aspiration_ok"

    VOLUME_TOL_FRAC = 0.10  # within 10 % of the requested volume

    def verify(self, evidence: Evidence) -> VerificationResult:
        ot = evidence.expected.get("liquid_handler", "ot")
        requested = evidence.expected.get("volume_ul")
        dev = evidence.telemetry.get(ot)
        reported = dev.get("last_aspirated_ul") if isinstance(dev, dict) else None

        if requested is None:
            return VerificationResult(
                False, 0.0, "no requested volume supplied — nothing to verify against",
                {"liquid_handler": ot})
        if float(requested) <= 0:
            return VerificationResult(
                False, 0.0, f"requested volume {float(requested)} µl is not positive",
                {"liquid_handler": ot})
        if reported is None:
            return VerificationResult(
                False, 0.0,
                f"{ot!r} reported no aspirated volume — cannot confirm liquid moved",
                {"liquid_handler": ot, "requested_ul": float(requested)})

        requested_f, reported_f = float(requested), float(reported)
        error_frac = abs(reported_f - requested_f) / requested_f
        confidence = min(1.0 - _ramp(error_frac, 0.0, self.VOLUME_TOL_FRAC), SINGLE_CHANNEL_CAP)
        return VerificationResult(
            error_frac <= self.VOLUME_TOL_FRAC and confidence >= PASS_THRESHOLD,
            confidence,
            f"aspirated {reported_f:.1f} µl of {requested_f:.1f} µl requested "
            f"({error_frac * 100:.1f}% off)",
            {"liquid_handler": ot, "requested_ul": requested_f,
             "reported_ul": reported_f, "error_frac": error_frac},
        )


AGENTS: dict[str, VerificationAgent] = {
    a.name: a for a in (
        CapRemovedAgent(), GraspSecureAgent(), TubeAlignedAgent(), AspirationAgent(),
    )
}
