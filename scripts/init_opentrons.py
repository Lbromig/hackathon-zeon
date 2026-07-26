#!/usr/bin/env python3
"""Initialize the Opentrons OT-One: prove each gantry axis moves, without homing.

Brings the OT-One from power-on to "known responsive": open the serial port -> confirm the
firmware answers -> read endstops and position -> jog X, Y and Z a few mm in each direction
-> report. Nothing is homed, and the A/B pipette plungers are never touched.

Why not home. `G28.2` (the code Opentrons' own software uses) was tried on this unit and
left the firmware unresponsive to everything including `M112` and `Ctrl-X`, recoverable only
by a power cycle: the board answers a bare `ok` and then blocks in its main loop. Because
Smoothieware services serial from that same loop, a blocked move means nothing further is
read from the port — commands sent into that silence queue up and run later. So this script
verifies motion instead, and the driver refuses to home. See drivers/opentrons/driver.py.

Direction is never assumed. An axis resting on its min endstop is moved *away* from it and
the release is verified with `M119`; measured on this machine, Z parks on `min_z` and
positive Z moves *toward* that switch, so a naive "+ is away from min" drives into it.

Usage:
    python scripts/init_opentrons.py                      # port from OT_SERIAL_PORT / autodetect
    python scripts/init_opentrons.py --port /dev/cu.usbmodem11301
    python scripts/init_opentrons.py --dry-run            # connect + report, move nothing
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from drivers.base import DriverError                        # noqa: E402
from drivers.opentrons.driver import OpentronsDriver        # noqa: E402

OK, FAIL, INFO = "[ ok ]", "[FAIL]", "      "


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=None,
                    help="serial port (default: OT_SERIAL_PORT, else autodetect)")
    ap.add_argument("--dry-run", action="store_true",
                    help="connect and report state, but move nothing")
    ap.add_argument("--json", action="store_true", help="machine-readable report on stdout")
    args = ap.parse_args()

    port = args.port
    if not port:
        # Read the configured fleet rather than the env var directly, so this agrees with
        # what the backend would use.
        from core.config import Settings
        for entry in Settings.load().fleet:
            if entry.get("type") == "opentrons":
                port = entry.get("port")
                break

    driver = OpentronsDriver("ot", {"port": port} if port else {})
    try:
        driver.connect()
    except DriverError as e:
        print(f"{FAIL} {e}")
        return 6

    status = driver.status()
    print(f"{OK} connected on {driver.config.get('port')}")
    print(f"{INFO} firmware  {status['firmware']}")
    print(f"{INFO} endstops  {_fmt_endstops(status['endstops'])}")
    print(f"{INFO} position  {_fmt_position(status['position'])}")

    if args.dry_run:
        print(f"{INFO} --dry-run: nothing moved")
        driver.disconnect()
        return 0

    try:
        report = driver.initialize()
    except DriverError as e:
        print(f"{FAIL} {e}")
        driver.disconnect()
        return 7
    finally:
        pass

    print()
    for axis, result in report["axes"].items():
        where = "started ON its endstop" if result["started_on_endstop"] else "clear of endstops"
        print(f"{OK} {axis}: moved {result['moved_mm']:g} mm each way "
              f"({where}, net {result['net_mm']:+g} mm)")
    print()
    print(f"{INFO} endstops  {_fmt_endstops(report['endstops'])}")
    print(f"{INFO} position  {_fmt_position(report['position'])}")
    print(f"{INFO} not homed — coordinates are relative only, and commanded (upper case) may")
    print(f"{INFO} differ from actual (lower case) if any move was ever cut short")

    driver.disconnect()
    if args.json:
        print(json.dumps(report, indent=2))
    return 0


def _fmt_endstops(endstops: dict) -> str:
    if not endstops:
        return "(unknown)"
    return "  ".join(f"{name}={'TRIGGERED' if hit else 'open'}"
                     for name, hit in sorted(endstops.items()))


def _fmt_position(position: dict) -> str:
    if not position:
        return "(unknown)"
    upper = {k: v for k, v in position.items() if k.isupper()}
    lower = {k: v for k, v in position.items() if k.islower()}
    return (f"commanded {' '.join(f'{k}{v:g}' for k, v in sorted(upper.items()))} | "
            f"actual {' '.join(f'{k}{v:g}' for k, v in sorted(lower.items()))}")


if __name__ == "__main__":
    raise SystemExit(main())
