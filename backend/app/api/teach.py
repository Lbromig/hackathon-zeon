"""Teach / jog endpoints — hand-driving the arms from the UI.

Everything here is deliberately synchronous: the xArm SDK blocks, and FastAPI
runs sync handlers in a threadpool, so a two-second `wait=True` move never
stalls the event loop (or the 2 Hz `/ws/state` stream).

Safety rules enforced here rather than in the client, because a client can't be
trusted to be the only client:

* one in-flight command per arm (non-blocking lock -> 409), since two concurrent
  SDK motion calls on one arm is a real hazard and click-spam produces exactly that
* every delta and speed is clamped to the arm's configured soft limits
* motion is refused while the arm has a latched error — clear it first
* absolute cartesian moves are refused beyond `max_move_to_jump` from the current
  pose, so a typo'd Z can't drive the TCP through the deck

`/stop` deliberately skips the busy lock: an e-stop that waits for the move it is
trying to interrupt would be useless. Do not "fix" that.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, HTTPException

from drivers import ArmDriver, ConnectionState, DriverError, InstrumentKind, Pose

from core.config import settings
from core.motion import cap_ops

from ..schemas import (
    ArmActionResult,
    ArmLimitsModel,
    ArmState,
    ArmSummary,
    CapRequest,
    EnableRequest,
    FreeDriveRequest,
    GripperModel,
    GripperRequest,
    JogRequest,
    MoveToRequest,
    PoseModel,
    StopRequest,
    TaughtPose,
)
from ..services.device_manager import device_manager

router = APIRouter(prefix="/api/arms", tags=["teach"])

CARTESIAN_AXES = ("x", "y", "z", "roll", "pitch", "yaw")
LINEAR_AXES = ("x", "y", "z")
GRIPPER_CACHE_S = 1.0    # width is a Modbus round-trip; the UI polls at 2 Hz
# Cap on orientation change for one absolute move. Bounding translation alone lets
# a same-position target whip the wrist through 180 deg at full speed.
MAX_MOVE_TO_ROTATION_DEG = 90.0
# xArm mode 2 = joint teaching. The arm reports this as its mode while hand-guiding.
FREE_DRIVE_MODE = 2

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_gripper_cache: dict[str, tuple[float, float | None]] = {}   # id -> (read_at, width)
_gripper_cache_guard = threading.Lock()   # mutated from several threadpool threads
_poses_guard = threading.Lock()


# --- helpers -----------------------------------------------------------------

def _arm(device_id: str) -> ArmDriver:
    try:
        dev = device_manager.get(device_id)
    except KeyError:
        raise HTTPException(404, f"no device {device_id}")
    if dev.info.kind != InstrumentKind.ARM:
        raise HTTPException(400, f"{device_id} is not an arm")
    return dev  # type: ignore[return-value]


def _lock_for(device_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(device_id, threading.Lock())


def _busy(device_id: str) -> bool:
    return _lock_for(device_id).locked()


def _limits_model(arm: ArmDriver) -> ArmLimitsModel:
    lim = arm.limits
    return ArmLimitsModel(
        joints=[[lo, hi] for lo, hi in lim.joints] if lim.joints else None,
        max_jog_linear=lim.max_jog_linear,
        max_jog_angular=lim.max_jog_angular,
        max_speed_linear=lim.max_speed_linear,
        max_speed_angular=lim.max_speed_angular,
        max_move_to_jump=lim.max_move_to_jump,
        max_move_to_rotation=MAX_MOVE_TO_ROTATION_DEG,
    )


def _gripper_model(arm: ArmDriver, *, read_width: bool) -> GripperModel:
    info = arm.gripper_info
    model = GripperModel(
        kind=info.kind, supports_width=info.supports_width,
        min_width=info.min_width, max_width=info.max_width,
        units=info.units, stroke_m=info.stroke_m,
    )
    if not (read_width and info.supports_width and arm.state == ConnectionState.CONNECTED):
        return model
    with _gripper_cache_guard:
        cached = _gripper_cache.get(arm.device_id)
    if cached and time.monotonic() - cached[0] < GRIPPER_CACHE_S:
        model.width = cached[1]
        return model
    try:
        model.width = arm.gripper_width()
        with _gripper_cache_guard:
            _gripper_cache[arm.device_id] = (time.monotonic(), model.width)
    except Exception:
        model.width = None
    return model


def _state(arm: ArmDriver, *, read_gripper: bool = True) -> ArmState:
    state = ArmState(
        id=arm.device_id,
        state=arm.state.value,
        connected=arm.state == ConnectionState.CONNECTED,
        busy=_busy(arm.device_id),
        gripper=_gripper_model(arm, read_width=read_gripper),
    )
    if not state.connected:
        return state
    try:
        # PoseModel rejects non-finite values; do the same for joints by hand so a
        # garbage reading surfaces as `detail` instead of failing to serialize and
        # 500-ing the 2 Hz poller.
        state.pose = PoseModel(**arm.get_pose().__dict__)
        joints = arm.get_joints()
        if not all(math.isfinite(j) for j in joints):
            raise ValueError(f"non-finite joint reading {joints}")
        state.joints = joints
    except Exception as e:
        state.detail = str(e)
    status = _status(arm)
    state.error_code = status.get("error_code")
    state.warn_code = status.get("warn_code")
    state.free_drive = status.get("mode") == FREE_DRIVE_MODE
    return state


def _status(arm: ArmDriver) -> dict[str, Any]:
    try:
        return arm.status()
    except Exception:
        return {}


def _require_movable(arm: ArmDriver) -> None:
    """Refuse motion on a disconnected or faulted arm.

    Fails *closed*: if the status read itself fails we refuse, rather than reading
    a missing ``error_code`` as "no fault". `_status` returns {} on any exception,
    so the naive `.get("error_code")` check waves motion onto an arm whose state we
    could not read — precisely when something is already wrong.
    """
    if arm.state != ConnectionState.CONNECTED:
        raise DriverError(f"{arm.device_id} is not connected")
    status = _status(arm)
    if "error_code" not in status:
        raise DriverError(
            f"could not read {arm.device_id} fault state "
            f"({status.get('read_errors') or 'status unavailable'}) — refusing to move"
        )
    code = status.get("error_code")
    if code:
        raise DriverError(f"arm has error {code} — clear errors before moving")


def _command(device_id: str, action: Callable[[ArmDriver], str]) -> ArmActionResult:
    """Run one arm command under the per-arm lock, always answering with fresh state."""
    arm = _arm(device_id)
    lock = _lock_for(device_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, f"{device_id} is busy with another command")
    try:
        detail, ok = action(arm), True
    except DriverError as e:
        detail, ok = str(e), False
    except ValueError as e:
        detail, ok = str(e), False
    except Exception as e:  # never leak a driver/SDK traceback as a 500
        detail, ok = f"{type(e).__name__}: {e}", False
    finally:
        lock.release()
    return ArmActionResult(ok=ok, detail=detail, state=_state(arm))


def _clamp(value: float, limit: float, what: str) -> float:
    """Reject (don't clamp) an out-of-range increment.

    Written to fail *closed*: `not (abs <= limit)` rather than `abs > limit`, so a
    non-finite value is refused instead of sailing through. Every comparison against
    NaN is False, which is why the naive form passes NaN. Schemas reject non-finite
    at the boundary too, but values also arrive from the taught-pose file on disk.
    """
    if not math.isfinite(value):
        raise ValueError(f"{what} must be a finite number, got {value!r}")
    if not abs(value) <= limit:
        raise ValueError(f"{what} {value:g} exceeds the limit of {limit:g}")
    return value


def _speed(arm: ArmDriver, requested: float | None, *, angular: bool) -> float | None:
    """Cap a requested speed.

    ``angular`` selects the joint-space cap (deg/s). Cartesian moves — including
    roll/pitch/yaw jogs — take a *linear* TCP speed in mm/s; the SDK's set_position
    has no separate angular-rate argument, so capping a wrist jog against
    max_speed_angular would silently reinterpret "60 °/s" as 60 mm/s.
    """
    if requested is None:
        return None
    if not math.isfinite(requested):
        raise ValueError(f"speed must be a finite number, got {requested!r}")
    cap = arm.limits.max_speed_angular if angular else arm.limits.max_speed_linear
    return max(1.0, min(float(requested), cap))


def _assert_reachable_in_one_move(arm: ArmDriver, target: Pose) -> float:
    """Refuse an absolute cartesian target too far from the current pose.

    Bounds translation *and* rotation: a target with identical xyz but yaw 180° away
    measures as "0 mm travelled" yet whips the wrist through half a turn. Shared by
    /move_to and the taught-pose replay, which previously skipped the check entirely.
    """
    current = arm.get_pose()
    for name, value in vars(target).items():
        if not math.isfinite(value):
            raise ValueError(f"target {name} must be a finite number, got {value!r}")
    jump = _distance(current, target)
    if not jump <= arm.limits.max_move_to_jump:
        raise ValueError(
            f"target is {jump:.0f} mm away, over the {arm.limits.max_move_to_jump:g} mm "
            "single-move limit — jog closer first"
        )
    turn = _rotation_delta(current, target)
    if not turn <= MAX_MOVE_TO_ROTATION_DEG:
        raise ValueError(
            f"target rotates {turn:.0f}°, over the {MAX_MOVE_TO_ROTATION_DEG:g}° "
            "single-move limit — jog the wrist closer first"
        )
    # Cartesian targets must also respect the joint soft limits, or a limit added to
    # keep end-effector tooling clear of the arm can be walked straight past by a
    # move_to. Advisory (IK picks one branch) but it catches the common case.
    reason = arm.check_pose_target(target)
    if reason:
        raise ValueError(reason)
    return jump


# --- read --------------------------------------------------------------------

@router.get("", response_model=list[ArmSummary])
def list_arms() -> list[ArmSummary]:
    out = []
    for dev in device_manager.all():
        if dev.info.kind != InstrumentKind.ARM:
            continue
        arm: ArmDriver = dev  # type: ignore[assignment]
        out.append(ArmSummary(
            id=arm.device_id, name=arm.info.name, model=arm.info.model,
            state=arm.state.value, connected=arm.state == ConnectionState.CONNECTED,
            axis_count=arm.axis_count,
            gripper=_gripper_model(arm, read_width=False),
            limits=_limits_model(arm),
        ))
    return out


@router.get("/{device_id}/state", response_model=ArmState)
def arm_state(device_id: str) -> ArmState:
    return _state(_arm(device_id))


# --- motion ------------------------------------------------------------------

@router.post("/{device_id}/jog", response_model=ArmActionResult)
def jog(device_id: str, req: JogRequest) -> ArmActionResult:
    """One discrete increment on one axis. Cartesian mm/deg, or a single joint."""
    def action(arm: ArmDriver) -> str:
        _require_movable(arm)
        axis = req.axis.lower()
        if req.space == "cartesian":
            if axis not in CARTESIAN_AXES:
                raise ValueError(f"unknown cartesian axis {req.axis!r}")
            linear = axis in LINEAR_AXES
            limit = arm.limits.max_jog_linear if linear else arm.limits.max_jog_angular
            delta = _clamp(req.delta, limit, f"jog {axis}")
            kw = {"dx": 0.0, "dy": 0.0, "dz": 0.0, "droll": 0.0, "dpitch": 0.0, "dyaw": 0.0}
            kw[{"x": "dx", "y": "dy", "z": "dz",
                "roll": "droll", "pitch": "dpitch", "yaw": "dyaw"}[axis]] = delta
            # Cartesian moves take a linear TCP speed (mm/s) even for a wrist jog —
            # see _speed(). Capping against max_speed_angular here would reinterpret
            # the number rather than limit the rotation rate.
            arm.move_relative(speed=_speed(arm, req.speed, angular=False), **kw)
            return f"jog {axis} {delta:+g}{'mm' if linear else '°'}"

        index = _joint_index(axis, arm.axis_count)
        delta = _clamp(req.delta, arm.limits.max_jog_angular, f"jog {axis}")
        deltas = [0.0] * arm.axis_count
        deltas[index] = delta
        _preflight_joints(arm, [c + d for c, d in zip(arm.get_joints(), deltas)])
        arm.move_joints_relative(deltas, speed=_speed(arm, req.speed, angular=True))
        return f"jog J{index + 1} {delta:+g}°"

    return _command(device_id, action)


@router.post("/{device_id}/move_to", response_model=ArmActionResult)
def move_to(device_id: str, req: MoveToRequest) -> ArmActionResult:
    """Absolute move. Joint targets are exact; cartesian targets are distance-capped."""
    if (req.pose is None) == (req.joints is None):
        raise HTTPException(400, "provide exactly one of 'pose' or 'joints'")

    def action(arm: ArmDriver) -> str:
        _require_movable(arm)
        if req.joints is not None:
            if len(req.joints) != arm.axis_count:
                raise ValueError(f"expected {arm.axis_count} joint angles, got {len(req.joints)}")
            _preflight_joints(arm, req.joints)
            arm.move_joints(req.joints, speed=_speed(arm, req.speed, angular=True))
            return "move to joint target"

        target = Pose(**req.pose.model_dump())  # type: ignore[union-attr]
        jump = _assert_reachable_in_one_move(arm, target)
        arm.move_to(target, speed=_speed(arm, req.speed, angular=False))
        return f"move to pose ({jump:.1f} mm travelled)"

    return _command(device_id, action)


@router.post("/{device_id}/home", response_model=ArmActionResult)
def home(device_id: str) -> ArmActionResult:
    def action(arm: ArmDriver) -> str:
        _require_movable(arm)
        arm.home()
        return "homed"
    return _command(device_id, action)


@router.post("/{device_id}/gripper", response_model=ArmActionResult)
def gripper(device_id: str, req: GripperRequest) -> ArmActionResult:
    def action(arm: ArmDriver) -> str:
        if arm.state != ConnectionState.CONNECTED:
            raise DriverError(f"{arm.device_id} is not connected")
        info = arm.gripper_info
        with _gripper_cache_guard:
            _gripper_cache.pop(arm.device_id, None)
        if req.action == "open":
            arm.release()
            return "gripper open"
        if req.action == "close":
            arm.grip()
            return "gripper closed"
        if not info.supports_width:
            raise ValueError(f"the {info.kind} gripper has no commandable width")
        if req.width is None:
            raise ValueError("'set' requires a width")
        # Reject rather than clamp — same reason the driver does: a clamped width
        # is how a unit mix-up turns into a crushed tube.
        if not info.min_width <= req.width <= info.max_width:
            raise ValueError(
                f"width {req.width:g} is outside {info.min_width:g}..{info.max_width:g} "
                f"{info.units}"
            )
        arm.grip(width=req.width)
        return f"gripper set to {req.width:g} {info.units}"
    return _command(device_id, action)


# --- state changes -----------------------------------------------------------

@router.post("/{device_id}/enable", response_model=ArmActionResult)
def enable(device_id: str, req: EnableRequest) -> ArmActionResult:
    def action(arm: ArmDriver) -> str:
        arm.enable(req.on)
        return "motors enabled" if req.on else "motors disabled (brakes engaged)"
    return _command(device_id, action)


@router.post("/{device_id}/cap", response_model=ArmActionResult)
def cap(device_id: str, req: CapRequest) -> ArmActionResult:
    """Grab / release / unscrew a cap.

    `unscrew` is a ratchet: the tool cabling cannot take a continuous 360°, so the wrist
    takes 180° bites, opening and unwinding between them. It pre-flights every wrist
    angle it would visit before moving at all — stopping halfway would leave the cap
    partly unscrewed with the wrist wound round.
    """
    def action(arm: ArmDriver) -> str:
        _require_movable(arm)
        cfg = cap_ops.CapConfig(
            grip_counts=req.width,
            half_turns=req.half_turns,
            joint_speed=_speed(arm, req.speed, angular=True) or cap_ops.DEFAULT_JOINT_SPEED,
        )
        try:
            if req.action == "grab":
                return cap_ops.grab_cap(arm, cfg)
            if req.action == "ungrab":
                return cap_ops.ungrab_cap(arm)
            return cap_ops.unscrew_cap(arm, cfg)
        except cap_ops.CapOpError as e:
            raise ValueError(str(e)) from e
    return _command(device_id, action)


@router.post("/{device_id}/free_drive", response_model=ArmActionResult)
def free_drive(device_id: str, req: FreeDriveRequest) -> ArmActionResult:
    """Hand-guiding: make the arm back-drivable so an operator can position it.

    This is how the first teach of a pose happens — push the arm where you want it,
    then save. Two caveats the UI must surface:

    * The arm holds against gravity using the configured payload. If that is wrong
      the arm sinks (or climbs) when released — support it before enabling.
    * Programmed motion does not behave normally while it is on, so it is turned
      off again before any commanded move.
    """
    def action(arm: ArmDriver) -> str:
        if arm.state != ConnectionState.CONNECTED:
            raise DriverError(f"{arm.device_id} is not connected")
        try:
            arm.set_free_drive(req.on)
        except NotImplementedError as e:
            raise DriverError(str(e)) from e
        return ("hand-guiding ON — arm is back-drivable, support it"
                if req.on else "hand-guiding off — back in position control")
    return _command(device_id, action)


@router.post("/{device_id}/clear_errors", response_model=ArmActionResult)
def clear_errors(device_id: str) -> ArmActionResult:
    def action(arm: ArmDriver) -> str:
        arm.clear_errors()
        return "errors cleared, arm re-enabled"
    return _command(device_id, action)


@router.post("/{device_id}/stop", response_model=ArmActionResult)
def stop(device_id: str, req: StopRequest) -> ArmActionResult:
    """E-stop. Bypasses the busy lock on purpose — it exists to interrupt a move."""
    arm = _arm(device_id)
    try:
        arm.stop(emergency=req.emergency)
        detail = "EMERGENCY STOP" if req.emergency else "stopped"
        ok = True
    except Exception as e:
        detail, ok = f"stop failed: {e}", False
    return ArmActionResult(ok=ok, detail=detail, state=_state(arm, read_gripper=False))


# --- taught poses ------------------------------------------------------------

@router.get("/{device_id}/poses", response_model=list[TaughtPose])
def list_poses(device_id: str) -> list[TaughtPose]:
    _arm(device_id)
    return [TaughtPose(**p) for p in _load_poses().get(device_id, {}).values()]


@router.post("/{device_id}/poses", response_model=list[TaughtPose])
def save_pose(device_id: str, req: TaughtPose) -> list[TaughtPose]:
    """Snapshot where the arm is right now under a name.

    Both spaces are stored, but go-to replays the *joints*: those were physically
    reached, so there's no IK branch to guess at.
    """
    arm = _arm(device_id)
    if arm.state != ConnectionState.CONNECTED:
        raise HTTPException(400, f"{device_id} is not connected")
    try:
        entry = TaughtPose(
            name=req.name.strip(),
            pose=PoseModel(**arm.get_pose().__dict__),
            joints=arm.get_joints(),
            gripper_width=_gripper_model(arm, read_width=True).width,
            note=req.note,
            saved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
    except DriverError as e:
        raise HTTPException(503, str(e))
    with _poses_guard:
        store = _load_poses()
        store.setdefault(device_id, {})[entry.name] = entry.model_dump()
        _write_poses(store)
    return [TaughtPose(**p) for p in _load_poses().get(device_id, {}).values()]


@router.delete("/{device_id}/poses/{name}", response_model=list[TaughtPose])
def delete_pose(device_id: str, name: str) -> list[TaughtPose]:
    _arm(device_id)
    with _poses_guard:
        store = _load_poses()
        if store.get(device_id, {}).pop(name, None) is None:
            raise HTTPException(404, f"no taught pose {name!r} for {device_id}")
        _write_poses(store)
    return [TaughtPose(**p) for p in _load_poses().get(device_id, {}).values()]


@router.post("/{device_id}/poses/{name}/goto", response_model=ArmActionResult)
def goto_pose(device_id: str, name: str, speed: float | None = None) -> ArmActionResult:
    saved = _load_poses().get(device_id, {}).get(name)
    if saved is None:
        raise HTTPException(404, f"no taught pose {name!r} for {device_id}")
    entry = TaughtPose(**saved)

    def action(arm: ArmDriver) -> str:
        _require_movable(arm)
        if entry.joints and len(entry.joints) == arm.axis_count:
            _preflight_joints(arm, entry.joints)
            arm.move_joints(entry.joints, speed=_speed(arm, speed, angular=True))
            return f"went to {name!r} (joint replay)"
        if entry.pose is None:
            raise ValueError(f"taught pose {name!r} has no usable target")
        # Same cap as /move_to. This path is reached whenever the saved joints are
        # missing or the wrong length (hand-edited file, pose saved under a
        # different axis count), so it must not be the one route that skips the guard.
        target = Pose(**entry.pose.model_dump())
        jump = _assert_reachable_in_one_move(arm, target)
        arm.move_to(target, speed=_speed(arm, speed, angular=False))
        return f"went to {name!r} (cartesian, {jump:.1f} mm travelled)"

    return _command(device_id, action)


# --- internals ---------------------------------------------------------------

def _joint_index(axis: str, axis_count: int) -> int:
    if not axis.startswith("j") or not axis[1:].isdigit():
        raise ValueError(f"unknown joint axis {axis!r} (expected j1..j{axis_count})")
    index = int(axis[1:]) - 1
    if not 0 <= index < axis_count:
        raise ValueError(f"joint {axis!r} out of range (arm has {axis_count} axes)")
    return index


def _preflight_joints(arm: ArmDriver, target: list[float]) -> None:
    reason = arm.check_joint_target(target)
    if reason:
        raise ValueError(reason)


def _rotation_delta(a: Pose, b: Pose) -> float:
    """Largest single-axis orientation change (deg), wrapped to +/-180."""
    return max(abs((getattr(b, axis) - getattr(a, axis) + 180.0) % 360.0 - 180.0)
               for axis in ("roll", "pitch", "yaw"))


def _distance(a: Pose, b: Pose) -> float:
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2) ** 0.5


def _load_poses() -> dict[str, dict[str, dict[str, Any]]]:
    path = settings.teach_poses_file
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[teach] could not read {path}: {e}")
        return {}


def _write_poses(store: dict[str, dict[str, dict[str, Any]]]) -> None:
    path = settings.teach_poses_file
    # dirname("poses.json") is "" — makedirs("") raises FileNotFoundError and would
    # lose the pose being saved when HZ_TEACH_POSES_FILE is a bare filename.
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, path)   # atomic: never leave a half-written pose library
