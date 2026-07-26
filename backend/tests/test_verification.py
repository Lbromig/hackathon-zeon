"""Verification agents: fail-closed contract, and each channel's real decision.

The single most important assertion here is `test_empty_evidence_never_passes`.
These agents previously returned ok=True with confidence 0.0 when they had no
evidence, which made "camera unplugged" and "step succeeded" identical to the
orchestrator — the whole verify/retry loop reported green while nothing moved.
"""
import numpy as np
import pytest

from core.verification.agents import (
    AGENTS,
    PASS_THRESHOLD,
    SINGLE_CHANNEL_CAP,
    CapRemovedAgent,
    Evidence,
    VerificationResult,
)


def test_all_agents_registered():
    assert set(AGENTS) == {"cap_removed", "grasp_secure", "tube_aligned", "aspiration_ok"}


def test_agents_return_verification_results():
    ev = Evidence()
    for name, agent in AGENTS.items():
        result = agent.verify(ev)
        assert isinstance(result, VerificationResult), name
        assert isinstance(result.ok, bool)
        assert 0.0 <= result.confidence <= 1.0


def test_empty_evidence_never_passes():
    """No evidence must never read as success, and must say why."""
    for name, agent in AGENTS.items():
        result = agent.verify(Evidence())
        assert result.ok is False, f"{name} passed on empty evidence"
        assert result.confidence == 0.0, name
        assert result.detail, f"{name} gave no reason"


# --- cap_removed: effort channel ------------------------------------------

def _arm_status(torque_nm: float, arm: str = "right") -> dict:
    """A status() snapshot whose wrist (last real joint) carries `torque_nm`."""
    return {arm: {"effort": {"joints_torque": [0.1, -6.4, -16.2, 0.2, 1.3, torque_nm],
                             "currents": [0.0] * 6}}}


def _cap_evidence(peak: float, final: float, arm: str = "right") -> Evidence:
    return Evidence(
        telemetry=_arm_status(final, arm),
        during=[_arm_status(peak, arm)],
        before=_arm_status(0.2, arm),
        expected={"turning_arm": arm},
    )


def test_cap_removed_passes_on_torque_collapse():
    # Unscrewing loaded the wrist to 4 Nm; once the threads let go it fell to 0.3.
    result = AGENTS["cap_removed"].verify(_cap_evidence(peak=4.0, final=0.3))
    assert result.ok is True
    assert result.confidence >= PASS_THRESHOLD
    assert result.data["torque_drop"] > 0.9


def test_cap_removed_fails_while_cap_still_tight():
    # Still pushing hard against the threads: no collapse, so no removal.
    result = AGENTS["cap_removed"].verify(_cap_evidence(peak=4.0, final=3.9))
    assert result.ok is False
    assert result.data["torque_drop"] < 0.1


def test_cap_removed_refuses_when_wrist_was_never_loaded():
    """A drop from noise to noise is not evidence of unscrewing."""
    result = AGENTS["cap_removed"].verify(_cap_evidence(peak=0.2, final=0.01))
    assert result.ok is False
    assert "never loaded" in result.detail


def test_single_channel_is_capped_below_certainty():
    """Torque alone, however clean, must not outrank two agreeing channels."""
    result = AGENTS["cap_removed"].verify(_cap_evidence(peak=10.0, final=0.0))
    assert result.confidence <= SINGLE_CHANNEL_CAP
    assert set(result.data["channels"]) == {"torque"}


def test_peak_falls_back_to_before_when_no_during_samples():
    ev = Evidence(
        telemetry=_arm_status(0.2),
        before=_arm_status(5.0),
        expected={"turning_arm": "right"},
    )
    result = AGENTS["cap_removed"].verify(ev)
    assert result.data["torque_peak_nm"] == 5.0


def test_wrist_is_last_real_joint_not_the_unused_slot():
    """The controller reports a fixed 7-slot array; slot 7 is always 0.0 on a
    6-axis arm. Reading it as the wrist would report every step as unloaded."""
    ev = _cap_evidence(peak=4.0, final=0.3)
    assert ev.telemetry["right"]["effort"]["joints_torque"][-1] == 0.3
    assert AGENTS["cap_removed"].verify(ev).data["torque_final_nm"] == 0.3


# --- cap_removed: vision channel ------------------------------------------

cv2 = pytest.importorskip("cv2", reason="vision channel needs opencv-contrib")


def _frame_with_marker(marker_id: int, at: tuple[int, int], size: int = 120,
                       shape: tuple[int, int] = (480, 640)) -> np.ndarray:
    """A white frame with one tag36h11 marker pasted at `at` (top-left)."""
    from core.perception.fiducials import TAG_FAMILY

    dic = cv2.aruco.getPredefinedDictionary(TAG_FAMILY)
    tag = cv2.aruco.generateImageMarker(dic, marker_id, size)
    frame = np.full((*shape, 3), 255, np.uint8)
    x, y = at
    frame[y:y + size, x:x + size] = cv2.cvtColor(tag, cv2.COLOR_GRAY2BGR)
    return frame


def test_vision_channel_sees_the_cap_marker_move():
    mid = CapRemovedAgent.CAP_MARKER_ID
    ev = Evidence(
        before_frames={"overview_cam": _frame_with_marker(mid, (100, 100))},
        frames={"overview_cam": _frame_with_marker(mid, (400, 300))},
        telemetry=_arm_status(0.3),
        during=[_arm_status(4.0)],
        expected={"turning_arm": "right", "overview_camera": "overview_cam"},
    )
    result = AGENTS["cap_removed"].verify(ev)
    assert set(result.data["channels"]) == {"torque", "vision"}
    assert result.data["cap_marker_travel"] > CapRemovedAgent.MOVE_HI
    assert result.ok is True
    # Two agreeing channels clear the single-channel ceiling.
    assert result.confidence > SINGLE_CHANNEL_CAP


def test_vision_channel_reports_a_cap_that_never_moved():
    mid = CapRemovedAgent.CAP_MARKER_ID
    still = (100, 100)
    ev = Evidence(
        before_frames={"overview_cam": _frame_with_marker(mid, still)},
        frames={"overview_cam": _frame_with_marker(mid, still)},
        telemetry=_arm_status(3.9),
        during=[_arm_status(4.0)],
        expected={"turning_arm": "right", "overview_camera": "overview_cam"},
    )
    result = AGENTS["cap_removed"].verify(ev)
    assert result.data["cap_marker_travel"] == pytest.approx(0.0, abs=1e-3)
    assert result.ok is False


# --- grasp_secure ----------------------------------------------------------

def test_grasp_secure_passes_when_jaws_held_open_by_the_tube():
    ev = Evidence(telemetry={"left": {"gripper_width_m": 0.0153}},
                  expected={"holding_arm": "left", "grasp_width_m": 0.0153})
    assert AGENTS["grasp_secure"].verify(ev).ok is True


def test_grasp_secure_fails_when_gripper_closed_on_nothing():
    ev = Evidence(telemetry={"left": {"gripper_width_m": 0.0}},
                  expected={"holding_arm": "left", "grasp_width_m": 0.0153})
    result = AGENTS["grasp_secure"].verify(ev)
    assert result.ok is False
    assert result.data["error_m"] == pytest.approx(0.0153)


def test_grasp_secure_refuses_without_an_expected_width():
    """With no expected width any reading would 'pass' — that is the old bug."""
    ev = Evidence(telemetry={"left": {"gripper_width_m": 0.0}},
                  expected={"holding_arm": "left"})
    result = AGENTS["grasp_secure"].verify(ev)
    assert result.ok is False
    assert "no expected grasp width" in result.detail


def test_grasp_secure_reports_no_width_feedback_distinctly():
    """A binary gripper reports None, which must not read as a zero-width grasp."""
    ev = Evidence(telemetry={"left": {"gripper_width_m": None}},
                  expected={"holding_arm": "left", "grasp_width_m": 0.0153})
    result = AGENTS["grasp_secure"].verify(ev)
    assert result.ok is False
    assert "no gripper width" in result.detail


# --- tube_aligned ----------------------------------------------------------

def test_tube_aligned_passes_within_tolerance():
    ev = Evidence(telemetry={"left": {"pose": {"x": 100.0, "y": 50.0, "z": 200.0}}},
                  expected={"holding_arm": "left",
                            "present_pose_mm": {"x": 101.0, "y": 50.0, "z": 200.0}})
    assert AGENTS["tube_aligned"].verify(ev).ok is True


def test_tube_aligned_fails_outside_tolerance():
    ev = Evidence(telemetry={"left": {"pose": {"x": 100.0, "y": 50.0, "z": 200.0}}},
                  expected={"holding_arm": "left",
                            "present_pose_mm": {"x": 130.0, "y": 50.0, "z": 200.0}})
    result = AGENTS["tube_aligned"].verify(ev)
    assert result.ok is False
    assert result.data["error_mm"] == pytest.approx(30.0)


def test_tube_aligned_refuses_without_a_target():
    ev = Evidence(telemetry={"left": {"pose": {"x": 0.0, "y": 0.0, "z": 0.0}}},
                  expected={"holding_arm": "left"})
    assert AGENTS["tube_aligned"].verify(ev).ok is False


# --- aspiration_ok ---------------------------------------------------------

def test_aspiration_passes_when_reported_volume_matches():
    ev = Evidence(telemetry={"ot": {"last_aspirated_ul": 198.0}},
                  expected={"liquid_handler": "ot", "volume_ul": 200.0})
    assert AGENTS["aspiration_ok"].verify(ev).ok is True


def test_aspiration_fails_on_a_short_draw():
    ev = Evidence(telemetry={"ot": {"last_aspirated_ul": 40.0}},
                  expected={"liquid_handler": "ot", "volume_ul": 200.0})
    result = AGENTS["aspiration_ok"].verify(ev)
    assert result.ok is False
    assert result.data["error_frac"] == pytest.approx(0.8)


def test_aspiration_refuses_when_the_handler_reports_nothing():
    ev = Evidence(telemetry={"ot": {}}, expected={"liquid_handler": "ot", "volume_ul": 200.0})
    result = AGENTS["aspiration_ok"].verify(ev)
    assert result.ok is False
    assert "no aspirated volume" in result.detail


def test_aspiration_rejects_a_nonpositive_request():
    ev = Evidence(telemetry={"ot": {"last_aspirated_ul": 0.0}},
                  expected={"liquid_handler": "ot", "volume_ul": 0.0})
    assert AGENTS["aspiration_ok"].verify(ev).ok is False
