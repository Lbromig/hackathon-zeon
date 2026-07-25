#!/usr/bin/env python3
"""Read-only connectivity and status check for a UFACTORY xArm controller."""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
from typing import Any


PORTS = {
    502: "robot command service",
    18333: "UFACTORY Studio",
    30001: "robot status stream",
}


def controller_ip(value: str) -> str:
    """Validate a controller IPv4 address supplied on the command line."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid IP address") from exc
    if address.version != 4:
        raise argparse.ArgumentTypeError("the xArm controller requires an IPv4 address")
    return str(address)


def check_ports(ip: str, timeout: float) -> dict[str, bool]:
    """Check the three expected controller services without sending commands."""
    results: dict[str, bool] = {}
    for port, label in PORTS.items():
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                results[f"{port} ({label})"] = True
        except OSError:
            results[f"{port} ({label})"] = False
    return results


def sdk_read_only_check(ip: str, timeout: float) -> dict[str, Any]:
    """Connect through the official SDK and call getter methods only."""
    try:
        from xarm import version as xarm_sdk_version
        from xarm.wrapper import XArmAPI
    except ImportError as exc:
        raise RuntimeError(
            "The xArm Python SDK is missing. Install it with: "
            "python3 -m pip install xarm-python-sdk"
        ) from exc

    arm = XArmAPI(ip, do_not_open=True, is_radian=False)
    try:
        arm.connect(timeout=timeout)
        if not arm.connected:
            raise RuntimeError(f"The SDK could not connect to {ip}")

        return {
            "sdk_version": getattr(
                xarm_sdk_version, "__version__", str(xarm_sdk_version)
            ),
            "connected": arm.connected,
            "controller_version": arm.get_version(),
            "state": arm.get_state(),
            "error_warning": arm.get_err_warn_code(),
            "position_mm_degrees": arm.get_position(is_radian=False),
            "joint_angles_degrees": arm.get_servo_angle(is_radian=False),
        }
    finally:
        arm.disconnect()


def print_human(result: dict[str, Any]) -> None:
    """Print the diagnostic result for a person at the robot."""
    print(f"xArm controller: {result['controller_ip']}")
    for name, reachable in result["ports"].items():
        status = "OPEN" if reachable else "closed/unreachable"
        print(f"  {status:18} {name}")

    if result["connected"]:
        print("\nRead-only SDK status:")
        for name, value in result["status"].items():
            print(f"  {name}: {value}")
        print("\nPASS: controller reached; no motion commands were sent.")
    elif "sdk_error" in result:
        print(f"\nSDK check failed: {result['sdk_error']}")
    else:
        print(f"\n{result['message']}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Connect to a UFACTORY xArm controller and read status only. "
            "This tool never enables the motors or sends a motion command."
        )
    )
    parser.add_argument("ip", type=controller_ip, help="IP printed on the control-box label")
    parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="connection timeout in seconds (default: 3)",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args()

    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    result: dict[str, Any] = {
        "controller_ip": args.ip,
        "ports": check_ports(args.ip, args.timeout),
    }

    if not any(result["ports"].values()):
        result["connected"] = False
        result["message"] = (
            "No xArm services answered. Confirm the controller is powered on and the "
            "computer's wired IPv4 address is in the same 192.168.1.x subnet."
        )
        exit_code = 2
    else:
        try:
            result["status"] = sdk_read_only_check(args.ip, args.timeout)
            result["connected"] = True
            exit_code = 0
        except Exception as exc:
            result["connected"] = False
            result["sdk_error"] = f"{type(exc).__name__}: {exc}"
            exit_code = 3

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print_human(result)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
