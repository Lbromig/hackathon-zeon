"""UFACTORY xArm driver — wraps the vendored xArm-Python-SDK.

We run two of these (left / right) cooperatively. Both are calibrated into one
shared world frame; that transform lives in config (``world_from_base``).

Two SDK footguns this driver exists to contain:

* Almost every ``XArmAPI`` call returns an int code, 0 = success. Discarding it
  makes a *rejected* command look like a successful one — so every call goes
  through ``_check``. The exception is ``disconnect``, which must never raise.
* ``arm.state`` as *reported* (1=moving, 2=idle, 3=suspended, 4=stopped) uses a
  different numbering from the argument to ``set_state()`` (0=motion, 3=pause,
  4=stop). Never compare one against the other.
"""
from __future__ import annotations

import math
import time
from typing import Any

from ..base import ConnectionState, DeviceInfo, DriverError, InstrumentKind
from ..capabilities.arm import ArmDriver, ArmLimits, GripperInfo, GripperKind, Pose

try:  # SDK is optional at import time so the backend can boot without hardware
    from xarm.wrapper import XArmAPI
except Exception:  # pragma: no cover
    XArmAPI = None

# arm.device_type -> model. Lite 6 is 9, the 850 is 12; xArm 5/6/7 report their axis count.
DEVICE_TYPE_LITE6 = 9
DEVICE_TYPE_850 = 12

# No SDK motion call may block forever. The SDK's own pause-wait has no timeout:
# if the arm enters state 3 (suspended) mid-move, an un-timed-out call hangs the
# worker thread while holding this arm's lock, and that arm is 409-busy for good.
MOTION_TIMEOUT_S = 60.0
REPORT_READY_TIMEOUT_S = 5.0
BRAKE_SETTLE_S = 0.2

# Parallel (xArm) gripper opening, controller counts: 0 = closed, 850 = open.
# The counts are 0.1 mm each, so full stroke is 85 mm — override per end effector
# with config["gripper_stroke_m"] if a different one is fitted.
PARALLEL_MIN = 0.0
PARALLEL_MAX = 850.0
PARALLEL_STROKE_M = 0.085
PARALLEL_SPEED = 2000


class XArmDriver(ArmDriver):
    """Config: {"ip": "192.168.1.xxx", "name": "left", "tcp_speed": 120,
    "gripper": "auto"|"parallel"|"lite6"|"bio"|"none", "limits": {...}}."""

    def __init__(self, device_id: str, config: dict[str, Any] | None = None) -> None:
        super().__init__(device_id, config)
        self._api: "XArmAPI | None" = None
        self._ip = self.config.get("ip")
        self._gripper_kind: GripperKind = self._configured_gripper()
        self._gripper_ready = False
        self._model = "xArm"
        self._axis: int | None = None
        self._joint_limits: list[tuple[float, float]] | None = None

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            id=self.device_id,
            name=self.config.get("name", self.device_id),
            kind=InstrumentKind.ARM,
            model=self._model,
            vendor="UFactory",
            meta={"ip": self._ip, "gripper": self._gripper_kind},
        )

    def connect(self) -> None:
        if XArmAPI is None:
            raise DriverError("xArm SDK not installed (uv sync installs the vendored SDK)")
        if not self._ip:
            raise DriverError("xArm driver requires config['ip']")
        self._state = ConnectionState.CONNECTING
        try:
            self._api = XArmAPI(self._ip, is_radian=False)
            self._await_report(self._api)
            self._identify(self._api)

            # Bring-up, mirroring scripts/init_xarm.py. A latched fault from a
            # previous session blocks every motion command until it is cleared,
            # and an unchecked motion_enable would leave us reporting CONNECTED
            # for an arm that rejects everything.
            self._check(self._api.clean_warn(), "clean_warn")
            self._check(self._api.clean_error(), "clean_error")
            self._check(self._api.motion_enable(True), "motion_enable")
            self._check(self._api.set_mode(0), "set_mode(0)")     # 0 = position control
            self._check(self._api.set_state(0), "set_state(0)")   # 0 = motion
            self._apply_safety_settings(self._api)

            self._gripper_kind = self._resolve_gripper(self._api)
            self._gripper_ready = False
            self._state = ConnectionState.CONNECTED
        except Exception as e:
            self._state = ConnectionState.ERROR
            # Never leave a half-initialised, possibly energized arm behind.
            api, self._api = self._api, None
            if api is not None:
                try:
                    api.disconnect()
                except Exception:
                    pass
            raise DriverError(f"xArm connect failed: {e}") from e

    def _await_report(self, api: "XArmAPI") -> None:
        """Block until the controller's 'rich' report has populated.

        Until the first report lands, ``axis`` reads back the SDK's default of 7
        and ``device_type`` is unset, so anything sized off ``axis_count`` in that
        window builds 7 joint values for a 6-axis arm. ``motor_brake_states`` is
        ``[-1, ...]`` until the report arrives, which makes a reliable sentinel.
        """
        deadline = time.monotonic() + REPORT_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                if list(api.motor_brake_states or [-1])[0] != -1:
                    return
            except Exception:
                pass
            time.sleep(0.1)
        raise DriverError(
            f"no controller report within {REPORT_READY_TIMEOUT_S:g}s — "
            "axis count and model would be unreliable"
        )

    def _identify(self, api: "XArmAPI") -> None:
        """Cache model / axis count / joint limits once, from the populated report."""
        self._axis = int(api.axis)
        device_type = int(api.device_type)
        if self._axis == 6 and device_type == DEVICE_TYPE_LITE6:
            self._model = "Lite 6"
        elif self._axis == 6 and device_type == DEVICE_TYPE_850:
            self._model = "xArm 850"
        else:
            self._model = f"xArm {self._axis}"
        self._joint_limits = self._joint_limits_from_sdk(api)

    @staticmethod
    def _joint_limits_from_sdk(api: "XArmAPI") -> list[tuple[float, float]] | None:
        """Per-model joint limits from the SDK's own table, converted to degrees.

        Mirrors the variant selection in xarm/x3/xarm.py::_is_out_of_joint_range —
        serial numbers in [1305, 8500) use a different table.
        """
        try:
            from xarm.core.config.x_config import XCONF

            device_type = int(api.device_type)
            sn = api.sn or ""
            if len(sn) >= 6 and sn[2:6].isdigit() and 1305 <= int(sn[2:6]) < 8500:
                device_type = int(f"{api.axis}1305")
            radians = XCONF.Robot.JOINT_LIMITS.get(int(api.axis), {}).get(device_type, [])
            if not radians:
                return None
            return [(math.degrees(lo), math.degrees(hi)) for lo, hi in radians]
        except Exception:
            return None

    def _apply_safety_settings(self, api: "XArmAPI") -> None:
        """Payload, TCP offset and collision settings.

        These live on the controller and are volatile — they do not survive a
        reboot unless ``save_conf()`` is called, so re-apply on every connect
        rather than trusting whatever the last session left behind. An understated
        payload means wrong gravity compensation (measured: ~11 mm of sag per
        brake cycle) and skewed collision thresholds.
        """
        payload = self.config.get("payload_kg")
        if payload is not None:
            com = [float(v) for v in self.config.get("payload_com", [0.0, 0.0, 0.0])]
            self._check(api.set_tcp_load(float(payload), com, wait=True), "set_tcp_load")

        offset = self.config.get("tcp_offset")
        if offset is not None:
            # Without this the commanded frame is the flange, not the tool tip, so
            # every geometrically-planned grasp is off by the tool length.
            self._check(api.set_tcp_offset([float(v) for v in offset], wait=True),
                        "set_tcp_offset")

        sensitivity = self.config.get("collision_sensitivity")
        if sensitivity is not None:
            self._check(api.set_collision_sensitivity(int(sensitivity)),
                        "set_collision_sensitivity")
        self._check(api.set_self_collision_detection(True), "set_self_collision_detection")

        if self.config.get("save_conf"):
            self._check(api.save_conf(), "save_conf")

    def disconnect(self) -> None:
        """Leave the arm mechanically safe, then drop the connection.

        Closing the socket does NOT stop a queued trajectory — the controller runs
        it to completion unsupervised, with no reachable /stop. De-energizing the
        servos is also what engages the holding brakes, so skipping it leaves an
        arm that sags the moment power is cut. Best effort: this must not raise.
        """
        api, self._api = self._api, None
        if api is not None:
            try:
                self._brake(api)
            except Exception as e:  # pragma: no cover - hardware dependent
                print(f"[xarm {self.device_id}] WARNING: could not brake on disconnect: {e}")
            finally:
                try:
                    api.disconnect()
                except Exception:
                    pass
        self._gripper_ready = False
        self._gripper_kind = self._configured_gripper()
        self._state = ConnectionState.DISCONNECTED

    def _brake(self, api: "XArmAPI") -> None:
        """STOP -> settle -> servos off (brakes engage) -> verify."""
        api.set_state(4)                      # 4 = stop
        self._wait_until_stopped(api)
        time.sleep(BRAKE_SETTLE_S)            # settle before handing off to the brakes
        api.motion_enable(enable=False)       # servos off -> holding brakes engage
        time.sleep(0.3)
        # motor_brake_states: 0 = brake engaged, 1 = released
        brakes = list(api.motor_brake_states or [])[: self._axis or 6]
        if brakes and not all(b == 0 for b in brakes):
            print(f"[xarm {self.device_id}] WARNING: brakes not all engaged: {brakes}")

    @staticmethod
    def _wait_until_stopped(api: "XArmAPI", timeout: float = 10.0) -> bool:
        """Poll until the arm is genuinely idle.

        Reported state 0 *and* 1 both mean "still moving" (the SDK's own wait_move
        treats only >= 4 as terminal), and a non-empty command queue means motion
        is still pending. XArmAPI is a flat wrapper and does not expose wait_move.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                moving = api.state in (0, 1) or (api.cmd_num or 0) > 0
            except Exception:
                return False
            if not moving:
                return True
            time.sleep(0.05)
        return False

    def status(self) -> dict[str, Any]:
        if self._api is None:
            return {"state": self._state, "connected": False}
        out: dict[str, Any] = {
            "state": self._state,
            "connected": True,
            "mode": self._api.mode,
            "arm_state": self._api.state,
            "error_code": self._api.error_code,
            "warn_code": self._api.warn_code,
        }
        # Pose and joints are live socket reads and either can fail transiently.
        # Guard them *individually*: callers gate motion on error_code, so letting
        # a failed pose read propagate would blank the fault codes too, and a
        # missing error_code reads as "no fault" — waving motion onto a faulted arm.
        for key, read in (("pose", lambda: self.get_pose().__dict__),
                          ("joints", self.get_joints)):
            try:
                out[key] = read()
            except Exception as e:
                out.setdefault("read_errors", {})[key] = str(e)
        return out

    # --- motion ------------------------------------------------------------
    def _require(self) -> "XArmAPI":
        if self._api is None:
            raise DriverError("xArm not connected")
        return self._api

    @staticmethod
    def _check(code: Any, what: str) -> None:
        """Raise unless the SDK reported success.

        A few calls (``emergency_stop``) return None rather than a code — treat
        that as "no verdict available", never as success.
        """
        if code is None:
            return
        if isinstance(code, tuple):
            code = code[0]
        if code != 0:
            raise DriverError(f"{what} failed (code={code})")

    def enable(self, on: bool = True) -> None:
        """Energize (brakes release) or de-energize (brakes engage) the servos.

        Order matters when disabling: stop first, *then* drop the servos. Dropping
        them mid-trajectory hands a moving arm straight to the mechanical brakes.
        Verified rather than assumed — an operator who is told "brakes engaged"
        may put a hand into the workspace.
        """
        api = self._require()
        if on:
            self._check(api.motion_enable(True), "motion_enable(True)")
            self._check(api.set_state(0), "set_state(0)")
            return

        self._check(api.set_state(4), "set_state(4)")
        self._wait_until_stopped(api)
        time.sleep(BRAKE_SETTLE_S)
        self._check(api.motion_enable(False), "motion_enable(False)")
        time.sleep(0.3)
        brakes = list(api.motor_brake_states or [])[: self.axis_count]
        if brakes and not all(b == 0 for b in brakes):
            raise DriverError(f"servos disabled but brakes not all engaged: {brakes}")

    def home(self) -> None:
        self._check(self._require().move_gohome(wait=True, timeout=MOTION_TIMEOUT_S),
                    "move_gohome")

    def stop(self, emergency: bool = False) -> None:
        """Halt motion, then verify the arm actually stopped.

        Neither path de-energizes the servos, so the arm ends up halted but still
        live. ``emergency_stop()`` returns None (no code exists to check), which is
        exactly why the post-condition below is the only real evidence of success.
        """
        api = self._require()
        if emergency:
            api.emergency_stop()
        else:
            self._check(api.set_state(4), "set_state(4)")
        if not self._wait_until_stopped(api, timeout=3.0):
            raise DriverError(
                f"stop issued but arm still reports state={api.state} "
                f"(cmd_num={api.cmd_num}) — motion may be continuing"
            )

    def clear_errors(self) -> None:
        """Clear latched faults and bring the arm back to a movable state.

        Mirrors the bring-up sequence in scripts/init_xarm.py: an emergency stop
        or a collision latches an error that blocks every subsequent motion
        command until it's cleaned *and* the arm is re-enabled.

        A gripper fault latches *separately*: without clean_gripper_error() every
        subsequent grip returns END_EFFECTOR_HAS_FAULT (102) forever.
        """
        api = self._require()
        self._check(api.clean_warn(), "clean_warn")
        self._check(api.clean_error(), "clean_error")
        if self._gripper_kind in ("parallel", "bio"):
            try:
                self._check(api.clean_gripper_error(), "clean_gripper_error")
            except DriverError as e:
                print(f"[xarm {self.device_id}] gripper error not cleared: {e}")
        self._gripper_ready = False   # force a re-enable on the next grip
        self._check(api.motion_enable(True), "motion_enable")
        self._check(api.set_mode(0), "set_mode(0)")
        self._check(api.set_state(0), "set_state(0)")

    def get_pose(self) -> Pose:
        code, p = self._require().get_position(is_radian=False)
        if code != 0:
            raise DriverError(f"get_position error {code}")
        return Pose(*p)  # [x, y, z, roll, pitch, yaw]

    def move_to(self, pose: Pose, speed: float | None = None, wait: bool = True) -> None:
        self._check(self._require().set_position(
            x=pose.x, y=pose.y, z=pose.z,
            roll=pose.roll, pitch=pose.pitch, yaw=pose.yaw,
            speed=speed or self.config.get("tcp_speed", 100), wait=wait,
            timeout=MOTION_TIMEOUT_S if wait else None,
        ), "set_position")

    def move_relative(self, dx=0, dy=0, dz=0, droll=0, dpitch=0, dyaw=0,
                      speed: float | None = None, wait=True) -> None:
        self._check(self._require().set_position(
            x=dx, y=dy, z=dz, roll=droll, pitch=dpitch, yaw=dyaw,
            relative=True, wait=wait, speed=speed or self.config.get("tcp_speed", 100),
            timeout=MOTION_TIMEOUT_S if wait else None,
        ), "set_position(relative)")

    # --- joint space ---------------------------------------------------------
    def get_joints(self) -> list[float]:
        code, angles = self._require().get_servo_angle(is_radian=False)
        if code != 0:
            raise DriverError(f"get_servo_angle error {code}")
        return [float(a) for a in angles[: self.axis_count]]

    def move_joints(self, angles: list[float], speed: float | None = None,
                    wait: bool = True) -> None:
        self._check(self._require().set_servo_angle(
            angle=list(angles), speed=speed or self.config.get("joint_speed", 20),
            is_radian=False, wait=wait, timeout=MOTION_TIMEOUT_S if wait else None,
        ), "set_servo_angle")

    def move_joints_relative(self, deltas: list[float], speed: float | None = None,
                             wait: bool = True) -> None:
        self._check(self._require().set_servo_angle(
            angle=list(deltas), speed=speed or self.config.get("joint_speed", 20),
            relative=True, is_radian=False, wait=wait,
            timeout=MOTION_TIMEOUT_S if wait else None,
        ), "set_servo_angle(relative)")

    def check_joint_target(self, angles: list[float]) -> str | None:
        """Soft-limit check, plus the controller's own verdict when connected."""
        reason = super().check_joint_target(angles)
        if reason or self._api is None:
            return reason
        try:
            # (code, limit) — `limit` is only meaningful when code == 0, and is
            # None when the controller couldn't answer. Truthiness-testing the
            # tuple itself would reject every target.
            code, limit = self._api.is_joint_limit(list(angles), is_radian=False)
            if code == 0 and limit is True:
                return "target is outside the controller's joint limits"
        except Exception:
            pass  # advisory only — the controller rejects the move anyway
        return None

    @property
    def limits(self) -> ArmLimits:
        """Configured soft limits, backfilled with the SDK's per-model joint table.

        Without this there are no joint soft limits at all: the base check returns
        early when ``joints`` is None, leaving the controller as the only backstop.
        """
        limits = super().limits
        if limits.joints is None and self._joint_limits:
            limits.joints = self._joint_limits
        return limits

    @property
    def axis_count(self) -> int:
        # Cached from the populated report in _identify(). Never read api.axis
        # directly: it defaults to 7 until the first rich report lands, so a jog in
        # that window would build 7 joint values for a 6-axis arm.
        if self._axis is not None:
            return self._axis
        return super().axis_count

    # --- gripper -------------------------------------------------------------
    def _configured_gripper(self) -> GripperKind:
        kind = str(self.config.get("gripper", "auto")).lower()
        return "unknown" if kind == "auto" else kind  # type: ignore[return-value]

    def _resolve_gripper(self, api: "XArmAPI") -> GripperKind:
        """`auto` picks the end-effector API matching the connected model.

        Same rule as scripts/init_xarm.py — a Lite 6 carries the pneumatic
        gripper, everything else the parallel one.
        """
        configured = self._configured_gripper()
        if configured != "unknown":
            return configured
        try:
            return "lite6" if api.device_type == DEVICE_TYPE_LITE6 else "parallel"
        except Exception:
            return "unknown"

    @property
    def gripper_info(self) -> GripperInfo:
        parallel = self._gripper_kind == "parallel"
        return GripperInfo(
            kind=self._gripper_kind,
            supports_width=parallel,
            min_width=PARALLEL_MIN if parallel else 0.0,
            max_width=PARALLEL_MAX if parallel else 0.0,
            units="counts",
            stroke_m=float(self.config.get("gripper_stroke_m", PARALLEL_STROKE_M)) if parallel else 0.0,
        )

    def _prepare_gripper(self) -> "XArmAPI":
        """Enable the end effector once per connection."""
        api = self._require()
        if self._gripper_kind in ("none", "unknown"):
            raise DriverError(f"no gripper configured for {self.device_id}")
        if not self._gripper_ready:
            if self._gripper_kind == "parallel":
                self._check(api.set_gripper_mode(0), "set_gripper_mode")
                self._check(api.set_gripper_enable(True), "set_gripper_enable")
                self._check(api.set_gripper_speed(PARALLEL_SPEED), "set_gripper_speed")
            elif self._gripper_kind == "bio":
                self._check(api.set_bio_gripper_enable(True), "set_bio_gripper_enable")
            self._gripper_ready = True
        return api

    def grip(self, width: float | None = None, force: float | None = None) -> None:
        """Close the gripper. ``width`` is in controller counts (see gripper_info.units).

        An out-of-range width is rejected rather than clamped: a caller that hands
        us metres (0.011) would otherwise silently get "fully closed" and crush
        whatever is in the jaws. Convert with ``width_from_metres()`` first.

        ``force`` is rejected outright — this SDK exposes no force control for any
        of these grippers, so accepting it would silently drop a safety-relevant
        argument the caller believes is being honoured.
        """
        if force is not None:
            raise ValueError(
                f"{self._gripper_kind} gripper has no force control in this SDK — "
                "'force' would be silently ignored"
            )
        api = self._prepare_gripper()
        if self._gripper_kind == "parallel":
            if width is None:
                pos = PARALLEL_MIN
            elif not PARALLEL_MIN <= width <= PARALLEL_MAX:
                raise DriverError(
                    f"gripper width {width!r} is outside {PARALLEL_MIN:g}..{PARALLEL_MAX:g} counts "
                    f"— pass counts, not metres (use width_from_metres())"
                )
            elif 0.0 < width < 1.0:
                # A range check alone does NOT catch the metres bug: 0.011 m is a
                # perfectly in-range count value that happens to mean "shut". One
                # count is 0.1 mm, so a sub-count target is never a real request —
                # it is a metre value that skipped width_from_metres().
                raise DriverError(
                    f"gripper width {width!r} is below one count (0.1 mm) — this looks "
                    f"like metres, not counts; convert with width_from_metres() "
                    f"(pass 0 or None to close fully)"
                )
            else:
                pos = float(width)
            self._check(api.set_gripper_position(pos, wait=True), "set_gripper_position")
        elif self._gripper_kind == "lite6":
            api.close_lite6_gripper()
        elif self._gripper_kind == "bio":
            self._check(api.close_bio_gripper(wait=True), "close_bio_gripper")

    def release(self) -> None:
        api = self._prepare_gripper()
        if self._gripper_kind == "parallel":
            self._check(api.set_gripper_position(PARALLEL_MAX, wait=True), "set_gripper_position")
        elif self._gripper_kind == "lite6":
            api.open_lite6_gripper()
        elif self._gripper_kind == "bio":
            self._check(api.open_bio_gripper(wait=True), "open_bio_gripper")

    def gripper_width(self) -> float | None:
        """Opening in controller units, or None when the gripper has no feedback."""
        if self._gripper_kind != "parallel":
            return None
        code, pos = self._require().get_gripper_position()
        if code != 0:
            raise DriverError(f"get_gripper_position error {code}")
        return float(pos)
