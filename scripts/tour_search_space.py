#!/usr/bin/env python3
"""Physically tour the OT-One's reachable space, so the camera can watch it move.

Drives a closed rectangular loop in X/Y at the current Z, returning to the exact
starting point. Every leg is relative (G91) because this machine has no working
endstops and therefore no datum — see docs/OT_ONE_HARDWARE.md. Because the loop
is closed and each leg is the same length out and back, the accumulated relative
error is the only drift, which is itself a useful thing for vision to measure.

Usage:
    python3 scripts/tour_search_space.py                  # 60 x 40 mm, 1 lap
    python3 scripts/tour_search_space.py --x 80 --y 60     # bigger box
    python3 scripts/tour_search_space.py --laps 3          # repeat
    python3 scripts/tour_search_space.py --dry-run         # print, move nothing

Nothing moves without the operator watching. A crash is invisible to software:
with no endstops and no current sensing a stalled stepper skips steps and the
timing looks identical to a clean move. Duration proves a leg ran, never that the
path was clear. Ctrl-C fires an emergency stop.
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, ".")

from drivers.opentrons.driver import (  # noqa: E402
    MAX_JOG_MM,
    DriverError,
    OpentronsDriver,
)

DEFAULT_PORT = "/dev/cu.usbmodem11201"


def legs(dx: float, dy: float) -> list[tuple[str, float]]:
    """A closed rectangle: +X, +Y, -X, -Y. Sums to zero on both axes."""
    return [("X", dx), ("Y", dy), ("X", -dx), ("Y", -dy)]


def chunk(delta: float, cap: float) -> list[float]:
    """Split one leg into steps no larger than the driver's per-jog cap."""
    out: list[float] = []
    remaining = abs(delta)
    sign = 1.0 if delta >= 0 else -1.0
    while remaining > 1e-9:
        s = min(cap, remaining)
        out.append(sign * s)
        remaining -= s
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=DEFAULT_PORT)
    ap.add_argument("--x", type=float, default=60.0, help="box width in mm")
    ap.add_argument("--y", type=float, default=40.0, help="box depth in mm")
    ap.add_argument("--laps", type=int, default=1)
    ap.add_argument("--feed", type=float, default=400.0, help="mm/min")
    ap.add_argument("--step", type=float, default=10.0,
                    help=f"max mm per jog (driver cap is {MAX_JOG_MM})")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    step_cap = min(args.step, MAX_JOG_MM)
    plan = [leg for _ in range(args.laps) for leg in legs(args.x, args.y)]
    total_mm = sum(abs(d) for _, d in plan)
    per_mm_s = 60.0 / args.feed

    print(f"tour: {args.x:.0f} x {args.y:.0f} mm box, {args.laps} lap(s)")
    print(f"  {len(plan)} legs, {total_mm:.0f} mm of travel, "
          f"{args.feed/60:.1f} mm/s, <= {step_cap:.0f} mm per jog")
    print(f"  estimated motion time {total_mm * per_mm_s:.0f}s")
    print("  the loop is CLOSED: it returns to the exact starting point.\n")

    if args.dry_run:
        for axis, d in plan:
            print(f"  {axis} {d:+.1f} mm  ({len(chunk(d, step_cap))} jog(s))")
        print("\ndry run, nothing moved.")
        return 0

    d = OpentronsDriver("ot-one-tour", {"port": args.port})
    print(f"connecting to {args.port} ...")
    d.connect()
    print(f"  connected: {d.info.name}\n")
    print("*** MOTION. WATCH IT. Ctrl-C cuts motion. ***\n")

    travelled = {"X": 0.0, "Y": 0.0}
    slow = 0
    try:
        # One jog_path per lap: all four legs are queued back-to-back and the
        # planner is drained once, so the gantry sweeps the rectangle in
        # continuous motion. Driving this with one jog() per chunk instead
        # drains the queue after every chunk, which stops the machine dead
        # between steps and makes the path visibly jitter.
        for lap in range(1, args.laps + 1):
            lap_legs = legs(args.x, args.y)
            lap_mm = sum(abs(dd) for _, dd in lap_legs)
            expected = lap_mm * per_mm_s
            print(f"  lap {lap}/{args.laps}: "
                  f"{' -> '.join(f'{a}{dd:+.0f}' for a, dd in lap_legs)}"
                  f"  ({lap_mm:.0f} mm, expect ~{expected:.1f}s continuous)")
            t0 = time.time()
            d.jog_path(lap_legs, feedrate=args.feed)
            dt = time.time() - t0
            for a, dd in lap_legs:
                travelled[a] += dd
            tag = ""
            if dt > expected * 1.6:
                slow += 1
                tag = "  <-- SLOW, possible contact"
            print(f"    done in {dt:.2f}s (exp {expected:.2f}s)  "
                  f"net X{travelled['X']:+.2f} Y{travelled['Y']:+.2f}{tag}")

        print(f"\ntour complete. {args.laps} lap(s), {total_mm:.0f} mm travelled.")
        print(f"  net displacement X{travelled['X']:+.2f} Y{travelled['Y']:+.2f} mm "
              f"(should be 0.00 / 0.00 — the loop is closed)")
        print(f"  slow legs: {slow}")
        if slow:
            print("  Some legs ran long. That can mean contact; check the deck.")
    except BaseException as e:
        print(f"\nABORTED: {type(e).__name__}: {e}")
        if d.estop():
            print("emergency stop written to the board.")
        else:
            print("emergency stop could NOT be written. CUT POWER AT THE SWITCH.")
        return 1
    finally:
        d.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
