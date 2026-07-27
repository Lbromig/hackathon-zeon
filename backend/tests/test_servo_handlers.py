"""The three servo-loop handlers: snapshot, identify, solve, nudge.

These are the handlers the alignment loop is made of, tested against stubs rather than the
bench. What they must get right is not the happy path — it is refusing to move on a number
nobody can stand behind:

* a solve that refused must not become a move,
* an axis the solve could not observe must not be moved as if it were zero,
* a vision-derived step over the clamp is a detection failure, refused rather than clamped,
* a stale frame must not be identified against.
"""
from __future__ import annotations

import logging

import numpy as np
import pytest

from backend.app.engine.actions import (CameraSnapshot, LHRelative, OffsetOutputs,
                                        VisionIdentify, VisionSolveOffset)
from backend.app.engine.blackboard import Blackboard
from backend.app.engine.context import ActionContext
from backend.app.engine.handlers import camera as camera_h
from backend.app.engine.handlers.camera import Frame
from backend.app.engine.handlers import liquid_handler as lh_h
from backend.app.engine.handlers import vision as vision_h


class StubLH:
    """A liquid handler that records what it was asked to do."""

    def __init__(self, *, encoders=False, referenced=False, shortfall=0.0):
        self.moves: list[dict] = []
        self._pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}
        self._prov = {"encoders": encoders, "referenced": referenced,
                      "source": "controller_step_counts"}
        self.shortfall = shortfall

    def move_by(self, dx=0.0, dy=0.0, dz=0.0):
        self.moves.append({"dx": dx, "dy": dy, "dz": dz})
        achieved = {}
        for axis, d in (("X", dx), ("Y", dy), ("Z", dz)):
            if d:
                got = d - self.shortfall
                self._pos[axis] += got
                achieved[axis] = got
        drift = {a: -self.shortfall for a in achieved} if self.shortfall else {}
        return {"achieved": achieved, "drift_mm": drift, "moved": bool(achieved)}

    def position(self):
        return {"actual": dict(self._pos), "commanded": dict(self._pos),
                "provenance": dict(self._prov)}


class StubCamera:
    def __init__(self, frame=None, width=640, height=480, colour=True, fps=30):
        if frame is None:
            frame = np.zeros((height, width, 3), np.uint8)
            if colour:
                frame[:, :, 2] = 200          # a red-ish frame: channels differ
        self.frame = frame
        self.config = {"width": width, "height": height}
        self.reads = 0
        self._fps = fps

    def capture(self):
        self.reads += 1
        return self.frame

    def status(self):
        return {"fps": self._fps}

    def intrinsics(self):
        return None                            # the bench's real state on the avf path


class StubDevices:
    def __init__(self, **devices):
        self._d = devices

    def get(self, device_id):
        return self._d[device_id]

    def require_arm(self, device_id):
        return self._d[device_id]

    def require_liquid_handler(self, device_id):
        return self._d[device_id]

    def is_simulated(self, device_id):
        return True


def make_ctx(action, devices, blackboard=None, tmp_path=None):
    return ActionContext(
        run_id="test", action=action, devices=devices,
        blackboard=blackboard or Blackboard(),
        log=logging.getLogger("test"), simulated=True,
        artifact_dir=str(tmp_path) if tmp_path else "",
    )


# --- camera.snapshot -----------------------------------------------------------------

def test_snapshot_drains_before_keeping_a_frame(tmp_path):
    """A bare read can return a queued older frame — the pre-move frame that stalls a loop."""
    cam = StubCamera()
    action = CameraSnapshot(device="handover_cam", fresh=True, store=True)
    ctx = make_ctx(action, StubDevices(handover_cam=cam), tmp_path=tmp_path)

    out = camera_h.snapshot(action, ctx)

    assert cam.reads == camera_h.DRAIN_FRAMES + 1, "must discard, then take the keeper"
    assert out.width == 640 and out.height == 480
    assert out.stream == "color"
    assert out.captured_at.endswith("Z")
    assert ctx.collected_artifacts(), "the frame must be recorded as an artifact"


def test_snapshot_reports_the_achieved_mode_not_the_requested_one(tmp_path):
    """The D435 RGB module delivers 1920x1080 for a 1280x720 request."""
    cam = StubCamera(width=1920, height=1080)
    cam.config = {"width": 1280, "height": 720}
    action = CameraSnapshot(device="handover_cam")
    ctx = make_ctx(action, StubDevices(handover_cam=cam), tmp_path=tmp_path)

    out = camera_h.snapshot(action, ctx)

    assert out.achieved_mode.startswith("1920x1080")
    assert any(w.code == "mode_substituted" for w in ctx.collected_warnings())


def test_snapshot_flags_infrared_delivered_as_colour(tmp_path):
    """IR arrives as three identical channels, so a shape check calls it colour."""
    cam = StubCamera(colour=False)
    action = CameraSnapshot(device="overview_cam")
    ctx = make_ctx(action, StubDevices(overview_cam=cam), tmp_path=tmp_path)

    out = camera_h.snapshot(action, ctx)

    assert out.stream == "ir"
    assert any(w.code == "ir_not_colour" for w in ctx.collected_warnings())


# --- vision.identify -----------------------------------------------------------------

def test_identify_reports_absence_as_absence(tmp_path):
    """R-VIS-1/2: a low-confidence guess presented as a detection is the failure."""
    bb = Blackboard()
    bb.set("frame", Frame(camera="handover_cam", width=640, height=480,
                          image=np.zeros((480, 640, 3), np.uint8), stale=False),
           device="handover_cam")
    action = VisionIdentify(device="handover_cam", target="tip")
    ctx = make_ctx(action, StubDevices(handover_cam=StubCamera()), bb, tmp_path)

    out = vision_h.identify(action, ctx)

    assert out.found is False
    assert out.method == "none"
    assert out.point_px is None
    # Written anyway: "looked and did not find" differs from "never looked".
    assert bb.get("tip", device="handover_cam").found is False


def test_identify_refuses_a_stale_frame(tmp_path):
    bb = Blackboard()
    bb.set("frame", Frame(camera="handover_cam", width=640, height=480,
                          image=np.zeros((480, 640, 3), np.uint8), stale=True),
           device="handover_cam")
    action = VisionIdentify(device="handover_cam", target="tube")
    ctx = make_ctx(action, StubDevices(handover_cam=StubCamera()), bb, tmp_path)

    with pytest.raises(ValueError, match="stale"):
        vision_h.identify(action, ctx)


def test_identify_needs_a_frame_first(tmp_path):
    action = VisionIdentify(device="handover_cam", target="tip")
    ctx = make_ctx(action, StubDevices(handover_cam=StubCamera()), Blackboard(), tmp_path)
    with pytest.raises(Exception):
        vision_h.identify(action, ctx)


# --- vision.solve_offset -------------------------------------------------------------

def _written_identify(bb, camera, target, point, found=True, t=None):
    from backend.app.engine.actions import IdentifyOutputs

    bb.set(target, IdentifyOutputs(target=target, found=found, point_px=list(point),
                                   score=0.95, method="tag_anchored",
                                   T_cam_feature=t), device=camera)


def test_solve_writes_a_refusal_rather_than_raising(tmp_path):
    """A refusal is an outcome the loop reads, not an exception it dies on."""
    bb = Blackboard()
    _written_identify(bb, "handover_cam", "tip", (100, 100))
    _written_identify(bb, "handover_cam", "tube", (0, 0), found=False)
    action = VisionSolveOffset(device="handover_cam", render_overlay=False)
    ctx = make_ctx(action, StubDevices(handover_cam=StubCamera()), bb, tmp_path)

    out = vision_h.solve_offset(action, ctx)

    assert out.method == "refused"
    assert out.refusal == "missing_detection"
    assert any(w.code == "offset_refused" for w in ctx.collected_warnings())
    assert bb.get("selected_offset").method == "refused"


def test_solve_warns_about_axes_no_view_can_observe(tmp_path):
    bb = Blackboard()
    _written_identify(bb, "handover_cam", "tip", (100, 100))
    _written_identify(bb, "handover_cam", "tube", (120, 100))
    action = VisionSolveOffset(device="handover_cam", render_overlay=False)
    ctx = make_ctx(action, StubDevices(handover_cam=StubCamera()), bb, tmp_path)

    out = vision_h.solve_offset(action, ctx)

    assert out.method == "axis_decoupled_jacobian"
    assert out.observed_axes == ["x"], "y and z are not observable from this view"
    assert out.residual_offset_mm["y"] is None
    assert any(w.code == "axes_unobserved" for w in ctx.collected_warnings())


def test_solve_renders_an_overlay_even_on_a_refusal(tmp_path):
    """A picture of the frame with one feature marked is how a stalled loop gets diagnosed."""
    bb = Blackboard()
    bb.set("frame", Frame(camera="handover_cam", width=640, height=480,
                          image=np.zeros((480, 640, 3), np.uint8), stale=False),
           device="handover_cam")
    _written_identify(bb, "handover_cam", "tip", (100, 100))
    _written_identify(bb, "handover_cam", "tube", (0, 0), found=False)
    action = VisionSolveOffset(device="handover_cam", render_overlay=True)
    ctx = make_ctx(action, StubDevices(handover_cam=StubCamera()), bb, tmp_path)

    vision_h.solve_offset(action, ctx)

    overlays = [a for a in ctx.collected_artifacts() if a.kind == "image/overlay"]
    assert overlays, "a refusal still needs its picture"


# --- lh.move_relative ----------------------------------------------------------------

def _offset_slot(bb, **residual):
    bb.set("selected_offset", OffsetOutputs(
        residual_offset_mm={"x": residual.get("x"), "y": residual.get("y"),
                            "z": residual.get("z")},
        magnitude_mm=1.0, observed_axes=[k for k, v in residual.items() if v is not None],
        method="axis_decoupled_jacobian"))


def test_nudge_moves_only_the_observed_axes():
    """An unobserved axis is not 'aligned'; commanding 0 for it would claim it was."""
    bb = Blackboard()
    _offset_slot(bb, x=2.0)                    # y and z unobserved
    lh = StubLH()
    action = LHRelative(device="ot", from_slot="selected_offset")
    ctx = make_ctx(action, StubDevices(ot=lh), bb)

    out = lh_h.move_relative(action, ctx)

    assert lh.moves == [{"dx": 2.0, "dy": 0.0, "dz": 0.0}]
    assert out.applied_mm == {"x": 2.0}
    assert any(w.code == "unobserved_axes" for w in ctx.collected_warnings())


def test_nudge_refuses_a_step_over_the_clamp_rather_than_clamping():
    """O9: a step this size is a detection failure wearing a command's clothes."""
    bb = Blackboard()
    _offset_slot(bb, x=40.0)
    lh = StubLH()
    action = LHRelative(device="ot", from_slot="selected_offset", clamp_mm=15.0)
    ctx = make_ctx(action, StubDevices(ot=lh), bb)

    with pytest.raises(ValueError, match="refusing rather than clamping"):
        lh_h.move_relative(action, ctx)
    assert lh.moves == [], "nothing may move"


def test_a_refused_solve_moves_nothing_but_is_not_a_failed_step():
    """R-VIS-7: a refusal is the road to `stalled`, not to `aborted`.

    Raising here would end the loop as "a step inside the loop failed", which reads as a broken
    engine rather than as a rig that cannot currently see. So: no motion, a warning that reaches
    the operator, and the loop's own no-progress rule decides when to stop.
    """
    bb = Blackboard()
    bb.set("selected_offset", OffsetOutputs(method="refused", refusal="low_observability"))
    lh = StubLH()
    action = LHRelative(device="ot", from_slot="selected_offset")
    ctx = make_ctx(action, StubDevices(ot=lh), bb)

    out = lh_h.move_relative(action, ctx)

    assert lh.moves == [], "a refused solve must not move the head"
    assert out.applied_mm == {}
    assert any(w.code == "offset_refused" for w in ctx.collected_warnings())


def test_a_malformed_offset_slot_is_still_an_error():
    """A refusal is an outcome; a slot holding the wrong type is a bug, and must raise."""
    bb = Blackboard()
    bb.set("selected_offset", {"not": "an offset"})
    action = LHRelative(device="ot", from_slot="selected_offset")
    ctx = make_ctx(action, StubDevices(ot=StubLH()), bb)

    with pytest.raises(ValueError, match="does not hold a solved offset"):
        lh_h.move_relative(action, ctx)


def test_a_move_stamps_the_freshness_marker():
    """The next iteration's snapshot proves freshness against this stamp (R-CAM-2/D20)."""
    camera_h.reset_move_marker()
    bb = Blackboard()
    _offset_slot(bb, x=1.0)
    action = LHRelative(device="ot", from_slot="selected_offset")
    ctx = make_ctx(action, StubDevices(ot=StubLH()), bb)

    assert camera_h._last_move_at is None
    lh_h.move_relative(action, ctx)
    assert camera_h._last_move_at is not None
    camera_h.reset_move_marker()


def test_a_converged_offset_is_a_no_op_not_a_failure():
    bb = Blackboard()
    _offset_slot(bb, x=0.0)
    lh = StubLH()
    action = LHRelative(device="ot", from_slot="selected_offset")
    ctx = make_ctx(action, StubDevices(ot=lh), bb)

    out = lh_h.move_relative(action, ctx)

    assert lh.moves == []
    assert out.applied_mm == {}


def test_nudge_reports_dead_reckoning_not_measurement():
    """R-LH-4: the OT has no encoders, so its position must never read as a measurement."""
    bb = Blackboard()
    _offset_slot(bb, x=1.0)
    action = LHRelative(device="ot", from_slot="selected_offset")
    ctx = make_ctx(action, StubDevices(ot=StubLH()), bb)

    assert lh_h.move_relative(action, ctx).provenance == "dead_reckoned"


def test_nudge_reports_measured_only_when_the_driver_earns_it():
    bb = Blackboard()
    _offset_slot(bb, x=1.0)
    action = LHRelative(device="ot", from_slot="selected_offset")
    ctx = make_ctx(action, StubDevices(ot=StubLH(encoders=True, referenced=True)), bb)

    assert lh_h.move_relative(action, ctx).provenance == "measured"


def test_nudge_warns_when_an_axis_did_not_follow():
    """A commanded move the axis never made makes the next solve measure against a lie."""
    bb = Blackboard()
    _offset_slot(bb, x=4.0)
    lh = StubLH(shortfall=1.5)
    action = LHRelative(device="ot", from_slot="selected_offset")
    ctx = make_ctx(action, StubDevices(ot=lh), bb)

    out = lh_h.move_relative(action, ctx)

    assert out.drift_mm == pytest.approx(1.5)
    assert any(w.code == "move_shortfall" for w in ctx.collected_warnings())


def test_nudge_accepts_literal_deltas_for_the_final_retract():
    """R-VIS-11's last step is this action with a literal dz, not a separate kind."""
    lh = StubLH()
    action = LHRelative(device="ot", dz=20.0, clamp_mm=25.0)
    ctx = make_ctx(action, StubDevices(ot=lh), Blackboard())

    out = lh_h.move_relative(action, ctx)

    assert lh.moves == [{"dx": 0.0, "dy": 0.0, "dz": 20.0}]
    assert out.applied_mm == {"z": 20.0}


# --- lifecycle: the liquid handler's init report --------------------------------------
#
# `_init_liquid_handler` used to return a fixed "axis wiggle, Z retracted" whatever happened,
# discarding the driver's report. On a machine with no encoders that hid the one failure that
# matters: a retract that ran its full distance without ever reaching the top switch is
# indistinguishable from a stalled axis driven into the mechanical top.


class _StubLHDriver:
    def __init__(self, report):
        self.device_id = "ot"
        self._report = report

    def initialize(self):
        return self._report


def _lh_ctx():
    from backend.app.engine.actions import Initialize

    action = Initialize(device="ot")
    return make_ctx(action, StubDevices(ot=object()))


def test_a_retract_that_reached_the_switch_is_reported_as_such():
    from backend.app.engine.handlers import lifecycle

    ctx = _lh_ctx()
    detail = lifecycle._init_liquid_handler(_StubLHDriver({
        "axes": {"x": {}, "y": {}, "z": {}},
        "retract": {"at_top": True, "already_there": False, "retracted_mm": 0.0,
                    "z_travel_mm": -1.8},
    }), ctx)
    assert "top switch" in detail
    # The counter says 0 completed steps because the switch latched mid-step; the travel is
    # what actually happened, and is what must be reported.
    assert "1.8 mm" in detail
    assert not [w for w in ctx.collected_warnings() if w.code == "lh_retract_incomplete"]


def test_a_retract_that_never_found_the_switch_warns():
    """The stall signature: full commanded distance, no switch. Must not read as success."""
    from backend.app.engine.handlers import lifecycle

    ctx = _lh_ctx()
    detail = lifecycle._init_liquid_handler(_StubLHDriver({
        "axes": {"z": {}},
        "retract": {"at_top": False, "already_there": False, "retracted_mm": 30.0,
                    "z_travel_mm": 30.0},
    }), ctx)
    assert "never reached the top switch" in detail
    warned = [w for w in ctx.collected_warnings() if w.code == "lh_retract_incomplete"]
    assert warned, "a retract that found no switch must be visible"
    assert "no encoders" in warned[0].message


def test_a_skipped_retract_is_not_claimed_as_done():
    from backend.app.engine.handlers import lifecycle

    ctx = _lh_ctx()
    detail = lifecycle._init_liquid_handler(_StubLHDriver({"axes": {"x": {}}}), ctx)
    assert "NOT retracted" in detail
    assert "retract_z_on_init" in detail


def test_already_parked_on_the_switch_is_a_clean_outcome():
    from backend.app.engine.handlers import lifecycle

    ctx = _lh_ctx()
    detail = lifecycle._init_liquid_handler(_StubLHDriver({
        "axes": {"z": {}}, "retract": {"already_there": True, "at_top": True},
    }), ctx)
    assert "already at the top switch" in detail
    assert not ctx.collected_warnings()


def test_an_axis_that_started_on_its_endstop_is_named():
    from backend.app.engine.handlers import lifecycle

    ctx = _lh_ctx()
    detail = lifecycle._init_liquid_handler(_StubLHDriver({
        "axes": {"z": {"started_on_endstop": True}},
        "retract": {"already_there": True, "at_top": True},
    }), ctx)
    assert "z started on its endstop" in detail


def test_a_driver_returning_nothing_does_not_invent_detail():
    from backend.app.engine.handlers import lifecycle

    ctx = _lh_ctx()
    detail = lifecycle._init_liquid_handler(_StubLHDriver(None), ctx)
    assert "no report returned" in detail
    assert "retracted" not in detail, "must not claim a retract it cannot see"


def test_the_simulated_liquid_handler_can_reach_its_top_switch():
    """R-SIM-8: the retract outcome has to be exercisable in simulation, not just on the bench."""
    import drivers.mock as mock

    d = mock.MockLiquidHandlerDriver("ot", {})
    d.connect()
    report = d.initialize()["retract"]
    assert report["at_top"] is True
    assert 0 < report["retracted_mm"] <= report["requested_mm"]
    assert d.retract_z()["already_there"] is True, "a second retract has nowhere to go"
