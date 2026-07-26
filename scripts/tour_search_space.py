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
    """A closed rectangle: +X, +Y, -X, -Y. Sums to zero on both axes.

    One axis at a time, so every corner is a 90 degree direction change and the
    planner must decelerate to near zero at each one. Visibly jerky. Kept for
    comparison; prefer smooth_legs.
    """
    return [("X", dx), ("Y", dy), ("X", -dx), ("Y", -dy)]


def sweep_legs(dx: float, dy: float, dz: float = 0.0,
               da: float = 0.0) -> list[dict[str, float]]:
    """Full-travel sweeps: six LONG legs, closed, almost no vertices.

    The polygon shapes trade leg length for corner angle — 16 sides means 16
    direction changes per lap, and even with a perfectly continuous command queue
    the planner still decelerates at each one, which reads as pulsing. This does
    the opposite: each leg runs the entire requested travel, so there are 6
    slowdowns per lap instead of 16, and the gantry actually crosses the full
    axis rather than an inscribed ellipse.

    dx/dy here are FULL travel, not half-widths. Legs: X out/back, Y out/back,
    then a diagonal out/back. Sums to zero on both axes.
    """
    def leg(**axes: float) -> dict[str, float]:
        return {a: v for a, v in axes.items() if v}

    return [
        # Each pair is out-and-back, so every axis closes on zero. Z and A ride
        # along with the gantry legs so all four move in one coordinated G0
        # rather than taking turns.
        leg(X=dx, Z=dz),
        leg(X=-dx, Z=-dz),
        leg(Y=dy, A=da),
        leg(Y=-dy, A=-da),
        leg(X=dx, Y=dy, Z=dz, A=da),
        leg(X=-dx, Y=-dy, Z=-dz, A=-da),
    ]


def helix_legs(dx: float, dy: float, dz: float,
               sides: int = 12) -> list[dict[str, float]]:
    """A closed 3D loop: the XY polygon with Z dipping down and back over a lap.

    All three axes move in every leg, in one coordinated G0 each, so the gantry
    sweeps a smooth 3D path rather than stepping axis by axis.

    Z is a full raised-dip-raised cycle per lap — `1 - cos` rather than `sin` — so
    it both starts and ends at the datum and the loop closes on every axis. On
    this machine **+Z is DOWN**, so a positive dz descends; it is the caller's job
    to know there is that much clearance, because a crash is invisible to
    software.
    """
    import math

    a, b = dx / 2.0, dy / 2.0
    pts = []
    for k in range(sides + 1):          # +1 so the last point closes onto the first
        t = 2 * math.pi * k / sides
        pts.append((
            a * math.cos(t),
            b * math.sin(t),
            dz * (1.0 - math.cos(t)) / 2.0,
        ))
    out: list[dict[str, float]] = []
    for k in range(sides):
        x0, y0, z0 = pts[k]
        x1, y1, z1 = pts[k + 1]
        leg = {"X": x1 - x0, "Y": y1 - y0}
        if abs(z1 - z0) > 1e-9:
            leg["Z"] = z1 - z0
        out.append(leg)
    return out


def smooth_legs(dx: float, dy: float, sides: int = 12) -> list[dict[str, float]]:
    """A closed polygon inscribed in the dx-by-dy box, as coordinated moves.

    Two things make this smooth where the rectangle is not. Each leg names X and
    Y in one G0, so it executes as a single coordinated diagonal rather than two
    separate axis moves. And with `sides` legs the direction change at each
    vertex is roughly 360/sides degrees instead of 90, so the planner barely has
    to slow down.

    Returns relative deltas that sum to zero on both axes, so the loop closes.
    """
    import math

    a, b = dx / 2.0, dy / 2.0
    pts = [
        (a * math.cos(2 * math.pi * k / sides), b * math.sin(2 * math.pi * k / sides))
        for k in range(sides)
    ]
    out: list[dict[str, float]] = []
    for k in range(sides):
        x0, y0 = pts[k]
        x1, y1 = pts[(k + 1) % sides]
        out.append({"X": x1 - x0, "Y": y1 - y0})
    return out


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
    ap.add_argument("--shape", choices=["rect", "smooth", "sweep"], default="sweep",
                    help="sweep = few long full-travel legs (smoothest, default); "
                         "smooth = inscribed polygon; rect = 90-degree corners")
    ap.add_argument("--sides", type=int, default=12,
                    help="legs per lap for --shape smooth; more = gentler corners")
    ap.add_argument("--z", type=float, default=0.0,
                    help="Z dip per lap in mm (+Z is DOWN). 0 = XY only. "
                         "Only pass this if you know the clearance.")
    ap.add_argument("--junction", type=float, default=None,
                    help="junction deviation in mm via M205 (board default ~0.05 is "
                         "conservative and slows every vertex). Try 0.2-0.4.")
    ap.add_argument("--accel", type=float, default=None,
                    help="acceleration in mm/s^2 via M204 (board config is 250). "
                         "Raise cautiously: no closed loop, so too high skips steps.")
    ap.add_argument("--a", type=float, default=0.0,
                    help="A-axis (right mount) travel per sweep leg, mm. 0 = off.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    step_cap = min(args.step, MAX_JOG_MM)
    total_mm = 0.0
    per_mm_s = 60.0 / args.feed

    print(f"tour: {args.x:.0f} x {args.y:.0f} mm box, {args.laps} lap(s)")
    print(f"  {args.feed/60:.1f} mm/s")
    print("  the loop is CLOSED: it returns to the exact starting point.\n")

    if args.dry_run:
        preview = (sweep_legs(args.x, args.y, args.z, args.a) if args.shape == 'sweep'
                   else legs(args.x, args.y) if args.shape == 'rect'
                   else helix_legs(args.x, args.y, args.z, args.sides) if args.z
                   else smooth_legs(args.x, args.y, args.sides))
        for e in preview:
            print('  ' + (', '.join(f'{a}{v:+.2f}' for a, v in e.items())
                          if isinstance(e, dict) else f'{e[0]} {e[1]:+.2f} mm'))
        print("\ndry run, nothing moved.")
        return 0

    d = OpentronsDriver("ot-one-tour", {"port": args.port})
    print(f"connecting to {args.port} ...")
    d.connect()
    print(f"  connected: {d.info.name}\n")
    # Corner blending: junction_deviation is absent from this board's config, so
    # it runs on Smoothieware's conservative built-in default and decelerates at
    # every vertex. Raising it is what removes the residual vertex-to-vertex
    # hitching once the command queue is already continuous.
    if args.junction is not None:
        print(f"  M205 X{args.junction} (junction deviation)")
        d._send(f"M205 X{args.junction:.3f}")
    if args.accel is not None:
        print(f"  M204 S{args.accel:.0f} (acceleration)")
        d._send(f"M204 S{args.accel:.0f}")
    print("*** MOTION. WATCH IT. Ctrl-C cuts motion. ***\n")

    travelled = {"X": 0.0, "Y": 0.0, "Z": 0.0, "A": 0.0}
    slow = 0
    try:
        # One jog_path per lap: all four legs are queued back-to-back and the
        # planner is drained once, so the gantry sweeps the rectangle in
        # continuous motion. Driving this with one jog() per chunk instead
        # drains the queue after every chunk, which stops the machine dead
        # between steps and makes the path visibly jitter.
        # EVERY lap is queued in ONE call. Splitting per lap drains the planner
        # between laps, which is a full stop the operator sees as a hitch.
        if args.shape == "sweep":
            one_lap: list = sweep_legs(args.x, args.y, args.z, args.a)
        elif args.shape == "rect":
            one_lap = legs(args.x, args.y)
        elif args.z:
            one_lap = helix_legs(args.x, args.y, args.z, args.sides)
        else:
            one_lap = smooth_legs(args.x, args.y, args.sides)
        path = [leg for _ in range(args.laps) for leg in one_lap]

        def leg_mm(e) -> float:
            if isinstance(e, dict):
                return sum(v * v for v in e.values()) ** 0.5
            return abs(e[1])

        path_mm = sum(leg_mm(e) for e in path)
        expected = path_mm * per_mm_s
        print(f"  shape={args.shape}"
              f"{f' sides={args.sides}' if args.shape != 'rect' else ''}, "
              f"{len(path)} legs queued as ONE continuous path")
        print(f"  {path_mm:.0f} mm, expect ~{expected:.1f}s\n")

        t0 = time.time()
        d.jog_path(path, feedrate=args.feed, max_total_mm=path_mm + 50)
        dt = time.time() - t0
        for e in path:
            if isinstance(e, dict):
                for a, v in e.items():
                    travelled[a] = travelled.get(a, 0.0) + v
            else:
                travelled[e[0]] += e[1]
        if dt > expected * 1.6:
            slow += 1
        print(f"  done in {dt:.2f}s (exp {expected:.2f}s)  "
              f"net X{travelled['X']:+.2f} Y{travelled['Y']:+.2f} Z{travelled['Z']:+.2f}")
        overhead = dt - expected
        print(f"  overhead {overhead:+.2f}s over {len(path)} legs "
              f"= {overhead/len(path)*1000:+.0f} ms/leg  "
              f"({'continuous' if abs(overhead)/len(path) < 0.15 else 'still hitching'})")
        total_mm = path_mm

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
