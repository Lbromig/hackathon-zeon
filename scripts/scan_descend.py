#!/usr/bin/env python3
"""Scan X at the safe height, then descend, one cycle at a time.

The pattern the operator asked for, and the one that reads well on camera: every
cycle raises to the top datum first, sweeps X out and back while it is up there
and nothing is in the way, then lowers to a target depth. Successive cycles go
deeper, so the descent is the part that changes and the traverse is always at
full clearance.

Usage:
    python3 scripts/scan_descend.py                      # 100 mm sweep, 30/50/70 mm
    python3 scripts/scan_descend.py --span 140
    python3 scripts/scan_descend.py --depths 20,40,60,80
    python3 scripts/scan_descend.py --cycles 3           # repeat the depth list
    python3 scripts/scan_descend.py --dry-run            # print, move nothing

Bounds come from hardware/ot_one_envelope.json and docs/OT_ONE_HARDWARE.md, not
from feel. Every target is checked against them BEFORE anything moves, because a
crash is invisible to software: with no endstops and no current sensing a stalled
stepper skips steps and the timing looks identical to a clean move. Duration
proves a leg ran, never that the path was clear. The operator watching is the
only detector. Ctrl-C fires an emergency stop.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, ".")

from core.config import _opentrons_port  # noqa: E402
from drivers.opentrons.driver import DriverError, OpentronsDriver  # noqa: E402

# X homes to one end, so 0 is that end and +X is the only direction with travel.
# 330 is confirmed usable; 350 was rejected as a dead end. See
# hardware/ot_one_envelope.json.
X_MIN, X_MAX = 20.0, 330.0
# +Z is DOWN. 0 is the homed top. ~90 mm is the measured clear travel below it,
# so 80 is kept as the floor with margin.
Z_TOP, Z_FLOOR = 0.0, 80.0


def sweep_sign(x0: float, span: float) -> float:
    """Which way X can sweep `span` mm and come back, from x0.

    Prefers whichever side has room. Raises if neither does, rather than
    clamping: a silently shortened sweep looks the same on camera as the
    requested one, and the whole point here is that the machine cannot tell us.
    """
    if x0 - span >= X_MIN:
        return -1.0
    if x0 + span <= X_MAX:
        return +1.0
    raise DriverError(
        f"a {span:.0f} mm sweep does not fit from X={x0:.1f}: "
        f"usable X is {X_MIN:.0f} to {X_MAX:.0f} mm"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None,
                    help="serial port; detected from /dev/cu.usbmodem* if omitted")
    ap.add_argument("--assume-x", type=float, default=None,
                    help="for --dry-run only: plan from this X when the board is "
                         "not reachable")
    ap.add_argument("--span", type=float, default=100.0,
                    help="X sweep in mm, out and back (default 100)")
    ap.add_argument("--depths", default="30,50,70",
                    help="comma-separated Z depths in mm, +Z is DOWN")
    ap.add_argument("--cycles", type=int, default=1,
                    help="how many times to repeat the whole depth list")
    ap.add_argument("--feed-x", type=float, default=600.0, help="mm/min for the X sweep")
    ap.add_argument("--feed-z", type=float, default=400.0, help="mm/min for Z moves")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        depths = [float(d) for d in args.depths.split(",") if d.strip()]
    except ValueError:
        print(f"bad --depths {args.depths!r}", file=sys.stderr)
        return 2
    if not depths:
        print("--depths is empty", file=sys.stderr)
        return 2

    # Check the depth list before opening the port, so a typo never reaches the
    # board. X is checked after connecting, since it depends on where we are.
    for z in depths:
        if not (Z_TOP <= z <= Z_FLOOR):
            print(f"refusing Z={z:.1f}: usable Z is {Z_TOP:.0f} to {Z_FLOOR:.0f} mm "
                  f"below the top datum", file=sys.stderr)
            return 2

    # Detected, not hardcoded: macOS renumbers usbmodem nodes on every
    # re-enumeration, so a literal path goes stale without anything changing
    # about the robot. See core.config._opentrons_port.
    port = _opentrons_port(args.port or os.getenv("OT_SERIAL_PORT") or None)

    d: OpentronsDriver | None = None
    if port:
        d = OpentronsDriver("ot-one-scan", {"port": port})
        print(f"connecting to {port} ...")
        try:
            d.connect()
            print(f"  connected: {d.info.name}")
        except DriverError as e:
            if not args.dry_run:
                print(f"\nconnect failed: {e}", file=sys.stderr)
                return 2
            print(f"  connect failed, planning offline: {e}")
            d = None
    elif not args.dry_run:
        print("no OT serial port: there is no /dev/cu.usbmodem* node, so the board "
              "is off the USB bus rather than merely renumbered. Power it off, "
              "unplug USB, wait 5 s, replug.", file=sys.stderr)
        return 2

    try:
        if d is not None:
            start = d.machine_position()
            x0 = start.get("X")
            z0 = start.get("Z")
            if x0 is None or z0 is None:
                raise DriverError(f"board did not report X and Z: {start}")
            print(f"  at X={x0:.1f} Y={start.get('Y', float('nan')):.1f} Z={z0:.1f}")
        else:
            # Dry run with nothing to ask. Say so loudly: the whole safety
            # argument here rests on knowing where the machine actually is, and
            # this number is not that.
            x0 = 300.0 if args.assume_x is None else args.assume_x
            z0 = 0.0
            print(f"  ASSUMING X={x0:.1f}, unverified. The board was not read.")

        sign = sweep_sign(x0, args.span)
        far = x0 + sign * args.span
        print(f"\nplan: {args.cycles} cycle(s) over depths {depths}")
        print(f"  sweep X {x0:.0f} -> {far:.0f} -> {x0:.0f} mm at Z={Z_TOP:.0f} "
              f"({'-X' if sign < 0 else '+X'}, the side with room)")
        print(f"  then descend to each of {', '.join(f'{z:.0f}' for z in depths)} mm")
        print(f"  X stays inside {X_MIN:.0f}..{X_MAX:.0f}, Z inside "
              f"{Z_TOP:.0f}..{Z_FLOOR:.0f}")

        if args.dry_run:
            print("\ndry run, nothing moved.")
            return 0

        print("\n*** MOTION. WATCH IT. Ctrl-C cuts motion. ***\n")
        sweep_mm = 2.0 * args.span

        for cycle in range(1, args.cycles + 1):
            for z in depths:
                # 1. Up first, always. The traverse only happens at full
                #    clearance, so the sweep never has to know what is below it.
                t0 = time.time()
                d.move_to_machine(Z=Z_TOP, feedrate=args.feed_z)
                t_up = time.time() - t0

                # 2. Sweep X out and back, X only. A short Z mixed into a long X
                #    leg makes Z crawl at the ratio of the two and the steppers
                #    growl, so each axis gets its own leg.
                t0 = time.time()
                d.jog_path([{"X": sign * args.span}, {"X": -sign * args.span}],
                           feedrate=args.feed_x, max_total_mm=sweep_mm + 50.0)
                t_scan = time.time() - t0

                # 3. Down to this cycle's depth.
                t0 = time.time()
                d.move_to_machine(Z=z, feedrate=args.feed_z)
                t_down = time.time() - t0

                exp_scan = sweep_mm * 60.0 / args.feed_x
                exp_down = z * 60.0 / args.feed_z
                print(f"  cycle {cycle} depth {z:>4.0f} mm | "
                      f"up {t_up:5.2f}s | scan {t_scan:5.2f}s (exp {exp_scan:.1f}) | "
                      f"down {t_down:5.2f}s (exp {exp_down:.1f})")
                if t_scan > exp_scan * 1.6 or t_down > exp_down * 1.6 + 2.0:
                    print("    ran long. That can mean contact; check the deck.")

        # Park back at the top, which is the safe state to leave it in.
        d.move_to_machine(Z=Z_TOP, feedrate=args.feed_z)
        end = d.machine_position()
        print(f"\ndone. parked at X={end.get('X', float('nan')):.1f} "
              f"Y={end.get('Y', float('nan')):.1f} Z={end.get('Z', float('nan')):.1f}")
        print(f"  X returned to its start: {abs(end.get('X', x0) - x0) < 0.01}")
    except BaseException as e:
        print(f"\nABORTED: {type(e).__name__}: {e}")
        # Fire the estop BEFORE unwinding. Closing the serial port does not stop
        # an in-flight move: an early version raised on timeout and closed the
        # port, and the axis kept grinding.
        if d is not None:
            if d.estop():
                print("emergency stop written to the board.")
            else:
                print("emergency stop could NOT be written. CUT POWER AT THE SWITCH.")
        return 1
    finally:
        if d is not None:
            d.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
