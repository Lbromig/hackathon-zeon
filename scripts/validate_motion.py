#!/usr/bin/env python3
"""Validate that an xArm actually moves, accurately, through a small closed path.

Drives the arm a few millimetres through a closed loop and checks the measured pose
after every leg against what was commanded. Because the path is closed (a square in
XY, then a Z hop), the arm must finish where it started — which catches lost steps,
drift, and a controller that accepts commands without executing them.

This exercises the real stack (XArmDriver -> capability -> SDK), not raw SDK calls,
so it doubles as an integration check on the driver.

Safety
------
* Every leg is a *relative* move of a few mm at a slow speed — no absolute jumps.
* The step size is capped (--step, max 25 mm) and the arm's own soft limits apply.
* The error code is checked before and after every leg; the run aborts on any fault.
* Motion needs an explicit --yes. Without it you get a dry run.
* The driver brakes the arm on disconnect, on every exit path.

The workspace must be clear: the arm will move up to `step` mm in each direction
from wherever it currently is.

Usage:
    python scripts/validate_motion.py --ip 192.168.3.13                 # dry run
    python scripts/validate_motion.py --ip 192.168.3.13 --yes           # really move
    python scripts/validate_motion.py --ip 192.168.3.13 --yes --step 5 --tol 0.3
    python scripts/validate_motion.py --fleet --yes                     # every xArm
"""
from __future__ import annotations

import argparse
import math
import sys
import time

sys.path.insert(0, ".")

from drivers.xarm.driver import XArmDriver  # noqa: E402

DEFAULT_STEP_MM = 10.0
MAX_STEP_MM = 25.0          # a "validate it moves" script has no business going further
DEFAULT_TOL_MM = 0.5        # per-leg positioning tolerance
DEFAULT_SPEED = 30.0        # mm/s — slow enough to watch and to stop
SETTLE_S = 0.4              # let the report catch up before reading the pose back


def _legs(step: float) -> list[tuple[str, tuple[float, float, float]]]:
    """A closed loop: XY square, then up and down. Sums to zero on every axis."""
    return [
        ("+X", (step, 0.0, 0.0)),
        ("+Y", (0.0, step, 0.0)),
        ("-X", (-step, 0.0, 0.0)),
        ("-Y", (0.0, -step, 0.0)),
        ("+Z", (0.0, 0.0, step)),
        ("-Z", (0.0, 0.0, -step)),
    ]


def _xyz(pose) -> tuple[float, float, float]:
    return (pose.x, pose.y, pose.z)


def _dist(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt(sum((p - q) ** 2 for p, q in zip(a, b)))


def _fmt(xyz) -> str:
    return "[" + ", ".join(f"{v:8.2f}" for v in xyz) + "]"


def validate(ip: str, name: str = "", *, step: float, tol: float, speed: float,
             gripper: str, dry_run: bool, brake: bool = True) -> bool:
    label = name or ip
    legs = _legs(step)
    print(f"\n=== Motion validation: '{label}' @ {ip} ===")
    print(f"  {len(legs)} legs of {step:g} mm at {speed:g} mm/s, tolerance {tol:g} mm")
    if dry_run:
        print("  [dry-run] would move:")
        for leg, (dx, dy, dz) in legs:
            print(f"    {leg}: d=({dx:+g}, {dy:+g}, {dz:+g}) mm")
        print("  [dry-run] pass --yes to actually move the arm.")
        return True

    driver = XArmDriver(label or "arm", {"ip": ip, "name": label, "gripper": gripper,
                                         "tcp_speed": speed})
    try:
        driver.connect()
    except Exception as e:
        print(f"  FAIL: could not connect: {e}")
        return False

    failures: list[str] = []
    try:
        start = _xyz(driver.get_pose())
        print(f"  start pose (mm): {_fmt(start)}")
        _require_healthy(driver, "before moving")

        expected = list(start)
        for leg, (dx, dy, dz) in legs:
            for i, d in enumerate((dx, dy, dz)):
                expected[i] += d
            target = tuple(expected)

            driver.move_relative(dx=dx, dy=dy, dz=dz, speed=speed, wait=True)
            time.sleep(SETTLE_S)
            actual = _xyz(driver.get_pose())
            error = _dist(actual, target)
            ok = error <= tol
            print(f"  {leg}  -> want {_fmt(target)}  got {_fmt(actual)}  "
                  f"err {error:5.2f} mm  {'ok' if ok else 'OUT OF TOLERANCE'}")
            if not ok:
                failures.append(f"{leg}: {error:.2f} mm > {tol:g} mm")
            _require_healthy(driver, f"after leg {leg}")

        # The loop is closed, so this is an independent check on the whole sequence:
        # per-leg errors can each pass while the arm quietly walks away from start.
        end = _xyz(driver.get_pose())
        closure = _dist(end, start)
        print(f"  return to start: {_fmt(start)} -> {_fmt(end)}  drift {closure:5.2f} mm")
        if closure > tol:
            failures.append(f"loop closure: {closure:.2f} mm > {tol:g} mm")

        if failures:
            print(f"  FAIL ({len(failures)}): " + "; ".join(failures))
            return False
        print("  PASS: all legs within tolerance and the arm returned to start")
        return True
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        return False
    finally:
        driver.disconnect(brake=brake)
        print(f"  [{label}] disconnected — "
              + ("arm braked (servos off)" if brake
                 else "arm left ENERGIZED and holding (no brake cycle, no clunk)"))


def _require_healthy(driver: XArmDriver, when: str) -> None:
    """Abort the run if the arm has latched a fault.

    A collision mid-validation latches an error and every later command is silently
    refused; without this check the remaining legs would all report as tolerance
    failures and bury the actual cause.
    """
    status = driver.status()
    code, warn = status.get("error_code"), status.get("warn_code")
    if code:
        raise RuntimeError(f"arm latched error {code} {when} (warn={warn})")


def _fleet_targets() -> list[tuple[str, str]]:
    try:
        from core.config import settings
    except Exception as e:
        sys.exit(f"--fleet: could not read the fleet config ({e})")
    return [(d["ip"], d.get("name", d["id"]))
            for d in settings.fleet if d.get("type") == "xarm" and d.get("ip")]


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate xArm motion with a small closed path.")
    ap.add_argument("--ip", action="append", default=[], help="arm IP (repeatable)")
    ap.add_argument("--fleet", action="store_true", help="validate every xArm in the config")
    ap.add_argument("--step", type=float, default=DEFAULT_STEP_MM,
                    help=f"leg length in mm (default {DEFAULT_STEP_MM:g}, max {MAX_STEP_MM:g})")
    ap.add_argument("--tol", type=float, default=DEFAULT_TOL_MM,
                    help=f"per-leg tolerance in mm (default {DEFAULT_TOL_MM:g})")
    ap.add_argument("--speed", type=float, default=DEFAULT_SPEED,
                    help=f"TCP speed in mm/s (default {DEFAULT_SPEED:g})")
    ap.add_argument("--gripper", default="none",
                    choices=["auto", "parallel", "lite6", "bio", "none"],
                    help="end-effector (default none — this script never actuates it)")
    ap.add_argument("--yes", action="store_true",
                    help="actually move the arm; without this it's a dry run")
    ap.add_argument("--leave-enabled", action="store_true",
                    help="leave the servos energized and holding instead of braking, so "
                         "back-to-back runs don't cycle the brakes. Don't leave it unattended")
    args = ap.parse_args()

    targets = _fleet_targets() if args.fleet else [(ip, "") for ip in args.ip]
    if not targets:
        ap.error("provide --ip <addr> (repeatable) or --fleet")
    if not math.isfinite(args.step) or not 0 < args.step <= MAX_STEP_MM:
        ap.error(f"--step must be in (0, {MAX_STEP_MM:g}] mm")
    if not math.isfinite(args.tol) or args.tol <= 0:
        ap.error("--tol must be > 0")

    if args.yes:
        print(f"MOVING {len(targets)} arm(s) up to {args.step:g} mm per axis — "
              f"the workspace must be clear.")
    results = {label or ip: validate(ip, label, step=args.step, tol=args.tol,
                                     speed=args.speed, gripper=args.gripper,
                                     dry_run=not args.yes,
                                     brake=not args.leave_enabled)
               for ip, label in targets}

    print("\n=== Summary ===")
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
