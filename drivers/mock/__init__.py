"""In-memory mock drivers — hardware-free stand-ins for tests and demos.

Importing this module registers the mocks under their own type names
(``mock_arm`` / ``mock_camera`` / ``mock_liquid_handler``) so a fleet config can
reference them exactly like a real driver. No vendor SDK, cv2, or sockets — every
call updates plain in-memory state, so the backend and workflows can run end to
end on a laptop.
"""
from __future__ import annotations

import math
from typing import Any

from core.sim import world as sim_world

from ..base import ConnectionState, DeviceInfo, DriverError, InstrumentKind
from ..capabilities.arm import ArmDriver, GripperInfo, Pose
from ..capabilities.camera import CameraDriver
from ..capabilities.liquid_handler import (DeckLocation, LiquidHandlerDriver, MoveLimits,
                                           RelativeMoveReport)
from ..registry import register

#: R-LH-4, for the mock as much as for the real machine. The simulated handler tracks what it
#: *commanded*, which is dead reckoning; the world applies a gain error it cannot see. Reporting
#: `measured` here would make the simulation easier than the bench in exactly the dimension the
#: servo loop's correctness depends on.
MOCK_LH_PROVENANCE = {
    "source": "commanded_steps",
    "encoders": False,
    "referenced": False,
    "note": "dead-reckoned from commanded moves; the world applies a gain error this cannot see",
}


def _cv2() -> Any:
    """OpenCV, or None. Kept lazy so the other mocks stay importable without it."""
    try:
        import cv2

        return cv2
    except Exception:  # pragma: no cover
        return None


class MockArmDriver(ArmDriver):
    """A 6-axis arm that just remembers where it was told to go."""

    def __init__(self, device_id: str, config: dict[str, Any] | None = None) -> None:
        super().__init__(device_id, config)
        self._pose = Pose(200.0, 0.0, 200.0, 180.0, 0.0, 0.0)
        self._joints = [0.0] * self.axis_count
        self._gripper = 850.0
        self._mode = 0          # 0 = position control, 2 = hand-guiding (xArm numbering)

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(id=self.device_id, name=self.config.get("name", self.device_id),
                          kind=InstrumentKind.ARM, model="MockArm", vendor="mock")

    def status(self) -> dict[str, Any]:
        return {"state": self._state, "connected": self._state == ConnectionState.CONNECTED,
                "error_code": 0, "warn_code": 0, "mode": self._mode}

    def set_free_drive(self, on: bool = True) -> None:
        """Pretend to hand-guide, so the teach UI's toggle works without hardware.

        Real arms become back-drivable; the mock just remembers the mode so the UI
        can round-trip the state.
        """
        self._mode = 2 if on else 0

    def connect(self) -> None:
        self._state = ConnectionState.CONNECTED

    def disconnect(self) -> None:
        self._state = ConnectionState.DISCONNECTED

    def enable(self, on: bool = True) -> None:
        self._state = ConnectionState.CONNECTED if on else ConnectionState.DISCONNECTED

    def home(self) -> None:
        self._joints = [0.0] * self.axis_count
        self._pose = Pose(200.0, 0.0, 200.0, 180.0, 0.0, 0.0)

    def stop(self, emergency: bool = False) -> None:
        pass

    def clear_errors(self) -> None:
        pass

    def get_pose(self) -> Pose:
        return Pose(**self._pose.__dict__)

    def move_to(self, pose: Pose, speed: float | None = None, wait: bool = True) -> None:
        self._pose = Pose(**pose.__dict__)

    def move_relative(self, dx=0, dy=0, dz=0, droll=0, dpitch=0, dyaw=0,
                      speed: float | None = None, wait: bool = True) -> None:
        p = self._pose
        self._pose = Pose(p.x + dx, p.y + dy, p.z + dz,
                          p.roll + droll, p.pitch + dpitch, p.yaw + dyaw)

    def get_joints(self) -> list[float]:
        return list(self._joints)

    def move_joints(self, angles: list[float], speed: float | None = None,
                    wait: bool = True, radius: float | None = None) -> None:
        # The mock has no trajectory queue, so a blend radius changes nothing about
        # where it ends up — it still lands exactly on the waypoint. Accepted so the
        # blended replay path is exercisable without hardware.
        self._joints = [float(a) for a in angles]

    def move_joints_relative(self, deltas: list[float], speed: float | None = None,
                             wait: bool = True) -> None:
        self._joints = [a + d for a, d in zip(self._joints, deltas)]

    @property
    def gripper_info(self) -> GripperInfo:
        return GripperInfo(kind="parallel", supports_width=True, min_width=0.0,
                           max_width=850.0, units="counts", stroke_m=0.085)

    def grip(self, width: float | None = None, force: float | None = None) -> None:
        if force is not None:
            raise ValueError("mock gripper has no force control")
        self._gripper = 0.0 if width is None else float(width)

    def release(self) -> None:
        self._gripper = 850.0

    def gripper_width(self) -> float | None:
        return self._gripper


class MockCameraDriver(CameraDriver):
    """A camera that returns a tiny synthetic frame."""

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(id=self.device_id, name=self.config.get("name", self.device_id),
                          kind=InstrumentKind.CAMERA, model="MockCamera", vendor="mock")

    def status(self) -> dict[str, Any]:
        return {"state": self._state, "connected": self._state == ConnectionState.CONNECTED}

    def connect(self) -> None:
        self._state = ConnectionState.CONNECTED

    def disconnect(self) -> None:
        self._state = ConnectionState.DISCONNECTED

    def capture(self) -> Any:
        try:  # numpy is a backend dep, but keep the mock importable without it
            import numpy as np

            return np.zeros((4, 4, 3), dtype="uint8")
        except Exception:  # pragma: no cover
            return b"\x00" * 48

    def capture_jpeg(self, quality: int = 85) -> bytes:
        return b"\xff\xd8\xff\xd9"  # minimal JPEG (SOI + EOI markers)


class MockTagCameraDriver(CameraDriver):
    """A camera that renders real tag36h11 markers — the overlay path, no hardware.

    The markers are generated with the same dictionary `core.perception.fiducials`
    detects, so the full chain (capture -> detect -> normalized polygon -> SVG
    overlay) is exercised for real; only the photons are fake. Tags drift slowly
    so a stalled MJPEG stream is obvious at a glance.

    Set ``"depth": true`` to also stand in for an RGB-D unit (RealSense): the mock
    then reports plausible pinhole intrinsics and a flat depth plane at
    ``depth_m``, which exercises the depth-sampling and back-projection path that
    only real hardware would otherwise reach.

    Config: {"markers": [180, 224], "width": 960, "height": 540, "motion": true,
             "depth": false, "depth_m": 0.5}.
    """

    DEFAULT_MARKERS = (180, 183, 224)

    def __init__(self, device_id: str, config: dict[str, Any] | None = None) -> None:
        super().__init__(device_id, config)
        self._frame_no = 0
        self.has_depth = bool(self.config.get("depth", False))

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(id=self.device_id, name=self.config.get("name", self.device_id),
                          kind=InstrumentKind.CAMERA, model="MockTagCamera", vendor="mock",
                          meta={"markers": list(self.config.get("markers", self.DEFAULT_MARKERS))})

    def status(self) -> dict[str, Any]:
        return {"state": self._state, "connected": self._state == ConnectionState.CONNECTED,
                "frames": self._frame_no}

    def connect(self) -> None:
        if _cv2() is None:
            raise DriverError("opencv-contrib-python not installed")
        self._state = ConnectionState.CONNECTED

    def disconnect(self) -> None:
        self._state = ConnectionState.DISCONNECTED

    def capture(self) -> Any:
        import numpy as np

        cv2 = _cv2()
        if cv2 is None or self._state != ConnectionState.CONNECTED:
            raise DriverError("mock tag camera not connected")

        w = int(self.config.get("width", 960))
        h = int(self.config.get("height", 540))
        frame = np.full((h, w, 3), 60, np.uint8)          # mid-grey bench
        self._frame_no += 1

        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
        markers = list(self.config.get("markers", self.DEFAULT_MARKERS))
        side = max(48, min(w, h) // 6)
        drift = 0.0
        if self.config.get("motion", True):
            drift = 20.0 * np.sin(self._frame_no / 25.0)

        for i, marker_id in enumerate(markers):
            tag = cv2.aruco.generateImageMarker(dictionary, int(marker_id), side)
            # quiet zone: the detector needs white margin around the tag
            pad = side // 5
            canvas = np.full((side + 2 * pad, side + 2 * pad), 255, np.uint8)
            canvas[pad:pad + side, pad:pad + side] = tag
            tile = cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR)

            step = w // (len(markers) + 1)
            cx = step * (i + 1)
            cy = int(h / 2 + drift * (1 if i % 2 == 0 else -1))
            y0, x0 = cy - tile.shape[0] // 2, cx - tile.shape[1] // 2
            y0 = max(0, min(h - tile.shape[0], y0))
            x0 = max(0, min(w - tile.shape[1], x0))
            frame[y0:y0 + tile.shape[0], x0:x0 + tile.shape[1]] = tile

        cv2.putText(frame, f"mock tag camera · frame {self._frame_no}", (12, h - 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1, cv2.LINE_AA)
        return frame

    def capture_jpeg(self, quality: int = 85) -> bytes:
        cv2 = _cv2()
        if cv2 is None:
            raise DriverError("opencv-contrib-python not installed")
        ok, buf = cv2.imencode(".jpg", self.capture(), [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise DriverError("jpeg encode failed")
        return buf.tobytes()

    # --- optional RGB-D half (config "depth": true) --------------------------
    def capture_depth(self) -> Any:
        """A flat plane at `depth_m`. Enough to exercise sampling + back-projection."""
        import numpy as np

        if not self.has_depth:
            raise NotImplementedError(f"{self.device_id}: no depth stream")
        w = int(self.config.get("width", 960))
        h = int(self.config.get("height", 540))
        return np.full((h, w), float(self.config.get("depth_m", 0.5)), np.float32)

    def capture_rgbd(self) -> tuple[Any, Any]:
        return self.capture(), self.capture_depth()

    def intrinsics(self) -> dict[str, Any] | None:
        """Plausible pinhole intrinsics — a ~60° horizontal FoV, centred principal point."""
        if not self.has_depth:
            return None
        w = int(self.config.get("width", 960))
        h = int(self.config.get("height", 540))
        f = w / 1.1547                       # 2*tan(30°)
        return {"fx": f, "fy": f, "cx": w / 2.0, "cy": h / 2.0,
                "width": w, "height": h, "coeffs": [0.0] * 5}

    def camera_matrix(self) -> Any:
        import numpy as np

        i = self.intrinsics()
        if not i:
            raise DriverError("no intrinsics on a colour-only mock camera")
        return np.array([[i["fx"], 0, i["cx"]], [0, i["fy"], i["cy"]], [0, 0, 1]], float)


class MockLiquidHandlerDriver(LiquidHandlerDriver):
    """A liquid handler that tracks tip state, aspirated volume, and **moves the sim world**.

    The relative-move half is what makes the servo loop worth simulating (D3/R-SIM-5): every
    ``move_relative`` decrements the shared tip↔tube offset in `core.sim.world`, which the
    synthetic camera then renders. So the loop converges *because the commanded moves close the
    offset*, and a wrong-signed jacobian is a reproducible pytest failure rather than a bench
    surprise with a pipette in it.

    **Task frame throughout**, per the capability: ``+z`` is up, away from the deck, and the
    offset is ``tube − tip`` so commanding the offset closes it. (This mock has no controller
    frame to convert from — that inversion is the OT-One's, and it lives in its driver.)

    **This mock's position is dead reckoning, deliberately.** It accumulates what it *commanded*,
    not what the world applied, and reports ``provenance: dead_reckoned``. The world applies a
    gain error the driver cannot see, exactly as a real stalled axis is invisible to a step
    counter — measured on this bench, a 10 mm retract advanced the counter 10 mm while the camera
    showed ~2 mm. Giving the mock ground truth would make simulation *easier* than reality in the
    one dimension that matters, and would hide the class of bug the world exists to expose: the
    discrepancy must only ever be visible through the camera.

    Config: ``{"sim_world": SimWorld, "envelope": {"z": [-50, 120]},
    "max_step_mm": {"z": 50}, "retract_z_mm": 30}``. ``sim_world`` is how a test isolates itself
    from the process-wide world without touching the singleton; omitted, it shares the singleton,
    which is the whole point of D3.
    """

    #: Per-axis single-move bound, mm, task frame. Generous enough for the plan's closing
    #: ``dz=+40`` retract and tight enough that a unit slip of 10x is refused rather than run.
    DEFAULT_MAX_STEP_MM = {"x": 50.0, "y": 50.0, "z": 50.0}

    #: How far :meth:`initialize` wiggles each axis. Small: this is a "does it move" check, not a
    #: calibration.
    INIT_STEP_MM = {"x": 3.0, "y": 3.0, "z": 2.0}

    #: Default retract at the end of :meth:`initialize`.
    DEFAULT_RETRACT_MM = 30.0

    def __init__(self, device_id: str, config: dict[str, Any] | None = None) -> None:
        super().__init__(device_id, config)
        self._has_tip = False
        self._aspirated_ul = 0.0
        self._position = {axis: 0.0 for axis in sim_world.AXES}
        #: Every relative move commanded, in order — the mock's equivalent of the loopback
        #: transport's wire log, and what a test asserts against.
        self.commanded: list[dict[str, float]] = []

    @property
    def world(self) -> "sim_world.SimWorld":
        """The simulated world this handler moves. The shared one unless config says otherwise."""
        given = self.config.get("sim_world")
        return given if isinstance(given, sim_world.SimWorld) else sim_world.world()

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(id=self.device_id, name=self.config.get("name", self.device_id),
                          kind=InstrumentKind.LIQUID_HANDLER, model="MockLH", vendor="mock")

    def status(self) -> dict[str, Any]:
        return {"state": self._state, "connected": self._state == ConnectionState.CONNECTED,
                "has_tip": self._has_tip, "aspirated_ul": self._aspirated_ul,
                "position": dict(self._position),
                "offset_mm": self.world.offset(),
                # Right next to the numbers, so nothing can read them as measurements (R-LH-4).
                "provenance": dict(MOCK_LH_PROVENANCE)}

    def connect(self) -> None:
        self._state = ConnectionState.CONNECTED

    def disconnect(self) -> None:
        self._state = ConnectionState.DISCONNECTED

    def home(self) -> None:
        pass

    def pick_up_tip(self, location: DeckLocation) -> None:
        self._has_tip = True

    def drop_tip(self, location: DeckLocation | None = None) -> None:
        self._has_tip = False

    def aspirate(self, volume_ul: float, location: DeckLocation) -> None:
        self._aspirated_ul += float(volume_ul)

    def dispense(self, volume_ul: float, location: DeckLocation) -> None:
        self._aspirated_ul = max(0.0, self._aspirated_ul - float(volume_ul))

    def move_to(self, location: DeckLocation) -> None:
        pass

    # --- relative motion, the servo loop's one action (R-LH-1) ---------------
    def move_relative(self, dx: float = 0.0, dy: float = 0.0,
                      dz: float = 0.0) -> RelativeMoveReport:
        """Move the head by a task-frame offset, and move the simulated world with it.

        ``+dz`` raises the head, so the shared offset's ``z`` decreases — that subtraction is what
        the whole simulation is for. Refuses rather than clamps (R-LH-3), and refuses a non-finite
        delta: every ``abs(delta) > limit`` comparison is False against NaN, so a naive check would
        wave it through here just as it would on hardware.
        """
        requested = {"x": float(dx), "y": float(dy), "z": float(dz)}
        limits = self.move_limits()
        for axis, delta in requested.items():
            if not math.isfinite(delta):
                raise DriverError(f"{self.device_id}: {axis} delta is not a finite number")
            reason = limits.refusal_for(axis, delta, self._position)
            if reason:
                raise DriverError(f"{self.device_id}: {reason}")

        moving = {a: v for a, v in requested.items() if v}
        before = dict(self._position)
        if not moving:
            return RelativeMoveReport(requested_mm=requested, applied_mm={},
                                      position_before=before, position_after=before,
                                      provenance="dead_reckoned", moved=False)

        self.commanded.append(dict(moving))
        self.world.apply_move(**{f"d{a}": v for a, v in moving.items()})
        for axis, delta in moving.items():
            # Commanded, not applied. See the class docstring: this machine has no encoders, and
            # pretending otherwise would make the simulation easier than the bench.
            self._position[axis] = round(self._position[axis] + delta, 6)

        return RelativeMoveReport(
            requested_mm=requested, applied_mm=dict(moving),
            position_before=before, position_after=dict(self._position),
            provenance="dead_reckoned", moved=True,
            detail={"offset_mm": self.world.offset()},
        )

    def move_limits(self) -> MoveLimits:
        """Per-axis step bound always; absolute envelope only when configured.

        No default envelope, for the same reason the real driver has none: nothing here is homed,
        so an absolute bound would be a number with no referenced origin — and a meaningless bound
        that reads as a safety feature is worse than a stated absence.
        """
        steps = {**self.DEFAULT_MAX_STEP_MM,
                 **{str(a).lower(): float(v)
                    for a, v in (self.config.get("max_step_mm") or {}).items()}}
        envelope = {str(a).lower(): (float(span[0]), float(span[1]))
                    for a, span in (self.config.get("envelope") or {}).items()}
        return MoveLimits(max_step_mm=steps, envelope_mm=envelope)

    def position(self) -> dict[str, Any]:
        """Task-frame position, and the statement that it is dead reckoning (R-LH-4).

        ``commanded`` and ``actual`` are the same numbers here, and that is not a shortcut — it is
        what a machine without encoders actually reports. The divergence a real controller shows
        after a cut-short move comes from its own planner, which this mock does not model; what it
        does model is the discrepancy that matters, between the counter and the world.
        """
        return {"actual": dict(self._position), "commanded": dict(self._position),
                "offset_mm": {a: 0.0 for a in sim_world.AXES},
                "provenance": dict(MOCK_LH_PROVENANCE),
                "frame": "task"}

    # --- initialization (R-LH-2) --------------------------------------------
    def initialize(self, retract_z: bool | None = None) -> dict[str, Any]:
        """Wiggle every axis both ways and return, then retract Z up.

        Named ``initialize`` exactly: `lifecycle._init_liquid_handler` probes for that attribute
        and *warns and skips* when it is absent, so a different name means the wiggle silently
        never runs — and an initialization that reports success while the head never moved is the
        failure D6 was written about.

        The wiggle goes through :meth:`move_relative` rather than around it, so the simulated world
        really does see the head move and come back. Net motion is zero by construction; net world
        change is zero to within the world's noise, which is the honest simulation of a real
        wiggle rather than a special case that skips the physics.

        The plungers are never touched — there are none here, and there is no code path that could
        grow one by accident.
        """
        if retract_z is None:
            retract_z = bool(self.config.get("retract_z_on_init", True))

        report: dict[str, Any] = {"axes": {}, "plungers_touched": False}
        for axis, step in self.INIT_STEP_MM.items():
            start = self._position[axis]
            self.move_relative(**{f"d{axis}": +step})
            self.move_relative(**{f"d{axis}": -step})
            report["axes"][axis] = {"moved_mm": step,
                                    "net_mm": round(self._position[axis] - start, 6),
                                    "directions": ["+", "-"]}
        if retract_z:
            report["retract"] = self.retract_z(self.config.get("retract_z_mm"))
        report["position"] = dict(self._position)
        report["provenance"] = dict(MOCK_LH_PROVENANCE)
        return report

    def retract_z(self, distance_mm: float | None = None) -> dict[str, Any]:
        """Raise the head. ``+z`` is up, so this is a positive move — see the capability docstring.

        There is no simulated top switch, so the distance is the only bound and a non-positive or
        non-finite request is refused rather than treated as "no retract": silently doing nothing
        when asked to move clear of the deck is the failure worth refusing loudly.
        """
        distance = self.DEFAULT_RETRACT_MM if distance_mm is None else float(distance_mm)
        if not math.isfinite(distance) or distance <= 0:
            raise DriverError(f"{self.device_id}: retract distance must be positive")
        report = self.move_relative(dz=+distance)
        return {"requested_mm": distance, "retracted_mm": report.applied_mm.get("z", 0.0),
                "at_top": False, "already_there": False,
                "position": dict(self._position)}


def register_mocks() -> None:
    """(Re)register the mock drivers with the shared registry. Idempotent."""
    register("mock_arm", MockArmDriver)
    register("mock_camera", MockCameraDriver)
    register("mock_tag_camera", MockTagCameraDriver)
    register("mock_liquid_handler", MockLiquidHandlerDriver)


# register on import so `import drivers.mock` is enough to make them available
register_mocks()

__all__ = [
    "MockArmDriver", "MockCameraDriver", "MockTagCameraDriver", "MockLiquidHandlerDriver",
    "register_mocks",
]
