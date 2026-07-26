#!/usr/bin/env python3
"""Find the OT-One's usable travel per axis, incrementally, operator-in-the-loop.

Why this cannot be automated: the machine has no working endstops and no current
sensing. When an axis reaches a hard stop the driver keeps issuing steps, the motor
skips them silently, and the move still "completes" in exactly the predicted time.
The position counter is open-loop so it reports the commanded value regardless, and
even an out-and-back loop closes to 0.00 on paper. Every software signal available
says the move was fine. **The operator hearing it is the only detector.**

So this grows one axis at a time in bounded steps, out and back, printing each
trial and pausing so a human can stop it. Whatever is confirmed good gets written
to hardware/ot_one_envelope.json, so the number is recorded once instead of
rediscovered by feel every session.

Usage:
    python3 scripts/find_envelope.py --axis X --start 150 --step 20 --trials 4
    python3 scripts/find_envelope.py --axis Y --start 95  --step 15 --trials 4
    python3 scripts/find_envelope.py --show          # print what is known so far

Between trials it waits for you: ENTER to continue, 'n' to mark the last one as
too far and stop. Ctrl-C fires an emergency stop.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, ".")

from drivers.opentrons.driver import OpentronsDriver  # noqa: E402
from scripts import require_port  # noqa: E402
STORE = pathlib.Path("hardware/ot_one_envelope.json")

# Confirmed good by earlier runs this session, as half-travel from centre.
KNOWN_GOOD = {"X": 150.0, "Y": 95.0, "Z": 12.0, "A": 6.0}


def load() -> dict:
    if STORE.exists():
        try:
            return json.loads(STORE.read_text())
        except Exception:
            pass
    return {"confirmed_mm": dict(KNOWN_GOOD), "rejected_mm": {}, "notes": []}


def save(data: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(data, indent=2) + "\n")


def show(data: dict) -> None:
    print("confirmed usable travel (mm, out-and-back from the current position):")
    for a, v in sorted(data["confirmed_mm"].items()):
        rej = data["rejected_mm"].get(a)
        tail = f"   (rejected at {rej} mm)" if rej else ""
        print(f"  {a}: {v:.0f}{tail}")
    if data["notes"]:
        print("notes:")
        for n in data["notes"]:
            print(f"  - {n}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None,
                    help="serial port; detected from /dev/cu.usbmodem* if omitted")
    ap.add_argument("--axis", choices=["X", "Y", "Z", "A"])
    ap.add_argument("--start", type=float, help="first trial distance, mm")
    ap.add_argument("--step", type=float, default=20.0, help="growth per trial, mm")
    ap.add_argument("--trials", type=int, default=4)
    ap.add_argument("--feed", type=float, default=600.0)
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--yes", action="store_true",
                    help="do not pause between trials (only if you are watching)")
    args = ap.parse_args()

    data = load()
    if args.show or not args.axis:
        show(data)
        if not args.axis:
            print("\npass --axis to grow one. Nothing moved.")
        return 0

    axis = args.axis
    start = args.start if args.start is not None else data["confirmed_mm"].get(axis, 20.0)

    port = require_port(args.port)
    d = OpentronsDriver("ot-one-envelope", {"port": port})
    print(f"connecting to {port} ...")
    d.connect()
    print(f"  connected: {d.info.name}\n")
    print(f"Growing {axis} from {start:.0f} mm in {args.step:.0f} mm steps, "
          f"{args.trials} trials, out-and-back each time.")
    print("LISTEN. A grind or a change in pitch is the ONLY signal that it hit a")
    print("stop — the timing and the position counter will both look perfect.\n")

    confirmed = data["confirmed_mm"].get(axis, start)
    try:
        for i in range(args.trials):
            dist = start + i * args.step
            expected = 2 * dist * 60.0 / args.feed
            print(f"  trial {i+1}/{args.trials}: {axis} +{dist:.0f} then -{dist:.0f} "
                  f"(expect ~{expected:.1f}s)")
            t0 = time.time()
            d.jog_path([{axis: dist}, {axis: -dist}], feedrate=args.feed,
                       max_total_mm=2 * dist + 50)
            dt = time.time() - t0
            print(f"    completed in {dt:.2f}s (exp {expected:.2f}s) "
                  f"— proves the steps were issued, NOT that it was clear")

            if args.yes:
                confirmed = dist
                continue
            reply = input("    sounded clean? ENTER = yes, 'n' = too far, stop: ").strip().lower()
            if reply == "n":
                data["rejected_mm"][axis] = dist
                data["notes"].append(
                    f"{axis}: operator rejected {dist:.0f} mm; last good {confirmed:.0f} mm")
                print(f"    marked {dist:.0f} mm as too far. Stopping.")
                break
            confirmed = dist

        data["confirmed_mm"][axis] = confirmed
        save(data)
        print(f"\nrecorded: {axis} confirmed usable to {confirmed:.0f} mm -> {STORE}")
        show(data)
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
