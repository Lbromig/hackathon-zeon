#!/usr/bin/env python3
"""Raise Z, then traverse X, using RELATIVE moves only.

For the state right after a power cycle, when the position counters have been
zeroed but the machine has not physically moved. Absolute positioning is wrong
in exactly that window and wrong in a way that drives into a hard stop: the
carriage was left near the far end of X, the counter now reads 0, so anything
trusting it believes the whole travel is ahead when almost none of it is.

Relative moves need no datum, which is why everything here is relative. Up is
the safe direction on Z, so the raise happens first and always.

Usage:
    python3 scripts/raise_and_traverse.py                 # 30 mm up, 100 mm -X
    python3 scripts/raise_and_traverse.py --up 45
    python3 scripts/raise_and_traverse.py --x -150
    python3 scripts/raise_and_traverse.py --dry-run

+Z is DOWN on this machine, so --up is given as a positive number and sent as a
negative Z. --x defaults NEGATIVE because X homes to one end and the carriage
sits near the far one; see hardware/ot_one_envelope.json.

A crash is invisible to software: with no endstops and no current sensing a
stalled stepper skips steps and the timing looks identical to a clean move. What
this can catch is a move that never ran at all, which shows up as a duration far
under the prediction. Ctrl-C fires an emergency stop.
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, ".")

from drivers.opentrons.driver import DriverError, OpentronsDriver  # noqa: E402
from scripts import require_port  # noqa: E402

# Bounds on a single invocation. Not machine limits: limits on how far this will
# go without the operator asking again.
MAX_UP_MM = 60.0
MAX_X_MM = 280.0
# Z oscillates DOWN first, toward the deck and a fitted tip, and its height above
# the deck is not known after a power cycle drops holding torque and lets it sag.
# So the per-run excursion is kept small deliberately.
MAX_DZ_MM = 40.0


def timed(label: str, fn, expected_s: float) -> float:
    """Run one leg and report how long it took against the prediction.

    A leg that finishes far too fast did not run. That is the halt signature:
    a halted board silently ignores G-code, so the commands ack and the motion
    never happens. It is the one failure mode duration CAN detect.
    """
    t0 = time.time()
    fn()
    dt = time.time() - t0
    flag = ""
    if expected_s > 0.5 and dt < expected_s * 0.5:
        flag = "  <-- FAR TOO FAST. The move did not run; check for a halt."
    elif expected_s > 0.5 and dt > expected_s * 1.6:
        flag = "  <-- ran long. That can mean contact; check the deck."
    print(f"  {label}: {dt:.2f}s (expected ~{expected_s:.2f}s){flag}")
    return dt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None,
                    help="serial port; detected from /dev/cu.usbmodem* if omitted")
    ap.add_argument("--up", type=float, default=30.0,
                    help=f"raise in mm, positive (cap {MAX_UP_MM:.0f})")
    ap.add_argument("--x", type=float, default=-100.0,
                    help=f"X travel in mm, negative is toward the homed end "
                         f"(cap {MAX_X_MM:.0f})")
    ap.add_argument("--dz", type=float, default=0.0,
                    help=f"Z excursion in mm for --z-laps, DOWN first "
                         f"(cap {MAX_DZ_MM:.0f})")
    ap.add_argument("--z-laps", type=int, default=0,
                    help="oscillate Z down-and-up this many times, queued as ONE "
                         "continuous path. Runs after the raise, before X.")
    ap.add_argument("--laps", type=int, default=0,
                    help="repeat X out-and-back this many times, queued as ONE "
                         "continuous path. 0 = a single one-way move.")
    ap.add_argument("--feed-z", type=float, default=300.0, help="mm/min for Z")
    ap.add_argument("--feed-x", type=float, default=600.0, help="mm/min for X")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.up < 0:
        print("--up is a positive number of mm to rise", file=sys.stderr)
        return 2
    if args.up > MAX_UP_MM:
        print(f"refusing a {args.up:.0f} mm raise: cap is {MAX_UP_MM:.0f} mm per run",
              file=sys.stderr)
        return 2
    if abs(args.dz) > MAX_DZ_MM:
        print(f"refusing a {args.dz:.0f} mm Z excursion: cap is {MAX_DZ_MM:.0f} mm "
              f"per run, because Z's height above the deck is not known",
              file=sys.stderr)
        return 2
    if abs(args.x) > MAX_X_MM:
        print(f"refusing a {args.x:.0f} mm X move: cap is {MAX_X_MM:.0f} mm per run",
              file=sys.stderr)
        return 2

    exp_z = args.up * 60.0 / args.feed_z
    exp_x = abs(args.x) * 60.0 / args.feed_x
    print(f"plan: raise {args.up:.0f} mm (Z {-args.up:+.0f}, up), "
          f"then X {args.x:+.0f} mm")
    print(f"  relative moves only, so nothing depends on the zeroed counters")
    if args.x > 0:
        print("  NOTE: +X is toward the dead end at ~350 mm. Only do this if you "
              "know the carriage is not already near it.")
    if args.dry_run:
        print("\ndry run, nothing moved.")
        return 0

    port = require_port(args.port)
    d = OpentronsDriver("ot-one-raise", {"port": port})
    print(f"connecting to {port} ...")
    # connect() sends M999 unconditionally, which is what clears a latched HALT.
    # jog_z.py talks raw serial and does not, which is how a halted board silently
    # swallowed a jog and reported it as a 1.00s move.
    d.connect()
    print(f"  connected: {d.info.name}")

    try:
        print(f"  counters before: {d.machine_position()}")
        print("\n*** MOTION. WATCH IT. Ctrl-C cuts motion. ***\n")

        timed(f"raise {args.up:.0f} mm",
              lambda: d.jog_path([{"Z": -args.up}], feedrate=args.feed_z,
                                 max_total_mm=args.up + 10.0),
              exp_z)

        if args.z_laps > 0 and abs(args.dz) > 1e-6:
            zpath = [{"Z": args.dz}, {"Z": -args.dz}] * args.z_laps
            ztotal = abs(args.dz) * 2 * args.z_laps
            timed(f"{args.z_laps} Z lap(s) of {args.dz:+.0f}/{-args.dz:+.0f} mm",
                  lambda: d.jog_path(zpath, feedrate=args.feed_z,
                                     max_total_mm=ztotal + 10.0),
                  ztotal * 60.0 / args.feed_z)
        if abs(args.x) > 1e-6:
            if args.laps > 0:
                # Out and back, every lap queued in ONE call. Splitting per lap
                # drains the planner between them, which is a full stop the
                # operator sees as a hitch.
                path = [{"X": args.x}, {"X": -args.x}] * args.laps
                total = abs(args.x) * 2 * args.laps
                timed(f"{args.laps} lap(s) of X {args.x:+.0f}/{-args.x:+.0f} mm",
                      lambda: d.jog_path(path, feedrate=args.feed_x,
                                         max_total_mm=total + 25.0),
                      total * 60.0 / args.feed_x)
            else:
                timed(f"X {args.x:+.0f} mm",
                      lambda: d.jog_path([{"X": args.x}], feedrate=args.feed_x,
                                         max_total_mm=abs(args.x) + 25.0),
                      exp_x)

        print(f"\n  counters after: {d.machine_position()}")
        print("  These are relative to wherever the board was zeroed, not to any "
              "physical datum.")
    except BaseException as e:
        print(f"\nABORTED: {type(e).__name__}: {e}")
        # Before unwinding: closing the port does NOT stop an in-flight move.
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
