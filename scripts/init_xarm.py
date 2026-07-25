#!/usr/bin/env python3
"""Initialize one or more UFACTORY xArm Lite 6 arms into a ready-to-move state.

Brings an arm from power-on to controllable: connect -> clear errors/warnings ->
enable motion -> position mode (0) -> ready state (0) -> sane motion/safety defaults,
then prints a status report. Optionally homes the arm.

Usage:
    python scripts/init_xarm.py --ip 192.168.1.10
    python scripts/init_xarm.py --ip 192.168.1.10 --ip 192.168.1.11 --home
    python scripts/init_xarm.py --fleet          # read xArm IPs from backend config
    python scripts/init_xarm.py --ip 192.168.1.10 --gripper lite6 --dry-run

SDK: pip install xarm-python-sdk   (or: pip install -e third_party/xArm-Python-SDK)
Docs: https://docs.api.ufactory.cc/xarm_python_sdk_docs/1.%20xArm-Python-SDK%20Installation.html
"""
from __future__ import annotations

import argparse
import sys
import time

try:
    from xarm.wrapper import XArmAPI
except Exception as e:  # pragma: no cover
    sys.exit(f"xArm SDK not installed ({e}); run: pip install xarm-python-sdk")

# Safe defaults for Lite 6 (mm, mm/s, mm/s^2)
DEFAULT_TCP_SPEED = 100
DEFAULT_TCP_ACC = 2000
COLLISION_SENSITIVITY = 3          # 0..5 (higher = more sensitive)
DEFAULT_PAYLOAD_KG = 0.5           # set to your real end-effector + tube load


def _ok(code, what) -> None:
    """xArm APIs return 0 on success; raise with context otherwise."""
    if code != 0:
        raise RuntimeError(f"{what} failed (code={code})")


def init_arm(ip: str, name: str = "", *, home: bool = False, gripper: str = "lite6",
             payload_kg: float = DEFAULT_PAYLOAD_KG, dry_run: bool = False) -> bool:
    label = name or ip
    print(f"\n=== Initializing xArm '{label}' @ {ip} ===")
    if dry_run:
        print("  [dry-run] would connect + enable + set mode/state; skipping.")
        return True

    arm = XArmAPI(ip, is_radian=False)
    try:
        # surface faults as they happen
        arm.register_error_warn_changed_callback(
            lambda d: print(f"  [{label}] error={d.get('error_code')} warn={d.get('warn_code')}")
        )
        if not arm.connected:
            arm.connect()
        for _ in range(50):
            if arm.connected:
                break
            time.sleep(0.1)
        if not arm.connected:
            raise RuntimeError("could not connect")

        print(f"  connected · SDK={arm.version} · SN={arm.sn} · mode={arm.mode} state={arm.state}")

        # clear any latched faults, then bring the arm live
        arm.clean_warn()
        arm.clean_error()
        _ok(arm.motion_enable(enable=True), "motion_enable")
        _ok(arm.set_mode(0), "set_mode(0)")     # 0 = position control
        _ok(arm.set_state(0), "set_state(0)")   # 0 = ready
        time.sleep(0.5)

        if arm.error_code != 0:
            raise RuntimeError(f"arm in error state after init (error_code={arm.error_code})")

        # motion + safety defaults
        arm.set_tcp_maxacc(DEFAULT_TCP_ACC)
        arm.set_collision_sensitivity(COLLISION_SENSITIVITY)
        arm.set_self_collision_detection(True)
        arm.set_tcp_load(payload_kg, [0, 0, 0])

        _init_gripper(arm, gripper, label)

        if home:
            print("  homing...")
            _ok(arm.move_gohome(wait=True, speed=DEFAULT_TCP_SPEED), "move_gohome")

        _report(arm, label)
        return arm.error_code == 0
    except Exception as e:
        print(f"  ERROR initializing '{label}': {e}")
        return False
    finally:
        arm.disconnect()


def _init_gripper(arm: XArmAPI, gripper: str, label: str) -> None:
    """Prepare the end-effector. Lite 6 uses the pneumatic gripper API."""
    try:
        if gripper == "lite6":
            arm.open_lite6_gripper(); time.sleep(0.5); arm.stop_lite6_gripper()
            print("  gripper: Lite6 pneumatic — opened + ready")
        elif gripper == "xarm":
            arm.set_gripper_mode(0); arm.set_gripper_enable(True); arm.set_gripper_speed(2000)
            print("  gripper: xArm gripper — enabled")
        elif gripper == "bio":
            arm.set_bio_gripper_enable(True)
            print("  gripper: BIO gripper — enabled")
        else:
            print("  gripper: none")
    except Exception as e:
        print(f"  gripper init skipped ({e})")


def _report(arm: XArmAPI, label: str) -> None:
    code, pos = arm.get_position()
    _, angles = arm.get_servo_angle()
    print(f"  [{label}] READY  state={arm.state} error={arm.error_code} warn={arm.warn_code}")
    if code == 0:
        print(f"           TCP pose (mm,deg): {[round(v, 1) for v in pos]}")
        print(f"           joints (deg):      {[round(v, 1) for v in angles]}")


def _fleet_ips() -> list[tuple[str, str]]:
    """Read xArm entries from the backend fleet config, if available."""
    sys.path.insert(0, "backend")
    try:
        from app.core.config import settings
        return [(d["ip"], d.get("name", d["id"])) for d in settings.fleet if d["type"] == "xarm"]
    except Exception as e:
        sys.exit(f"--fleet: could not read backend config ({e})")


def main() -> int:
    ap = argparse.ArgumentParser(description="Initialize xArm Lite 6 arm(s).")
    ap.add_argument("--ip", action="append", default=[], help="arm IP (repeatable)")
    ap.add_argument("--fleet", action="store_true", help="init all xArms from backend config")
    ap.add_argument("--home", action="store_true", help="home each arm after init")
    ap.add_argument("--gripper", choices=["lite6", "xarm", "bio", "none"], default="lite6")
    ap.add_argument("--payload", type=float, default=DEFAULT_PAYLOAD_KG, help="payload kg")
    ap.add_argument("--dry-run", action="store_true", help="don't touch hardware")
    args = ap.parse_args()

    targets = _fleet_ips() if args.fleet else [(ip, "") for ip in args.ip]
    if not targets:
        ap.error("provide --ip <addr> (repeatable) or --fleet")

    results = {label or ip: init_arm(ip, label, home=args.home, gripper=args.gripper,
                                     payload_kg=args.payload, dry_run=args.dry_run)
               for ip, label in targets}

    print("\n=== Summary ===")
    for name, ok in results.items():
        print(f"  {'OK ' if ok else 'FAIL'}  {name}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
