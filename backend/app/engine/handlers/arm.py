"""Arm action handlers: waypoint, relative, gripper, decap, traverse (R-ARM-1…6).

Five plain blocking functions. Each is called as ``fn(action, ctx)`` and returns the
``Outputs`` model ``OUTPUTS_FOR_KIND`` maps its kind to. None of them builds an
``ActionResult``, emits an event, catches its own errors or decides a retry — the runner
does all of that (§2.4, R-LOG-5), so a failure here is a raise and nothing else.

What these handlers are responsible for, and why each one is *here* rather than in a driver
--------------------------------------------------------------------------------------------
* **Refusing before moving.** Every pre-flight in this module runs to completion before the
  first commanded motion. A waypoint the acting arm does not own, an untaught waypoint, a
  joint target outside the soft limits, a decap bite that would drive J6 past its ceiling, a
  traverse whose fourth waypoint is untaught — all are refusals, not partial executions.
  Stopping halfway through a travel path leaves the arm somewhere nobody chose: over the
  deck, or between the two tables holding an open tube.
* **Preferring joint replay.** A taught waypoint's joint angles were *physically reached*,
  so replaying them has no IK branch to guess at. Cartesian is the fallback for a waypoint
  stored without usable joints, and for the offset case below.
* **Cooperative pause.** `arm.decap` and `arm.traverse` take seconds to tens of seconds, so
  they call ``ctx.checkpoint()`` at their own natural sub-step boundaries (D9). Where those
  boundaries are is a safety decision, not a convenience: between decap bites the jaws are
  open and the wrist is home, and between traverse waypoints the arm is at a taught pose.
* **Speeds by tier only.** Every speed comes from ``ctx.arm_speeds()``, which resolves the
  action's tier against *this* arm's soft limits (R-ENG-14). No raw number appears here.

Device claims (D21/R-ENG-11) are deliberately **not** here
----------------------------------------------------------
There is no per-arm lock in this module. Two engine actions cannot contend for one arm in
the first place — the runner is a single worker thread (D1) — so a claim taken here would
buy nothing and would be one more thing standing between the operator and
``POST /api/arms/{id}/stop``, which deliberately bypasses the teach lock today
(`backend/app/api/teach.py:17-19`) and must keep doing so. The contention that *is* real is
engine-versus-teach-API, which is process-wide and therefore the runner's or the API's to
broker, with the stop path exempt. See the report accompanying this slice.
"""
from __future__ import annotations

import math
from typing import Any

from core import speeds as speed_tiers
from core import waypoints
from core.motion import cap_ops
from drivers.capabilities.arm import ArmDriver, Pose

from ..actions import (ArmDecap, ArmGripper, ArmRelative, ArmTraverse, ArmWaypoint,
                       DecapOutputs, GripperOutputs, MoveOutputs, TraverseOutputs, handler)
from ..context import ActionContext

#: Settle time between decap steps, seconds. A real cap needs a moment for the jaws to
#: finish opening before the wrist unwinds; the mock does not, and 15 steps x 0.3 s in a
#: unit test is 4.5 s of nothing. Module-level so a test can zero it without reaching
#: into `CapConfig` through the handler.
DECAP_SETTLE_S = 0.3

#: Which motion path a waypoint move took, recorded so the log can answer "did that replay
#: the taught joints or solve IK?" — the two fail differently and the difference matters
#: after an unexpected trajectory.
JOINT_REPLAY = "joint_replay"
JOINT_REPLAY_THEN_OFFSET = "joint_replay+cartesian_offset"
CARTESIAN = "cartesian"


# --- shared helpers ----------------------------------------------------------------

def _finite(name: str, value: float) -> float:
    """Refuse a non-finite number, failing closed.

    `allow_inf_nan=False` on the action models already blocks this at the API boundary, but
    offsets also arrive from the taught-pose file on disk and from a blackboard slot, and
    every comparison against NaN is False — so a naive range check waves it through.
    """
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return float(value)


def _preflight_joints(arm: ArmDriver, target: list[float], *, what: str) -> None:
    """Refuse a joint target outside the soft limits. Never moves."""
    reason = arm.check_joint_target(target, current=_joints_or_none(arm))
    if reason:
        raise ValueError(f"{what} refused before moving: {reason}")


def _preflight_pose(arm: ArmDriver, target: Pose, *, what: str) -> None:
    """Refuse a cartesian target the driver's own IK check rejects."""
    for name, value in vars(target).items():
        _finite(f"target {name}", value)
    reason = arm.check_pose_target(target)
    if reason:
        raise ValueError(f"{what} refused before moving: {reason}")


def _joints_or_none(arm: ArmDriver) -> list[float] | None:
    """Current joints, or None if they cannot be read.

    Passed to `check_joint_target` as ``current`` so an arm already parked outside a soft
    limit — because the limit was tightened, or it was pushed by hand — can still be moved
    *back* toward the range. A limit with no escape traps the arm (see the capability's
    docstring); refusing to read is not a reason to close that escape.
    """
    try:
        return list(arm.get_joints())
    except Exception:
        return None


def _pose_list(pose: Pose) -> list[float]:
    return [pose.x, pose.y, pose.z, pose.roll, pose.pitch, pose.yaw]


def _read_pose(arm: ArmDriver) -> list[float]:
    """Pose as a list, or empty if unreadable. Reporting must not fail a good move."""
    try:
        return _pose_list(arm.get_pose())
    except Exception:
        return []


def _read_joints(arm: ArmDriver) -> list[float]:
    try:
        return list(arm.get_joints())
    except Exception:
        return []


def _speed_report(resolved: speed_tiers.Speeds, path: str) -> dict[str, Any]:
    """`MoveOutputs.resolved_speed` plus which motion path was commanded.

    The path belongs in the outputs — "prefer joint replay" is only a checkable claim if the
    record says which one ran — and `resolved_speed` is the one `dict[str, Any]` field
    `MoveOutputs` has. `actions.py` is frozen for Wave 1, so adding a `path` field to the
    model is a contract change and is reported rather than made. Documented so nobody
    "tidies" the key away.
    """
    report = dict(resolved.as_dict())
    report["path"] = path
    return report


def _usable_joints(arm: ArmDriver, joints: list[float] | None) -> list[float] | None:
    """Taught joints, but only if they can be replayed on *this* arm.

    A length mismatch means the pose was taught on a different axis count (or the file was
    hand-edited). Replaying six angles on a seven-axis arm would silently leave the last
    joint wherever it happened to be, so this falls back to cartesian instead.
    """
    if not joints:
        return None
    if len(joints) != arm.axis_count:
        return None
    return [_finite("taught joint angle", j) for j in joints]


def move_to_waypoint(arm: ArmDriver, resolved: waypoints.ResolvedWaypoint,
                     *, joint_speed: float | None, linear_speed: float | None,
                     dx: float = 0.0, dy: float = 0.0, dz: float = 0.0) -> str:
    """Move ``arm`` to a resolved waypoint, offsets applied. Returns the path taken.

    **Joints first.** Those angles were physically reached on this arm, so there is no IK
    branch to guess at — the same reasoning as the teach API's replay and
    `waypoints.ResolvedWaypoint`.

    **Offsets (R-ARM-2) break joint replay**, because the taught angles put the TCP at the
    taught pose and nowhere else. Rather than hand the whole move to IK, this replays the
    joints to reach the taught pose exactly and *then* applies the offset as a small
    relative cartesian move: the arm reaches the taught configuration by the trusted path
    and the IK-solved part of the trajectory is only the few millimetres that were asked
    for. Both halves are pre-flighted before the first of them runs.

    The offsets are applied in the frame the waypoint is stored in — the arm's base frame,
    which is what `get_pose()`/`move_to()` speak. `ArmWaypoint`'s docstring calls it "TCP
    frame"; see the report for that discrepancy. Nothing in the workflow's plan data uses a
    non-axis-aligned approach, so the two coincide for every current caller.

    The taught ``gripper_width`` is deliberately *not* replayed: opening or closing the jaws
    is `arm.gripper`, an action an operator can see in the plan, not a side effect of a move.
    """
    dx, dy, dz = (_finite("dx", dx), _finite("dy", dy), _finite("dz", dz))
    offset = any(v != 0.0 for v in (dx, dy, dz))
    joints = _usable_joints(arm, resolved.joints)
    label = f"waypoint ({resolved.device!r}, {resolved.name!r})"

    if joints is not None and not offset:
        _preflight_joints(arm, joints, what=label)
        arm.move_joints(joints, speed=joint_speed)
        return JOINT_REPLAY

    if joints is not None:
        # Pre-flight *both* halves up front: a joint replay that lands somewhere the offset
        # cannot legally leave is a move that has to be undone by hand.
        _preflight_joints(arm, joints, what=label)
        taught = Pose(*(_finite("taught pose component", v) for v in resolved.xyz_rpy))
        target = Pose(taught.x + dx, taught.y + dy, taught.z + dz,
                      taught.roll, taught.pitch, taught.yaw)
        _preflight_pose(arm, target, what=f"{label} with offsets")
        arm.move_joints(joints, speed=joint_speed)
        arm.move_relative(dx=dx, dy=dy, dz=dz, speed=linear_speed, wait=True)
        return JOINT_REPLAY_THEN_OFFSET

    taught = Pose(*(_finite("taught pose component", v) for v in resolved.xyz_rpy))
    target = Pose(taught.x + dx, taught.y + dy, taught.z + dz,
                  taught.roll, taught.pitch, taught.yaw)
    _preflight_pose(arm, target, what=label)
    arm.move_to(target, speed=linear_speed)
    return CARTESIAN


# --- arm.waypoint ------------------------------------------------------------------

@handler("arm.waypoint")
def waypoint(action: ArmWaypoint, ctx: ActionContext) -> MoveOutputs:
    """Move to a named waypoint, with optional x/y/z offsets (R-ARM-2). Also the home move.

    Resolution goes through `core.waypoints.resolve`, which is the single place ownership
    lives (R-WP-1/2): a waypoint the acting device does not own raises `WaypointNotOwned`
    naming both the owner and the actor, and an untaught pairing raises `WaypointNotTaught`
    carrying the `(device, name)` pair. Both propagate — the runner turns them into a failed
    action with a readable reason — and both happen **before the driver is even asked for
    its pose**, so a refusal cannot be a partial move.
    """
    arm = ctx.devices.require_arm(action.device)
    resolved = waypoints.resolve(action.device, action.waypoint)

    tier = ctx.arm_speeds()
    pose_before, joints_before = _read_pose(arm), _read_joints(arm)
    path = move_to_waypoint(arm, resolved,
                            joint_speed=tier.angular, linear_speed=tier.linear,
                            dx=action.dx, dy=action.dy, dz=action.dz)
    ctx.progress(f"at waypoint {action.waypoint!r} via {path}",
                 waypoint=action.waypoint, motion_path=path)

    return MoveOutputs(
        pose_before=pose_before, pose_after=_read_pose(arm),
        joints_before=joints_before, joints_after=_read_joints(arm),
        waypoint=action.waypoint,
        offsets_mm={"dx": action.dx, "dy": action.dy, "dz": action.dz},
        resolved_speed=_speed_report(tier, path),
    )


# --- arm.move_relative -------------------------------------------------------------

def _deltas_from_slot(action: ArmRelative, ctx: ActionContext) -> tuple[float, float, float]:
    """Read dx/dy/dz out of a blackboard slot (R-ENG, `ArmRelative.from_slot`).

    The slot holds one typed value and the handler reads its fields in Python — there is no
    dotted-path resolver (review S2). Three shapes are accepted because three producers
    exist: an `OffsetOutputs` (or its dumped dict) from the vision solve, and a plain
    ``{"x","y","z"}`` or ``{"dx","dy","dz"}`` mapping.

    An axis reported as ``None`` is **not observable**, which means something different from
    ``0.0`` — that distinction is the whole point of `OffsetOutputs.residual_offset_mm`. So
    an unobservable axis contributes no motion *and* raises a warning the operator can
    reach (R-LOG-6), and a slot with nothing observable at all is refused rather than
    executed as a move to nowhere.
    """
    slot = action.from_slot
    assert slot is not None
    value: Any = ctx.blackboard.get(slot)

    source = getattr(value, "residual_offset_mm", None)
    if source is None and isinstance(value, dict):
        source = value.get("residual_offset_mm")
    if source is None:
        source = value
    if not isinstance(source, dict):
        source = {k: getattr(source, k, None) for k in ("x", "y", "z")}

    out: list[float] = []
    observed = 0
    for axis, alias in (("x", "dx"), ("y", "dy"), ("z", "dz")):
        raw = source.get(axis, source.get(alias))
        if raw is None:
            ctx.warn("unobservable_axis",
                     f"slot {slot!r} reports no {axis} — commanding no {axis} motion. "
                     f"An unobservable axis is not a zero offset.")
            out.append(0.0)
            continue
        out.append(_finite(f"slot {slot!r} {axis}", float(raw)))
        observed += 1

    if not observed:
        raise ValueError(
            f"slot {slot!r} has no observable axis, so there is no move to make. "
            f"Refusing rather than commanding a zero move that would read as success. "
            f"Slot value: {value!r}")
    return out[0], out[1], out[2]


@handler("arm.move_relative")
def move_relative(action: ArmRelative, ctx: ActionContext) -> MoveOutputs:
    """Move by an offset (R-ARM-3), optionally taking dx/dy/dz from a blackboard slot.

    Orientation deltas always come from the literal fields: a slot holds a translation, and
    reinterpreting one of its numbers as degrees is the class of mistake `core.speeds`
    documents at length.
    """
    arm = ctx.devices.require_arm(action.device)

    if action.from_slot is not None:
        dx, dy, dz = _deltas_from_slot(action, ctx)
    else:
        dx = _finite("dx", action.dx)
        dy = _finite("dy", action.dy)
        dz = _finite("dz", action.dz)
    droll = _finite("droll", action.droll)
    dpitch = _finite("dpitch", action.dpitch)
    dyaw = _finite("dyaw", action.dyaw)

    tier = ctx.arm_speeds()
    pose_before, joints_before = _read_pose(arm), _read_joints(arm)

    # Pre-flight the *resulting* pose, not the delta: the soft limits bound where the arm
    # ends up, and a driver that can solve IK will refuse a target that leaves the envelope.
    if pose_before:
        target = Pose(pose_before[0] + dx, pose_before[1] + dy, pose_before[2] + dz,
                      pose_before[3] + droll, pose_before[4] + dpitch, pose_before[5] + dyaw)
        _preflight_pose(arm, target, what="relative move")

    arm.move_relative(dx=dx, dy=dy, dz=dz, droll=droll, dpitch=dpitch, dyaw=dyaw,
                      speed=tier.linear, wait=True)

    return MoveOutputs(
        pose_before=pose_before, pose_after=_read_pose(arm),
        joints_before=joints_before, joints_after=_read_joints(arm),
        # Translation only: the field is `offsets_mm` and a degree in a millimetre field is
        # the mistake `core.speeds` documents. The orientation deltas are already in
        # `ActionResult.inputs`, which is the action's own dump.
        offsets_mm={"dx": dx, "dy": dy, "dz": dz},
        resolved_speed=_speed_report(tier, "relative"),
    )


# --- arm.gripper -------------------------------------------------------------------

def _check_width(arm: ArmDriver, width: float) -> float:
    """Refuse — never clamp — an out-of-range gripper width.

    The capability contract already says drivers must reject rather than clamp, because
    clamping silently converts a unit-conversion bug (metres handed to a counts API) into a
    crushed payload. This check is here as well as in the driver for two reasons: it names
    the gripper's actual range in the refusal, and it holds for grippers whose driver does
    not enforce it — the in-memory mock stores whatever it is given. Either way the answer
    is a refusal, so the two cannot disagree in the dangerous direction.
    """
    info = arm.gripper_info
    if not info.supports_width:
        raise ValueError(
            f"the {info.kind} gripper on {arm.device_id!r} has no commandable width — "
            f"it is open/close only. Drop the `width` field.")
    _finite("gripper width", width)
    if not info.min_width <= width <= info.max_width:
        raise ValueError(
            f"gripper width {width:g} is outside {info.min_width:g}..{info.max_width:g} "
            f"{info.units} for {arm.device_id!r} — refusing rather than clamping, because a "
            f"clamped width is a unit bug that ends as a crushed payload. Convert an SI "
            f"opening with width_from_metres().")
    return float(width)


@handler("arm.gripper")
def gripper(action: ArmGripper, ctx: ActionContext) -> GripperOutputs:
    """Open or close the gripper (R-ARM-4), optionally to a width.

    ``width`` drives the same controller command in both directions — a parallel gripper has
    one "go to this opening" call, and whether that reads as opening or closing depends only
    on where the jaws are now. ``state`` therefore stays meaningful for the plan and the log
    while the commanded position is the width. Without a width, ``open`` releases fully and
    ``close`` closes fully.
    """
    arm = ctx.devices.require_arm(action.device)
    width_before = _gripper_width(arm)

    if action.width is None:
        if action.state == "open":
            arm.release()
        else:
            arm.grip()
    else:
        arm.grip(width=_check_width(arm, action.width))

    return GripperOutputs(state=action.state, width_before=width_before,
                          width_after=_gripper_width(arm))


def _gripper_width(arm: ArmDriver) -> float | None:
    """Current opening, or None — both for a gripper with no feedback and for one whose read
    failed. Reporting must not fail the action that already succeeded."""
    try:
        value = arm.gripper_width()
    except Exception:
        return None
    return None if value is None else float(value)


# --- arm.decap ---------------------------------------------------------------------

@handler("arm.decap")
def decap(action: ArmDecap, ctx: ActionContext) -> DecapOutputs:
    """Unscrew a cap: a full turn in ``step_deg`` bites, rewinding the wrist between them.

    R-ARM-5 / D12. The plan and the pre-flight are `core.motion.cap_ops`; this handler adds
    the three things only the engine can provide — the resolved speed tier, the cooperative
    pause point, and per-bite progress.

    Where the checkpoint goes is the safety decision. `cap_ops` fires ``on_bite`` after the
    unwind and *before* the re-grip: jaws open, wrist back at its starting angle, cap
    loosened by however many bites have run. Pausing there is safe and resuming is exact.
    Pausing anywhere else in the routine would hold the run with the wrist wound and the cap
    half off, which is the state D9 exists to avoid.

    The jaws must already be closed on the cap — a preceding `arm.gripper` close does that,
    and ``grip_counts`` should mirror its width. This handler never closes them first,
    because "close fully" on a cap that is not between the jaws is a crushed cap and no
    amount of care here can tell the difference.
    """
    arm = ctx.devices.require_arm(action.device)
    tier = ctx.arm_speeds()
    cfg = cap_ops.CapConfig(
        grip_counts=(None if action.grip_counts is None
                     else _check_width(arm, action.grip_counts)),
        step_deg=action.step_deg,
        turns=action.turns,
        joint_speed=tier.angular,
        settle_s=DECAP_SETTLE_S,
    )

    def on_bite(index: int, bites: int, rotated: float) -> None:
        ctx.progress(
            f"decap bite {index}/{bites}: cap turned {rotated:.0f}° of {cfg.total_deg:.0f}°, "
            f"jaws open, wrist back at start",
            bite=index, bites=bites, rotated_deg=rotated)
        # After the report, so a paused run shows the bite it completed rather than
        # appearing to hang before it.
        ctx.checkpoint()

    result = cap_ops.run_ratchet(arm, cfg, on_bite=on_bite)

    if not result.returned:
        # Reported, not raised: the cap is off and the jaws are open, so the action did what
        # it was for. A wrist that did not return is a mechanical problem that must reach the
        # operator (R-LOG-6) before the next decap walks the joint further.
        ctx.warn("wrist_did_not_return",
                 f"wrist ended {result.net_wrist_travel_deg:+.2f}° from where it started, "
                 f"expected 0 within {cap_ops.RETURN_TOLERANCE_DEG:g}°. Repeating decap "
                 f"will accumulate this — check the tool axis before running it again.")

    return DecapOutputs(
        bites=result.bites,
        step_deg=result.step_deg,
        total_rotation_deg=result.total_rotation_deg,
        net_wrist_travel_deg=result.net_wrist_travel_deg,
        preflight_ok=result.preflight_ok,
    )


# --- arm.traverse ------------------------------------------------------------------

def _segment_lengths(legs: list[list[float] | None]) -> list[float | None]:
    """Per-segment joint-space travel, as the largest single-joint change in degrees.

    The dominant joint rather than a norm, because a blend radius is expressed in the same
    units the controller measures the track in, and the shortest track along a segment is
    the one that bounds the radius.

    ``None`` means *unknown*, not zero: an endpoint stored cartesian-only has no joint-space
    length, and `move_to` has no radius argument at all, so a path containing one cannot be
    blended. A genuine ``0.0`` — two waypoints that coincide — is merely a segment with
    nothing to blend, which is a different thing.
    """
    out: list[float | None] = []
    for before, after in zip(legs, legs[1:]):
        if before is None or after is None:
            out.append(None)
            continue
        out.append(max((abs(b - a) for a, b in zip(before, after)), default=0.0))
    return out


def _clamp_blend(requested: float | None, segments: list[float | None]) -> float | None:
    """The radius actually usable, clamped to the shortest segment.

    A blend eats radius at *both* ends of a segment, so the bound is half the shortest one;
    the controller rejects a radius longer than the track outright. An unknown segment makes
    the path unblendable, and reporting the clamped value rather than the requested one is
    why `TraverseOutputs.blend_deg` exists.
    """
    if requested is None or requested <= 0.0:
        return None
    if any(s is None for s in segments):
        return None
    usable = [s for s in segments if s and s > 0.0]
    if not usable:
        return None
    return min(float(requested), min(usable) / 2.0)


@handler("arm.traverse")
def traverse(action: ArmTraverse, ctx: ActionContext) -> TraverseOutputs:
    """Move through an ordered list of named waypoints as one action (R-ARM-6).

    Every waypoint is resolved — ownership checked, taught-ness checked — and every joint
    target pre-flighted, **before the first move**. Refusing at waypoint four of six would
    leave the arm mid-path, which on this bench means over the deck or between the tables
    with an open tube in the jaws.

    Blending: where a radius survives the shortest-segment clamp, the intermediate moves are
    queued with ``wait=False`` so the controller arcs through the waypoints instead of
    stopping at each. ``ctx.checkpoint()`` still sits between waypoints, and it is a real
    pause point even when blended: a pause stops *issuing* moves, so the arm runs out of
    queued trajectory and comes to rest on the last waypoint it was given. That is a taught
    pose, which is the property that matters. Without a radius it is point-to-point and each
    waypoint is a hard boundary.
    """
    arm = ctx.devices.require_arm(action.device)
    tier = ctx.arm_speeds()

    # Resolve everything first. `resolve` raises on the wrong arm or an untaught pairing,
    # and doing the whole list up front is what makes those refusals rather than aborts.
    resolved = [waypoints.resolve(action.device, name) for name in action.waypoints]
    legs = [_usable_joints(arm, wp.joints) for wp in resolved]

    for wp, joints in zip(resolved, legs):
        label = f"traverse waypoint ({wp.device!r}, {wp.name!r})"
        if joints is not None:
            _preflight_joints(arm, joints, what=label)
        else:
            _preflight_pose(arm, Pose(*wp.xyz_rpy), what=label)

    blend = _clamp_blend(action.blend_deg,
                         _segment_lengths([_read_joints(arm) or None] + legs))
    if action.blend_deg and blend is None:
        ctx.warn("blend_unavailable",
                 f"blending {action.blend_deg:g}° was requested but this path cannot be "
                 f"blended (a segment has no joint-space length, or a waypoint is stored "
                 f"cartesian-only) — running it point to point instead.")

    reached: list[str] = []
    last = len(resolved) - 1
    for i, (wp, joints) in enumerate(zip(resolved, legs)):
        if i:
            # A pause here stops the arm at the previous waypoint (blended: at the last one
            # already queued). Before the move, so the pause is honoured rather than
            # noticed after the fact.
            ctx.checkpoint()
        if joints is not None:
            queue = blend is not None and i < last
            arm.move_joints(joints, speed=tier.angular, wait=not queue,
                            radius=blend if queue else None)
        else:
            arm.move_to(Pose(*wp.xyz_rpy), speed=tier.linear, wait=True)
        reached.append(wp.name)
        state = "reached" if (joints is None or blend is None or i == last) else "queued"
        ctx.progress(f"traverse {i + 1}/{len(resolved)}: {state} {wp.name}",
                     waypoint=wp.name, leg=i + 1, legs=len(resolved))

    # One wait for the whole queue: with blending the intermediate moves did not block, so
    # the action is not finished until the controller says it is.
    arm.wait_for_idle()

    return TraverseOutputs(reached=reached, blend_deg=blend,
                           resolved_speed=_speed_report(tier, "traverse"))


__all__ = ["waypoint", "move_relative", "gripper", "decap", "traverse", "move_to_waypoint",
           "DECAP_SETTLE_S", "JOINT_REPLAY", "JOINT_REPLAY_THEN_OFFSET", "CARTESIAN"]
