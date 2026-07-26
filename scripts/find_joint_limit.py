#!/usr/bin/env python3
"""Find a safe soft limit for one joint, by stepping it under human supervision.

Written for the J5 (wrist bend) clearance problem: with a camera on the flange, the
wrist can fold far enough that the tool fouls the arm. The controller cannot help
here — set_self_collision_detection() models the arm's own links ONLY and knows
nothing about anything bolted to the flange — so the limit has to be measured.

How it works: moves ONE joint, a few degrees at a time, and stops after every step
to ask whether the clearance is still acceptable. You answer while looking at the
arm. When you say stop, it backs off to the last angle you approved, applies a
safety margin, and prints the config snippet to paste.

    [Enter] / y   clearance is fine, take another step
    n / q         too close — back off to the last approved angle and stop
    b             same as n

This is deliberately interactive: it refuses to run unless stdin is a terminal, so
it can never step a joint toward a collision with nobody watching.

Safety
------
* Moves exactly one joint. Every other joint is commanded to its present value.
* Small steps (default 3 deg, max 15) at a slow joint speed (default 5 deg/s).
* Never exceeds the model's own mechanical range for that joint.
* A collision trip is caught, cleared, and backed off automatically.
* Requires --yes. Without it you get a dry run of the plan.
* Brakes the arm on every exit path.

Before running, jog the arm to the pose where clearance is WORST — the tool-to-arm
gap depends on J3 and J5 together, so a limit measured from an open pose may not
hold in a folded one. Measure the worst case and the limit is conservative
everywhere.

Usage:
    python scripts/find_joint_limit.py --ip 192.168.3.13                    # dry run
    python scripts/find_joint_limit.py --ip 192.168.3.13 --yes              # J5 both ways
    python scripts/find_joint_limit.py --ip 192.168.3.13 --yes --joint 5 --direction up
"""
from __future__ import annotations

import argparse
import math
import sys
import time

sys.path.insert(0, ".")

from drivers.xarm.driver import XArmDriver  # noqa: E402

DEFAULT_JOINT = 5               # J5 — the wrist bend, one before the tool spin axis
DEFAULT_STEP_DEG = 3.0
MAX_STEP_DEG = 15.0
DEFAULT_SPEED = 5.0             # deg/s
DEFAULT_MARGIN_DEG = 5.0        # backed off from the last approved angle
SETTLE_S = 0.4


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return "q"


def _joint_str(angles: list[float]) -> str:
    return "[" + ", ".join(f"{a:7.1f}" for a in angles) + "]"


def _resolve_start(spec: str, current: float, lo: float, hi: float) -> float | None:
    """Where to begin the sweep.

    Defaults to the middle of the mechanical range rather than wherever the arm is
    parked: if the joint already sits past the safe limit, the first step gets
    rejected and the sweep measures nothing. Starting from the middle — where the
    wrist is unfolded and clearance is largest — means both sweeps approach their
    limit from the safe side.
    """
    spec = spec.strip().lower()
    if spec == "mid":
        return (lo + hi) / 2
    if spec == "current":
        return current
    try:
        value = float(spec)
    except ValueError:
        return None
    if not math.isfinite(value) or not lo <= value <= hi:
        return None
    return value


def sweep(driver: XArmDriver, index: int, direction: int, *,
          step: float, speed: float, model_lo: float, model_hi: float) -> float | None:
    """Step one joint until the operator says stop.

    Returns the last angle the operator *explicitly approved*, or None if they
    rejected the very first step. None is not the same as "the start angle is fine":
    it means the joint was already at or past the safe limit when we began, and the
    real bound is somewhere beyond it in the retreating direction. Reporting the
    start angle in that case would hand back a limit nobody signed off on.
    """
    label = f"J{index + 1}"
    arrow = "increasing" if direction > 0 else "decreasing"
    bound = model_hi if direction > 0 else model_lo
    print(f"\n--- sweeping {label} {arrow} (mechanical bound {bound:+.1f} deg) ---")
    print("    [Enter]=another step   n=too close, stop   s=smaller steps   l=larger steps")

    start = driver.get_joints()[index]
    approved: float | None = None

    while True:
        angles = driver.get_joints()
        target = angles[index] + direction * step
        if (direction > 0 and target > model_hi) or (direction < 0 and target < model_lo):
            print(f"    reached the model's mechanical bound {bound:+.1f} deg — stopping")
            return approved

        angles[index] = target
        try:
            driver.move_joints(angles, speed=speed, wait=True)
            time.sleep(SETTLE_S)
        except Exception as e:
            print(f"    MOVE REFUSED at {target:+.1f} deg: {e}")
            return approved

        status = driver.status()
        if status.get("error_code"):
            # Almost certainly the tool contacting the arm. Clear it and retreat.
            print(f"    !! controller error {status['error_code']} at {target:+.1f} deg "
                  f"— treating as contact, backing off")
            driver.clear_errors()
            if approved is not None:
                _retreat(driver, index, approved, speed)
            return approved

        actual = driver.get_joints()[index]
        pose = driver.get_pose()
        print(f"    {label} = {actual:+7.1f} deg   TCP z={pose.z:7.1f}  "
              f"(moved {actual - start:+.1f} from start, step {step:g} deg)")

        answer = _ask("    clearance ok? [Enter]=yes  n=too close  s/l=step size: ")
        if answer in ("s", "smaller"):
            step = max(0.5, step / 2)
            print(f"    step size now {step:g} deg")
            continue
        if answer in ("l", "larger"):
            step = min(MAX_STEP_DEG, step * 2)
            print(f"    step size now {step:g} deg")
            continue
        if answer in ("n", "q", "b", "no", "stop"):
            if approved is None:
                print(f"    stopped on the FIRST step — {label} was already too close at "
                      f"the starting angle {start:+.1f} deg. Nothing measured this way; "
                      f"re-run with --start-at to begin from a safer angle.")
                _retreat(driver, index, start, speed)
                return None
            print(f"    stopping — last approved angle {approved:+.1f} deg")
            _retreat(driver, index, approved, speed)
            return approved
        approved = actual


def free_move_session(driver: XArmDriver) -> bool:
    """Hand-guide the arm into a starting pose before the sweep.

    Returns True if the operator actually used it, so the caller knows to sweep from
    where they left the arm rather than moving it somewhere else first.

    The arm stays gravity-compensated, but that compensation is only as good as
    ``tcp_load`` — with the payload understated the arm sinks when released. Support
    it before enabling, and expect some drift.
    """
    print("\n--- free move ---")
    print("  Enabling this makes the arm back-drivable: push it by hand into the pose")
    print("  you want to sweep from. It holds against gravity, but SUPPORT IT before")
    print("  you enable — the configured payload may not match the real end effector.")
    if _ask("  enable free move? [Enter]=yes  n=skip: ") in ("n", "q", "no", "skip"):
        print("  skipped.")
        return False

    driver.set_free_drive(True)
    try:
        print("\n  >>> FREE MOVE ACTIVE — move the arm by hand now. <<<")
        _ask("  press [Enter] when it is where you want it: ")
    finally:
        # Must come back to position control on every path: programmed moves in a
        # teaching mode do not behave normally.
        driver.set_free_drive(False)
        print("  free move OFF — back in position control")

    print(f"  joints now: {_joint_str(driver.get_joints())}")
    return True


def _retreat(driver: XArmDriver, index: int, angle: float, speed: float) -> None:
    """Return the joint to a known-good angle."""
    try:
        angles = driver.get_joints()
        angles[index] = angle
        driver.move_joints(angles, speed=speed, wait=True)
        print(f"    backed off to {angle:+.1f} deg")
    except Exception as e:
        print(f"    WARNING: could not back off to {angle:+.1f} deg: {e}")


def _report(joint: int, lo: float | None, hi: float | None, margin: float,
            model: tuple[float, float]) -> None:
    """Print what was actually measured — and refuse to invent what wasn't.

    An unmeasured bound falls back to the model's mechanical range, which is wider
    than anything we verified. Emitting a config snippet with a fabricated bound in
    it is worse than emitting nothing, so an incomplete result is reported as
    incomplete and the snippet is withheld.
    """
    print("\n=== Result ===")
    print(f"  model range      J{joint} = [{model[0]:+.1f}, {model[1]:+.1f}]")
    if lo is None and hi is None:
        print("  NOTHING MEASURED — both directions were rejected on the first step.")
        print("  Re-run with --start-at mid to begin from the middle of the range.")
        return

    if lo is not None:
        safe_lo = round(lo + margin, 1)
        print(f"  lowest approved  J{joint} = {lo:+7.1f} deg  -> limit {safe_lo:+.1f} "
              f"(with {margin:g} deg margin)")
    else:
        safe_lo = None
        print(f"  lowest  bound: NOT MEASURED — rejected on the first step, so the safe "
              f"limit is beyond the angle we started from, not at it.")
    if hi is not None:
        safe_hi = round(hi - margin, 1)
        print(f"  highest approved J{joint} = {hi:+7.1f} deg  -> limit {safe_hi:+.1f} "
              f"(with {margin:g} deg margin)")
    else:
        safe_hi = None
        print(f"  highest bound: NOT MEASURED — rejected on the first step, so the safe "
              f"limit is beyond the angle we started from, not at it.")

    if safe_lo is None or safe_hi is None:
        missing = "lower" if safe_lo is None else "upper"
        other = "down" if safe_lo is None else "up"
        print(f"\n  No config snippet: the {missing} bound is unknown, and filling it in "
              f"from the\n  model range would permit poses nobody checked. Re-run the "
              f"{other} sweep from\n  a safer angle:\n")
        print(f"      --joint {joint} --direction {other} --start-at mid")
        return

    print("\n  Paste into the arm's fleet entry in core/config.py:\n")
    print(f'      "joint_limit_overrides": {{"{joint}": [{safe_lo}, {safe_hi}]}},')
    print("\n  This is enforced on joint moves AND on cartesian moves (the driver solves")
    print("  IK and checks the result), so move_to can no longer step around it.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Measure a safe soft limit for one joint, with a human watching.")
    ap.add_argument("--ip", required=True, help="arm IP")
    ap.add_argument("--joint", type=int, default=DEFAULT_JOINT,
                    help=f"joint number, 1-based (default {DEFAULT_JOINT} = wrist bend)")
    ap.add_argument("--direction", choices=["up", "down", "both"], default="both",
                    help="which way to sweep (default both, returning to start between)")
    ap.add_argument("--step", type=float, default=DEFAULT_STEP_DEG,
                    help=f"degrees per step (default {DEFAULT_STEP_DEG:g}, max {MAX_STEP_DEG:g})")
    ap.add_argument("--speed", type=float, default=DEFAULT_SPEED,
                    help=f"joint speed deg/s (default {DEFAULT_SPEED:g})")
    ap.add_argument("--margin", type=float, default=DEFAULT_MARGIN_DEG,
                    help=f"safety margin subtracted from what you approved "
                         f"(default {DEFAULT_MARGIN_DEG:g} deg)")
    ap.add_argument("--start-at", default=None,
                    help="where to begin the sweep: 'mid' (middle of the mechanical "
                         "range), 'current' (wherever the joint is now), or an angle in "
                         "degrees. Defaults to 'current' if you used the free-move "
                         "session to place the arm, otherwise 'mid'")
    ap.add_argument("--no-free-move", action="store_true",
                    help="skip the hand-guiding session at the start")
    ap.add_argument("--known-low", type=float, default=None,
                    help="lowest APPROVED angle from an earlier run, so a one-direction "
                         "sweep can still emit a complete snippet (pass the approved "
                         "angle, not the margined limit)")
    ap.add_argument("--known-high", type=float, default=None,
                    help="highest APPROVED angle from an earlier run")
    ap.add_argument("--yes", action="store_true", help="actually move; else dry run")
    args = ap.parse_args()

    if not math.isfinite(args.step) or not 0 < args.step <= MAX_STEP_DEG:
        ap.error(f"--step must be in (0, {MAX_STEP_DEG:g}] deg")
    if not math.isfinite(args.speed) or not 0 < args.speed <= 30:
        ap.error("--speed must be in (0, 30] deg/s")
    if args.joint < 1:
        ap.error("--joint is 1-based")

    index = args.joint - 1
    print(f"=== J{args.joint} limit discovery @ {args.ip} ===")
    print(f"  {args.step:g} deg steps at {args.speed:g} deg/s, {args.margin:g} deg margin")

    if not args.yes:
        print("  [dry-run] would:")
        print(f"    1. read the current J{args.joint} angle")
        for d in (["up", "down"] if args.direction == "both" else [args.direction]):
            print(f"    2. step J{args.joint} {d} by {args.step:g} deg, asking after each step")
        print("    3. back off to the last angle you approved")
        print(f"    4. print a limit {args.margin:g} deg inside that, as a config snippet")
        print("  [dry-run] pass --yes to run it for real.")
        return 0

    if not sys.stdin.isatty():
        print("\n  REFUSING: --yes needs an interactive terminal, because every step is\n"
              "  gated on you looking at the arm and answering. Run it from your shell.")
        return 2

    driver = XArmDriver("limit-probe", {"ip": args.ip, "gripper": "none"})
    try:
        driver.connect()
    except Exception as e:
        print(f"  FAIL: could not connect: {e}")
        return 1

    lo_found, hi_found = args.known_low, args.known_high
    if lo_found is not None or hi_found is not None:
        print(f"  carrying forward measured bounds: low={lo_found} high={hi_found}")
    try:
        model = driver.model_joint_limits
        if not model or index >= len(model):
            print(f"  FAIL: no model limits for J{args.joint} on this arm")
            return 1
        model_lo, model_hi = model[index]
        start_angles = driver.get_joints()
        print(f"  joints now: {_joint_str(start_angles)}")
        print(f"  J{args.joint} is at {start_angles[index]:+.1f} deg, mechanical range "
              f"[{model_lo:+.1f}, {model_hi:+.1f}]")

        positioned = False
        if not args.no_free_move:
            positioned = free_move_session(driver)

        # If they hand-placed the arm, sweep from there — moving it back to mid would
        # throw away the pose they just chose. Explicit --start-at always wins.
        start_at = args.start_at or ("current" if positioned else "mid")
        start_angles = driver.get_joints()
        begin = _resolve_start(start_at, start_angles[index], model_lo, model_hi)
        if begin is None:
            ap.error("--start-at must be 'mid', 'current', or an angle in degrees")
        if abs(begin - start_angles[index]) > 0.5:
            print(f"\n  Will first move J{args.joint} from {start_angles[index]:+.1f} to "
                  f"{begin:+.1f} deg (the sweep's starting point).")
            print("  Check that path is clear — it is a single joint move, but a large one.")
            if _ask("  move there? [Enter]=yes  n=abort: ") in ("n", "q", "no"):
                print("  aborted before moving.")
                return 1
            _retreat(driver, index, begin, args.speed)

        start = driver.get_joints()[index]
        print(f"  sweeping from J{args.joint} = {start:+.1f} deg")
        print("  Watch the arm. Answer only when you can see the clearance.")

        if _ask("  ready? [Enter]=start  n=abort: ") in ("n", "q", "no"):
            print("  aborted before moving.")
            return 1

        directions = [1, -1] if args.direction == "both" else \
                     [1] if args.direction == "up" else [-1]
        for d in directions:
            approved = sweep(driver, index, d, step=args.step, speed=args.speed,
                             model_lo=model_lo, model_hi=model_hi)
            # A sweep that measured nothing must not overwrite a bound carried in
            # from --known-low/--known-high.
            if approved is not None:
                if d > 0:
                    hi_found = approved
                else:
                    lo_found = approved
            if len(directions) > 1:
                print(f"  returning J{args.joint} to the start angle {start:+.1f} deg")
                _retreat(driver, index, start, args.speed)

        _report(args.joint, lo_found, hi_found, args.margin, (model_lo, model_hi))
        return 0
    except KeyboardInterrupt:
        print("\n  ABORTED by operator (Ctrl-C) — braking")
        return 1
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        return 1
    finally:
        driver.disconnect()      # brakes the arm
        print("  disconnected (arm braked)")


if __name__ == "__main__":
    raise SystemExit(main())
