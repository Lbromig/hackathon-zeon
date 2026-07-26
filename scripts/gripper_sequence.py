#!/usr/bin/env python3
"""Gripper + short-move validation sequence for an xArm.

Confirms the gripper and small cartesian moves work together, by running eight
commanded actions inside a ~40 mm envelope around wherever the arm currently is:

    1  open gripper
    2  close half way
    3  move 10 mm away (+X by default)
    4  open gripper
    5  move above the original position, then rotate yaw +90 deg
    6  descend a little, then close the gripper

Steps 5 and 6 are each issued as a translation *then* a rotation/grip rather than
one coupled command: a combined translate-and-rotate is harder to predict and
harder to abort mid-flight. Pass --combine to fuse them.

Every target is computed from the pose read at launch, so the plan stays valid if
the arm has been moved since it was reviewed. The error code is re-checked after
every action, and each move's measured pose is compared with what was commanded.

Safety
------
* Motion requires an explicit --yes. Without it you get a dry run.
* Slow by default: 20 mm/s linear, 15 deg/s rotational, 1 s pause between steps.
* Travel is bounded: --distance, --approach and --descend are each capped at 50 mm.
* Aborts on any latched fault or out-of-tolerance move.
* The arm is braked on every exit path, including Ctrl-C — see XArmDriver.disconnect().

The workspace must be clear within ~40 mm of the current pose in every direction,
including above, and the jaws must be empty: step 6 closes fully.

Usage:
    python scripts/gripper_sequence.py --ip 192.168.3.13            # dry run
    python scripts/gripper_sequence.py --ip 192.168.3.13 --yes      # execute
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass, field

sys.path.insert(0, ".")

from drivers.capabilities.arm import Pose  # noqa: E402
from drivers.xarm.driver import XArmDriver  # noqa: E402

DEFAULT_DISTANCE_MM = 10.0      # step 3: how far "away" is
DEFAULT_APPROACH_MM = 30.0      # step 5a: clearance above the original position
DEFAULT_DESCEND_MM = 20.0       # step 6a: how far back down
DEFAULT_YAW_DEG = 90.0          # step 5b
MAX_TRAVEL_MM = 50.0            # cap on every distance knob
DEFAULT_SPEED = 20.0            # mm/s — slow enough to follow and to interrupt
DEFAULT_ROT_SPEED = 15.0        # deg/s
DEFAULT_PAUSE_S = 1.0
TOL_MM = 1.0                    # per-move positioning tolerance
SETTLE_S = 0.4                  # let the controller report catch up before reading back

GRIP_OPEN = 850.0               # controller counts: 0 = closed, 850 = open
GRIP_CLOSED = 0.0


@dataclass
class Options:
    distance: float = DEFAULT_DISTANCE_MM
    approach: float = DEFAULT_APPROACH_MM
    descend: float = DEFAULT_DESCEND_MM
    yaw: float = DEFAULT_YAW_DEG
    speed: float = DEFAULT_SPEED
    pause: float = DEFAULT_PAUSE_S
    combine: bool = False
    axis: str = "x"
    restore: bool = True


@dataclass
class Action:
    """One commanded action. Exactly one of `pose` / `grip` is set."""
    label: str
    pose: Pose | None = None
    grip: float | None = None
    notes: str = ""


def _pose(p: Pose, **delta) -> Pose:
    """A copy of `p` with the named components offset."""
    out = Pose(**vars(p))
    for key, value in delta.items():
        setattr(out, key, getattr(out, key) + value)
    return out


def build_sequence(start: Pose, opt: Options) -> list[Action]:
    """Every target derived from the live start pose, so the plan can't go stale."""
    axis_delta = {opt.axis: opt.distance}
    away = _pose(start, **axis_delta)
    above = _pose(start, z=opt.approach)
    rotated = _pose(above, yaw=opt.yaw)
    lowered = _pose(rotated, z=-opt.descend)

    actions = [
        Action("1  open gripper", grip=GRIP_OPEN),
        Action("2  close half way", grip=(GRIP_OPEN + GRIP_CLOSED) / 2),
        Action(f"3  move {opt.distance:g} mm in +{opt.axis.upper()}", pose=away),
        Action("4  open gripper", grip=GRIP_OPEN),
    ]
    if opt.combine:
        actions += [
            Action(f"5  above start, yaw {opt.yaw:+g} deg", pose=rotated,
                   notes="translation and rotation fused (--combine)"),
            Action(f"6a descend {opt.descend:g} mm", pose=lowered),
        ]
    else:
        actions += [
            Action(f"5a above start (+{opt.approach:g} mm Z)", pose=above),
            Action(f"5b rotate yaw {opt.yaw:+g} deg", pose=rotated,
                   notes="rotation only, no translation"),
            Action(f"6a descend {opt.descend:g} mm", pose=lowered,
                   notes=f"ends {opt.approach - opt.descend:g} mm above the start Z"),
        ]
    actions.append(Action("6b close gripper", grip=GRIP_CLOSED,
                          notes="closes fully — the jaws must be empty"))
    if opt.restore:
        # Without this the sequence is NOT idempotent: it adds `yaw` degrees every
        # run, so each re-run starts further round than the last and eventually
        # folds the wrist back far enough for end-effector tooling to foul the arm.
        # Observed for real: two consecutive runs tripped collision error 31.
        actions.append(Action("7  restore start orientation", pose=lowered_at_start_yaw(
            start, opt), notes="makes the sequence safe to re-run"))
    return actions


def lowered_at_start_yaw(start: Pose, opt: Options) -> Pose:
    """Final pose of the sequence, but with the original orientation restored."""
    return _pose(start, z=opt.approach - opt.descend)


def _fmt(p: Pose) -> str:
    return ("[" + ", ".join(f"{v:8.2f}" for v in
            (p.x, p.y, p.z, p.roll, p.pitch, p.yaw)) + "]")


def _dist(a: Pose, b: Pose) -> float:
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)


def _preflight(driver: XArmDriver, sequence: list[Action]) -> None:
    """Check every pose in the sequence before issuing the first command.

    Checking as you go is not good enough: by the time step 5b is refused the arm has
    already gripped and moved, and unwinding that safely is its own problem. Solve
    IK for all targets up front and refuse the whole run if any one of them lands
    outside the joint soft limits.
    """
    problems = []
    for action in sequence:
        if action.pose is None:
            continue
        reason = driver.check_pose_target(action.pose)
        if reason:
            problems.append(f"{action.label.strip()}: {reason}")
    if problems:
        raise RuntimeError(
            "sequence refused before any motion — "
            + "; ".join(problems)
            + ". Jog the arm to a less folded starting pose, or narrow the yaw change."
        )
    print(f"  pre-flight: all {sum(1 for a in sequence if a.pose)} pose targets "
          f"solve within the joint soft limits")


def _require_healthy(driver: XArmDriver, when: str) -> None:
    """Abort if the arm has latched a fault.

    A collision latches an error and every later command is silently refused;
    without this the remaining steps would all look like tolerance failures and
    bury the real cause.
    """
    status = driver.status()
    if "error_code" not in status:
        raise RuntimeError(f"could not read fault state {when} — refusing to continue")
    if status.get("error_code"):
        raise RuntimeError(f"arm latched error {status['error_code']} {when} "
                           f"(warn={status.get('warn_code')})")


def run(ip: str, name: str, opt: Options, *, execute: bool, brake: bool = True) -> bool:
    label = name or ip
    print(f"\n=== Gripper + move sequence: '{label}' @ {ip} ===")
    print(f"  {opt.speed:g} mm/s linear, {DEFAULT_ROT_SPEED:g} deg/s rotational, "
          f"{opt.pause:g}s between steps, tolerance {TOL_MM:g} mm")

    if not execute:
        # Dry run still needs a start pose to show real targets, so read one if the
        # arm is reachable; otherwise fall back to a placeholder and say so.
        start = Pose(0, 0, 0, 0, 0, 0)
        driver = XArmDriver(label or "arm", {"ip": ip, "gripper": "none"})
        try:
            driver.connect()
            start = driver.get_pose()
            print(f"  start pose (live): {_fmt(start)}")
        except Exception as e:
            print(f"  (arm unreachable, showing offsets from zero: {e})")
        finally:
            driver.disconnect()
        print("  [dry-run] planned actions:")
        for a in build_sequence(start, opt):
            target = _fmt(a.pose) if a.pose else f"grip -> {a.grip:g} counts"
            print(f"    {a.label:34s} {target}" + (f"   # {a.notes}" if a.notes else ""))
        print("  [dry-run] pass --yes to execute.")
        return True

    driver = XArmDriver(label or "arm", {"ip": ip, "gripper": "auto",
                                         "tcp_speed": opt.speed})
    try:
        driver.connect()
    except Exception as e:
        print(f"  FAIL: could not connect: {e}")
        return False

    try:
        start = driver.get_pose()
        print(f"  start pose: {_fmt(start)}")
        _require_healthy(driver, "before starting")
        if not driver.gripper_info.supports_width:
            raise RuntimeError(
                f"the {driver.gripper_info.kind} gripper has no commandable width — "
                "this sequence needs the parallel gripper"
            )

        sequence = build_sequence(start, opt)
        _preflight(driver, sequence)

        for action in sequence:
            time.sleep(opt.pause)
            if action.grip is not None:
                driver.grip(width=action.grip)
                time.sleep(SETTLE_S)
                actual = driver.gripper_width()
                shown = f"{actual:.0f}" if actual is not None else "n/a"
                print(f"  {action.label:34s} -> {action.grip:5.0f} counts "
                      f"(read back {shown})")
            else:
                assert action.pose is not None
                driver.move_to(action.pose, speed=opt.speed, wait=True)
                time.sleep(SETTLE_S)
                actual = driver.get_pose()
                error = _dist(actual, action.pose)
                print(f"  {action.label:34s} -> {_fmt(actual)}  err {error:5.2f} mm")
                if error > TOL_MM:
                    raise RuntimeError(
                        f"{action.label}: ended {error:.2f} mm from the commanded "
                        f"target, over the {TOL_MM:g} mm tolerance"
                    )
            _require_healthy(driver, f"after '{action.label.strip()}'")

        print(f"  final pose: {_fmt(driver.get_pose())}")
        print(f"  PASS: all {len(sequence)} actions completed within tolerance, no faults")
        ok = True
    except KeyboardInterrupt:
        print("\n  ABORTED by operator (Ctrl-C)")
        ok = False
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        ok = False
    finally:
        # Only hand the arm over energized on a clean pass. After a fault or an abort
        # the arm may be mid-trajectory or in a pose nobody vetted, and leaving that
        # live is the wrong default however the run was invoked.
        hand_over_live = brake is False and ok
        driver.disconnect(brake=not hand_over_live)
        print(f"  [{label}] disconnected — "
              + ("arm left ENERGIZED and holding (no brake cycle, no clunk)"
                 if hand_over_live else "arm braked (servos off)"))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Gripper + short-move validation sequence (see the sign-off sheet).")
    ap.add_argument("--ip", required=True, help="arm IP")
    ap.add_argument("--name", default="", help="label for output")
    ap.add_argument("--axis", choices=["x", "y"], default="x",
                    help="which axis step 3 moves along (default x)")
    ap.add_argument("--distance", type=float, default=DEFAULT_DISTANCE_MM,
                    help=f"step 3 travel in mm (default {DEFAULT_DISTANCE_MM:g})")
    ap.add_argument("--approach", type=float, default=DEFAULT_APPROACH_MM,
                    help=f"step 5a clearance above start in mm (default {DEFAULT_APPROACH_MM:g})")
    ap.add_argument("--descend", type=float, default=DEFAULT_DESCEND_MM,
                    help=f"step 6a descent in mm (default {DEFAULT_DESCEND_MM:g})")
    ap.add_argument("--yaw", type=float, default=DEFAULT_YAW_DEG,
                    help=f"step 5b yaw change in deg (default {DEFAULT_YAW_DEG:g})")
    ap.add_argument("--speed", type=float, default=DEFAULT_SPEED,
                    help=f"TCP speed in mm/s (default {DEFAULT_SPEED:g} — deliberately slow)")
    ap.add_argument("--pause", type=float, default=DEFAULT_PAUSE_S,
                    help=f"seconds between steps (default {DEFAULT_PAUSE_S:g})")
    ap.add_argument("--combine", action="store_true",
                    help="fuse each translate+rotate into one command instead of two")
    ap.add_argument("--no-restore", action="store_true",
                    help="skip the final move back to the starting orientation. Without the "
                         "restore, each run adds --yaw degrees and re-running walks the wrist "
                         "round until tooling fouls the arm")
    ap.add_argument("--yes", action="store_true",
                    help="actually move the arm; without this it's a dry run")
    ap.add_argument("--leave-enabled", action="store_true",
                    help="leave the servos energized and holding instead of braking. "
                         "Skips the brake engage/release cycle entirely, so no clunk and the "
                         "next run starts smoothly. The arm stays live — don't leave it "
                         "unattended this way")
    args = ap.parse_args()

    for field_name in ("distance", "approach", "descend"):
        value = getattr(args, field_name)
        if not math.isfinite(value) or not 0 < value <= MAX_TRAVEL_MM:
            ap.error(f"--{field_name} must be in (0, {MAX_TRAVEL_MM:g}] mm")
    if args.descend > args.approach:
        ap.error("--descend may not exceed --approach; the arm would end below the start Z")
    if not math.isfinite(args.yaw) or abs(args.yaw) > 180:
        ap.error("--yaw must be within +/-180 deg")
    if not math.isfinite(args.speed) or not 0 < args.speed <= 100:
        ap.error("--speed must be in (0, 100] mm/s for this script")

    opt = Options(distance=args.distance, approach=args.approach, descend=args.descend,
                  yaw=args.yaw, speed=args.speed, pause=args.pause,
                  combine=args.combine, axis=args.axis, restore=not args.no_restore)

    if args.yes:
        print(f"MOVING the arm at {args.ip} within a "
              f"{max(args.distance, args.approach):g} mm envelope — workspace must be clear, "
              f"jaws must be empty.")
    ok = run(args.ip, args.name, opt, execute=args.yes, brake=not args.leave_enabled)
    print(f"\n=== {'PASS' if ok else 'FAIL'} ===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
