#!/usr/bin/env python3
"""Calibrate the OT-One plunger: which axis drives it, and µL per mm.

Answers Q-OT-PLUNGER-1. Until both values exist, `aspirate`/`dispense` refuse,
which is what blocks the OT side of the floor path.

Neither value can be read from the firmware — this board's config has no plunger
entries, exactly as it has none for the axis limits. And they cannot be assumed:
`M119` reports `min_b` but no `min_c`, so the two plungers are not symmetric and
the mounted side has to be found by moving them.

Two phases, both operator-in-the-loop:

  1. IDENTIFY  — jog B a little, then C a little, and you say which one moved.
  2. MEASURE   — jog the identified plunger a known distance, dispense into a
                 tared container or read a graduation, and you enter the volume.

Usage:
    python3 scripts/calibrate_plunger.py --identify
    python3 scripts/calibrate_plunger.py --measure --axis B --mm 2.0

Safety: plunger travel is short and driving one past its seal jams it, so every
move is capped at MAX_PLUNGER_JOG_MM and runs slowly. Ctrl-C fires an emergency
stop. Start from a mid-travel position, not against a stop.
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, ".")

from drivers.opentrons.driver import (  # noqa: E402
    MAX_PLUNGER_JOG_MM,
    PLUNGER_AXES,
    PLUNGER_FEED,
    DriverError,
    OpentronsDriver,
)
from scripts import require_port  # noqa: E402



def connect(port: str) -> OpentronsDriver:
    d = OpentronsDriver("ot-one-plunger", {"port": port})
    print(f"connecting to {port} ...")
    d.connect()
    print(f"  connected: {d.info.name}\n")
    return d


def nudge(d: OpentronsDriver, axis: str, mm: float) -> float:
    """One bounded plunger move, timed. Returns measured duration."""
    expected = abs(mm) * 60.0 / PLUNGER_FEED
    t0 = time.time()
    d.jog(axis, mm, feedrate=PLUNGER_FEED)
    dt = time.time() - t0
    flag = ""
    if dt > expected * 1.6:
        flag = "  <-- SLOW: resistance, it may be against a stop"
    print(f"    {axis} {mm:+.2f} mm   {dt:.2f}s (exp {expected:.2f}s){flag}")
    return dt


def phase_identify(d: OpentronsDriver, mm: float) -> None:
    print("PHASE 1 — identify which plunger is mounted")
    print(f"Jogging each candidate {mm:+.1f} mm then back. WATCH THE PIPETTES.\n")
    for axis in PLUNGER_AXES:
        print(f"  testing {axis}:")
        try:
            nudge(d, axis, mm)
            time.sleep(0.8)
            nudge(d, axis, -mm)   # return it, so nothing is left displaced
        except DriverError as e:
            print(f"    {axis} refused: {e}")
        time.sleep(1.0)
    print(
        "\nWhichever plunger visibly moved is the mounted one. Record it:\n"
        "  in core/config.py DEFAULT_FLEET, on the opentrons entry, add\n"
        '    "plunger_axis": "B"   (or "C")\n'
        "If NEITHER moved, the plunger motor is not connected — that is a\n"
        "hardware finding worth recording, not a calibration result.\n"
        "If BOTH moved, one is a spare mount; pick the one holding the tip."
    )


def phase_measure(d: OpentronsDriver, axis: str, mm: float) -> None:
    print(f"PHASE 2 — measure µL per mm on plunger {axis}")
    print(
        "Before this: a tip must be fitted and primed, its end submerged in\n"
        "water, and the plunger at a repeatable starting point.\n"
    )
    print(f"Drawing up over {mm:.2f} mm of plunger travel ...\n")
    remaining, moved = mm, 0.0
    while remaining > 1e-9:
        step = min(MAX_PLUNGER_JOG_MM, remaining)
        nudge(d, axis, -step)      # negative draws up, per the driver convention
        moved += step
        remaining -= step
        time.sleep(0.4)
    print(f"\n  drew up over {moved:.2f} mm of travel.")
    print(
        "Now dispense it into a tared container (or read the tip graduation) and\n"
        "measure the volume in µL. Then:\n"
        f"    plunger_ul_per_mm = <volume_uL> / {moved:.2f}\n"
        "Record BOTH values on the opentrons entry in core/config.py:\n"
        f'    "plunger_axis": "{axis}", "plunger_ul_per_mm": <computed>\n'
        "After that, aspirate()/dispense() work and Q-OT-PLUNGER-1 closes.\n"
        "Take the measurement at least twice — a single reading cannot show\n"
        "whether the plunger is repeatable, which is what actually matters."
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None,
                    help="serial port; detected from /dev/cu.usbmodem* if omitted")
    ap.add_argument("--identify", action="store_true", help="phase 1")
    ap.add_argument("--measure", action="store_true", help="phase 2")
    ap.add_argument("--axis", choices=list(PLUNGER_AXES), help="phase 2: mounted axis")
    ap.add_argument("--mm", type=float, default=1.0,
                    help=f"travel per test move (cap {MAX_PLUNGER_JOG_MM})")
    args = ap.parse_args()

    if not (args.identify or args.measure):
        ap.error("choose --identify or --measure")
    if args.measure and not args.axis:
        ap.error("--measure needs --axis (run --identify first)")
    if args.mm <= 0:
        ap.error("--mm must be positive")

    d = connect(require_port(args.port))
    try:
        if args.identify:
            phase_identify(d, min(args.mm, MAX_PLUNGER_JOG_MM))
        else:
            phase_measure(d, args.axis, args.mm)
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
