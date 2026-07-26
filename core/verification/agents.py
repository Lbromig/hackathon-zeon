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
# Two channels at least this far apart are treated as contradicting each other
# rather than averaged. TUNABLE: set so that a confident negative (0.0) always
# conflicts with any channel that would otherwise pass on its own. No bench
# measurement backs the exact value.
DISAGREEMENT_SPREAD = 0.50


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
    """Combine per-channel confidences. One channel alone is capped.

    Only valid once the channels are known to roughly concur. Averaging
    contradictory channels produces a middling number that describes neither of
    them, so callers must check ``_conflict`` first.
    """
    if not channels:
        return 0.0
    if len(channels) == 1:
        return min(next(iter(channels.values())), SINGLE_CHANNEL_CAP)
    return sum(channels.values()) / len(channels)


def _conflict(channels: dict[str, float]) -> tuple[str, str] | None:
    """The most extreme pair of channels, when they contradict each other.

    Averaging assumes the channels are measuring the same thing and roughly
    agreeing. When they are far apart that assumption is false: two independent
    observations of one event are telling opposite stories, and the mean hides
    precisely the case an operator needs to see.

    The concrete failure this prevents: depth is the only channel that measures
    the tube rather than a proxy for it, and a confident CAP_ON scores 0.0. With
    torque at 0.9 and vision at 0.9 the mean is 0.600, which clears
    PASS_THRESHOLD exactly, so the agent used to report the cap removed while the
    one channel that actually looked at the tube said it was still on.

    Returns ``(higher_channel, lower_channel)`` or None.
    """
    if len(channels) < 2:
        return None
    hi = max(channels, key=lambda k: channels[k])
    lo = min(channels, key=lambda k: channels[k])
    if channels[hi] - channels[lo] >= DISAGREEMENT_SPREAD:
        return hi, lo
    return None


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


def _norm(delta: tuple[float, float]) -> float:
    """Euclidean length of an image-space displacement."""
    return (delta[0] * delta[0] + delta[1] * delta[1]) ** 0.5


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
        # Marker displacement alone cannot tell "the cap moved" from "the camera
        # moved" or "somebody nudged the bench". Both translate the cap marker
        # across the image identically. When a static datum marker is supplied we
        # subtract its displacement, which is textbook common-mode rejection: the
        # part of the motion shared by a fixed reference is not the cap moving.
        before_c = _marker_centre(evidence.before_frames.get(cam), self.CAP_MARKER_ID)
        after_c = _marker_centre(evidence.frames.get(cam), self.CAP_MARKER_ID)
        diag = _frame_diagonal(evidence.frames.get(cam))
        if before_c and after_c and diag:
            delta = (after_c[0] - before_c[0], after_c[1] - before_c[1])
            datum_id = evidence.expected.get("datum_marker_id")
            referenced = False
            if datum_id is not None:
                d_before = _marker_centre(evidence.before_frames.get(cam), int(datum_id))
                d_after = _marker_centre(evidence.frames.get(cam), int(datum_id))
                if d_before and d_after:
                    d_delta = (d_after[0] - d_before[0], d_after[1] - d_before[1])
                    data["datum_marker_travel"] = _norm(d_delta) / diag
                    delta = (delta[0] - d_delta[0], delta[1] - d_delta[1])
                    referenced = True
                else:
                    notes.append(
                        f"datum marker {int(datum_id)} not tracked on {cam!r}, so cap "
                        "travel is unreferenced and a camera or bench move would read "
                        "as the cap moving"
                    )
            else:
                notes.append(
                    "no datum_marker_id supplied, so cap travel is unreferenced and "
                    "cannot be told apart from a camera or bench move"
                )
            travel = _norm(delta) / diag
            channels["vision"] = _ramp(travel, self.MOVE_LO, self.MOVE_HI)
            data["cap_marker_travel"] = travel
            data["vision_datum_referenced"] = referenced
        else:
            notes.append(f"cap marker {self.CAP_MARKER_ID} not tracked on {cam!r}")

        # --- depth channel ---
        # A third, independent channel, and the only one that measures the thing
        # itself rather than a proxy for it. The marker channel needs a fiducial
        # on the cap to stay stuck and in view, which is the first thing a wet
        # bench takes away; the torque channel infers from the arm rather than
        # observing the tube. Height at the mouth is geometry: uncapping uncovers
        # a surface 15 to 20 mm further away whatever the lighting does.
        #
        # Needs a reference captured while capped, so it is silent rather than
        # failing when one was never taken. See depth_height.py.
        depth_channel, depth_data, depth_note = self._depth_channel(evidence)
        # Record what depth saw even when it abstains. An inconclusive read is
        # when the measured delta and the valid-pixel fraction are most worth
        # having, because they are what tell you the region drifted off the
        # mouth rather than the cap not moving.
        data |= depth_data
        if depth_channel is not None:
            channels["depth"] = depth_channel
        elif depth_note:
            notes.append(depth_note)

        if not channels:
            return VerificationResult(
                False, 0.0, "no usable evidence: " + "; ".join(notes), data)

        data["channels"] = channels
        summary = ", ".join(f"{k}={v:.2f}" for k, v in channels.items())

        # Contradiction is its own outcome, ahead of fusion. Two channels this
        # far apart cannot both be right, and their mean describes neither, so
        # there is no confidence to report and nothing for the orchestrator to
        # retry blindly. Stop and surface it.
        conflict = _conflict(channels)
        if conflict is not None:
            hi, lo = conflict
            spread = channels[hi] - channels[lo]
            data["disagreement"] = {
                "high": hi, "low": lo, "spread": spread,
                "would_have_fused_to": _fuse(channels),
            }
            detail = (
                f"channels contradict: {hi}={channels[hi]:.2f} vs {lo}={channels[lo]:.2f} "
                f"(spread {spread:.2f} >= {DISAGREEMENT_SPREAD:.2f}). Independent "
                f"observations of the same event disagree, so this needs a human "
                f"rather than an average [{summary}]"
            )
            if notes:
                detail += " (" + "; ".join(notes) + ")"
            return VerificationResult(False, 0.0, detail, data)

        confidence = _fuse(channels)
        detail = summary
        if notes:
            detail += " (" + "; ".join(notes) + ")"
        return VerificationResult(confidence >= PASS_THRESHOLD, confidence, detail, data)

    def _depth_channel(
        self, evidence: Evidence
    ) -> tuple[float | None, dict[str, Any], str]:
        """Confidence from the height delta at the tube mouth.

        Returns (confidence or None, data, note). None means the channel had
        nothing to say, which is not a failure: the other channels still decide.
        """
        from .depth_height import HeightStat, Verdict, compare, measure_region

        frame = evidence.frames.get("depth")
        scale = evidence.expected.get("depth_scale")
        roi = evidence.expected.get("cap_roi")
        reference = evidence.expected.get("cap_reference")

        if frame is None or scale is None or roi is None or reference is None:
            return None, {}, "no depth reference for the tube mouth"
        if not isinstance(reference, HeightStat):
            return None, {}, "cap_reference is not a HeightStat"

        try:
            observed = measure_region(frame, float(scale), tuple(roi))
        except Exception as exc:
            return None, {}, f"depth measurement failed: {exc}"

        check = compare(reference, observed)
        info = {
            "depth_delta_mm": round(check.delta_mm, 2),
            "depth_verdict": check.verdict.value,
            "depth_valid_fraction": round(observed.valid_fraction, 3),
        }
        # UNKNOWN contributes nothing rather than contributing zero. A zero would
        # drag the fused average down and let an unreadable region veto two
        # channels that did see something.
        if check.verdict is Verdict.UNKNOWN:
            return None, info, f"depth inconclusive: {check.detail}"
        if check.verdict is Verdict.CAP_ON:
            return 0.0, info, ""
        return check.confidence, info, ""


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
