#!/usr/bin/env python3
"""Initialize one or more UFACTORY xArm arms into a ready-to-move state.

Brings an arm from power-on to controllable: connect -> clear errors/warnings ->
enable motion -> position mode (0) -> ready state (0) -> sane motion/safety defaults
-> exercise the gripper (open/close/open) -> status report. Optionally homes the arm.

The arm is ALWAYS left braked: `teardown()` runs in a finally block, so the servos
are de-energized and the holding brakes engage whether init succeeds, fails, or
raises. Re-enabling is the caller's job (XArmDriver.connect() does it).

Usage:
    python scripts/init_xarm.py --ip 192.168.3.13
    python scripts/init_xarm.py --ip 192.168.3.13 --ip 192.168.3.11 --home
    python scripts/init_xarm.py --fleet          # read xArm IPs from backend config
    python scripts/init_xarm.py --ip 192.168.3.13 --gripper xarm --dry-run

SDK: uv sync   (vendored at third_party/xArm-Python-SDK)
Docs: https://docs.api.ufactory.cc/xarm_python_sdk_docs/1.%20xArm-Python-SDK%20Installation.html
"""
from __future__ import annotations

import argparse
import sys
import time

try:
    from xarm.wrapper import XArmAPI
except Exception as e:  # pragma: no cover
    sys.exit(f"xArm SDK not installed ({e}); run: uv sync")

# Motion / safety defaults (mm, mm/s, mm/s^2)
DEFAULT_TCP_SPEED = 100
DEFAULT_TCP_ACC = 2000
COLLISION_SENSITIVITY = 3          # 0..5 (higher = more sensitive)

# End-effector mass and its centre of gravity in flange coords (mm). The controller
# derives gravity/torque compensation from these, so a wrong value shows up as sag
# the moment the servos hand off to the brakes -- and as skewed collision detection.
# TODO: measure for the actual gripper + carried labware; xArm parallel gripper is ~0.86 kg.
DEFAULT_PAYLOAD_KG = 0.86
DEFAULT_PAYLOAD_COM = [0.0, 0.0, 55.0]

# Braking with the TCP far out horizontally loads J2/J3 hard; the wind-up in the
# drivetrain unwinds into the brake backlash and the arm visibly droops.
SAG_WARN_RADIUS_MM = 350

# Parallel (xArm) gripper: position is 0 (closed) .. 850 (open)
GRIPPER_OPEN_POS = 850
GRIPPER_CLOSED_POS = 0
GRIPPER_SPEED = 2000
PNEUMATIC_SETTLE_S = 0.6           # Lite6 valve needs time to actually move

# arm.device_type -> model. Lite 6 is 9, the 850 is 12; xArm 5/6/7 report their axis count.
DEVICE_TYPE_LITE6 = 9
DEVICE_TYPE_850 = 12

# arm.state as *reported* (note set_state() uses a different numbering: 0=motion, 3=pause, 4=stop)
STATE_NAMES = {0: "ready", 1: "in motion", 2: "idle", 3: "suspended", 4: "stopped"}


def _state(arm: XArmAPI) -> str:
    return f"{arm.state} ({STATE_NAMES.get(arm.state, '?')})"


def _ok(code, what) -> None:
    """xArm APIs return 0 on success; raise with context otherwise."""
    if code != 0:
        raise RuntimeError(f"{what} failed (code={code})")


def _model_name(arm: XArmAPI) -> str:
    if arm.axis == 6 and arm.device_type == DEVICE_TYPE_LITE6:
        return "Lite 6"
    if arm.axis == 6 and arm.device_type == DEVICE_TYPE_850:
        return "xArm 850"
    return f"xArm {arm.axis}"


def _resolve_gripper(arm: XArmAPI, requested: str) -> str:
    """`auto` picks the end-effector API that matches the connected model."""
    if requested != "auto":
        return requested
    return "lite6" if arm.device_type == DEVICE_TYPE_LITE6 else "xarm"


def init_arm(ip: str, name: str = "", *, home: bool = False, gripper: str = "auto",
             payload_kg: float = DEFAULT_PAYLOAD_KG, payload_com: list[float] | None = None,
             save_conf: bool = False, leave_enabled: bool = False,
             dry_run: bool = False) -> bool:
    label = name or ip
    print(f"\n=== Initializing xArm '{label}' @ {ip} ===")
    if dry_run:
        print("  [dry-run] would connect + enable + set mode/state + cycle gripper; skipping.")
        return True

    try:
        arm = XArmAPI(ip, is_radian=False)
    except Exception as e:
        # Constructing the API can fail outright (bad IP, no route) — bail before
        # the finally-block below would reference an unbound `arm`.
        print(f"  ERROR: could not open '{label}' @ {ip}: {e}")
        return False

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

        print(f"  connected · {_model_name(arm)} · fw={arm.version} · SN={arm.sn} "
              f"· mode={arm.mode} state={_state(arm)}")

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

        com = list(payload_com if payload_com is not None else DEFAULT_PAYLOAD_COM)
        _ok(arm.set_tcp_load(payload_kg, com, wait=True), "set_tcp_load")
        print(f"  payload: {payload_kg} kg, CoG {com} mm (drives gravity compensation)")
        if save_conf:
            # tcp_load / sensitivity are volatile otherwise -- lost on controller reboot.
            _ok(arm.save_conf(), "save_conf")
            print("  config saved to the controller (survives reboot)")

        _init_gripper(arm, _resolve_gripper(arm, gripper), label)

        if home:
            # Homing folds the arm in, which is also the low-torque pose to brake at.
            # NOTE: move_gohome is documented as running without limit detection --
            # the path is not collision-checked, so the workspace must be clear.
            print("  homing (path is NOT collision-checked — workspace must be clear)...")
            _ok(arm.move_gohome(wait=True, speed=DEFAULT_TCP_SPEED), "move_gohome")

        _report(arm, label)
        return arm.error_code == 0
    except Exception as e:
        print(f"  ERROR initializing '{label}': {e}")
        return False
    finally:
        teardown(arm, label, leave_enabled=leave_enabled)


def _wait_until_stopped(arm: XArmAPI, timeout: float = 10.0) -> bool:
    """Block until the arm reports it is no longer moving (reported state 1 = in motion).

    XArmAPI is a flat wrapper and does not expose the SDK's internal wait_move(),
    so poll the reported state instead.
    """
    end = time.time() + timeout
    while time.time() < end:
        if arm.state != 1:
            return True
        time.sleep(0.1)
    return False


def _warn_if_braking_under_load(arm: XArmAPI, label: str) -> None:
    """Braking far from the base is what makes the arm droop and clunk.

    Sag scales with the horizontal moment arm: the servos hand off to mechanical
    brakes, and the wind-up stored in the drivetrain unwinds into the brake backlash.
    Folding the arm in first (``--home``) cuts the lever and most of the droop.
    """
    try:
        x, y = arm.position[:2]
    except Exception:
        return
    radius = (x * x + y * y) ** 0.5
    if radius > SAG_WARN_RADIUS_MM:
        print(f"  [{label}] NOTE: braking at {radius:.0f} mm horizontal reach "
              f"(> {SAG_WARN_RADIUS_MM} mm) — expect droop/clunk; "
              f"re-run with --home to park folded first")


def teardown(arm: XArmAPI, label: str, *, leave_enabled: bool = False) -> None:
    """Always leave the arm mechanically safe: STOP, servos off, brakes engaged.

    Runs on every exit path. Disabling the servos is what engages the holding
    brakes — without it the arm stays energized, and a later power cut would let
    it sag under gravity.

    `leave_enabled` hands the arm over still live (for the backend to drive), which
    avoids a pointless brake cycle — every engage/release costs a clunk and a little
    droop, so don't do it if something is about to re-enable the arm anyway.
    """
    try:
        if not arm.connected:
            return
        if leave_enabled:
            print(f"  [{label}] teardown: leaving arm ENABLED (brakes released) as requested")
            return

        # Best-effort pre-checks only: never let these stop us from braking below.
        try:
            _warn_if_braking_under_load(arm, label)
            _wait_until_stopped(arm)            # don't brake mid-trajectory
        except Exception as e:
            print(f"  [{label}] teardown pre-check skipped ({e})")

        arm.set_state(4)                        # 4 = STOP
        time.sleep(0.2)                         # let the drivetrain settle before brakes take over
        arm.motion_enable(enable=False)         # servos off -> holding brakes engage
        time.sleep(0.3)
        # motor_brake_states: 0 = brake engaged, 1 = released (first `axis` entries valid)
        brakes = list(arm.motor_brake_states or [])[: arm.axis or 6]
        held = all(b == 0 for b in brakes) if brakes else None
        print(f"  [{label}] teardown: state={_state(arm)} brakes={brakes}"
              f" -> {'all engaged' if held else 'CHECK: not all engaged'}")
    except Exception as e:
        print(f"  [{label}] WARNING: teardown failed, arm may still be energized: {e}")
    finally:
        try:
            arm.disconnect()
        except Exception:
            pass


def _init_gripper(arm: XArmAPI, gripper: str, label: str) -> None:
    """Enable the end-effector and exercise it: open -> close -> open.

    Leaves the gripper OPEN so the arm is ready to grasp and isn't holding anything.
    """
    try:
        if gripper == "lite6":
            # Pneumatic: no position feedback, so drive the valve and wait it out.
            arm.open_lite6_gripper(); time.sleep(PNEUMATIC_SETTLE_S)
            arm.close_lite6_gripper(); time.sleep(PNEUMATIC_SETTLE_S)
            arm.open_lite6_gripper(); time.sleep(PNEUMATIC_SETTLE_S)
            arm.stop_lite6_gripper()            # de-energize the valve, stays open
            print("  gripper: Lite6 pneumatic — cycled open/close, left open")
        elif gripper == "xarm":
            _ok(arm.set_gripper_mode(0), "set_gripper_mode(0)")
            _ok(arm.set_gripper_enable(True), "set_gripper_enable")
            _ok(arm.set_gripper_speed(GRIPPER_SPEED), "set_gripper_speed")
            _ok(arm.set_gripper_position(GRIPPER_OPEN_POS, wait=True), "gripper open")
            _ok(arm.set_gripper_position(GRIPPER_CLOSED_POS, wait=True), "gripper close")
            _ok(arm.set_gripper_position(GRIPPER_OPEN_POS, wait=True), "gripper open")
            print("  gripper: xArm parallel — cycled open/close, left open")
        elif gripper == "bio":
            _ok(arm.set_bio_gripper_enable(True), "set_bio_gripper_enable")
            _ok(arm.open_bio_gripper(wait=True), "bio open")
            _ok(arm.close_bio_gripper(wait=True), "bio close")
            _ok(arm.open_bio_gripper(wait=True), "bio open")
            print("  gripper: BIO — cycled open/close, left open")
        else:
            print("  gripper: none (skipped)")
    except Exception as e:
        # A missing/mismatched end-effector shouldn't fail the arm init.
        print(f"  gripper: '{gripper}' cycle skipped ({e})")


def _report(arm: XArmAPI, label: str) -> None:
    code, pos = arm.get_position()
    _, angles = arm.get_servo_angle()
    print(f"  [{label}] READY  state={_state(arm)} error={arm.error_code} warn={arm.warn_code}")
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
    ap = argparse.ArgumentParser(description="Initialize xArm arm(s).")
    ap.add_argument("--ip", action="append", default=[], help="arm IP (repeatable)")
    ap.add_argument("--fleet", action="store_true", help="init all xArms from backend config")
    ap.add_argument("--home", action="store_true", help="home each arm after init")
    ap.add_argument("--gripper", choices=["auto", "lite6", "xarm", "bio", "none"], default="auto",
                    help="end-effector API; 'auto' picks from the connected model")
    ap.add_argument("--payload", type=float, default=DEFAULT_PAYLOAD_KG,
                    help=f"end-effector mass in kg (default {DEFAULT_PAYLOAD_KG})")
    ap.add_argument("--payload-com", type=float, nargs=3, metavar=("X", "Y", "Z"),
                    default=DEFAULT_PAYLOAD_COM,
                    help=f"payload centre of gravity in flange coords, mm "
                         f"(default {DEFAULT_PAYLOAD_COM})")
    ap.add_argument("--save-conf", action="store_true",
                    help="persist load/sensitivity to the controller (else lost on reboot)")
    ap.add_argument("--leave-enabled", action="store_true",
                    help="skip the brake cycle and hand the arm over live (avoids a clunk "
                         "when the backend is about to re-enable it anyway)")
    ap.add_argument("--dry-run", action="store_true", help="don't touch hardware")
    args = ap.parse_args()

    targets = _fleet_ips() if args.fleet else [(ip, "") for ip in args.ip]
    if not targets:
        ap.error("provide --ip <addr> (repeatable) or --fleet")

    results = {label or ip: init_arm(ip, label, home=args.home, gripper=args.gripper,
                                     payload_kg=args.payload, payload_com=args.payload_com,
                                     save_conf=args.save_conf, leave_enabled=args.leave_enabled,
                                     dry_run=args.dry_run)
               for ip, label in targets}

    print("\n=== Summary ===")
    for name, ok in results.items():
        print(f"  {'OK ' if ok else 'FAIL'}  {name}")
    print("  all arms left ENABLED (brakes released)" if args.leave_enabled
          else "  all arms left braked (servos off)")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
