"""Helpers shared by the bench scripts."""
from __future__ import annotations

import os


def resolve_port(configured: str | None = None) -> str:
    """The OT-One serial port, detected rather than hardcoded.

    macOS renumbers `usbmodem` nodes on every re-enumeration, so a literal path
    written into a script goes stale without anything changing about the robot,
    and it fails as a bare "No such file or directory" that reads like the robot
    is unplugged. Every script here used to carry its own copy of one such
    literal, all of them pointing at a node that no longer exists.

    Order: an explicit --port, then OT_SERIAL_PORT, then detection. Returns ""
    when there is no usbmodem node at all, which means the board is off the USB
    bus rather than merely renumbered, and callers should say so rather than
    handing an empty path to pyserial.
    """
    from core.config import _opentrons_port

    return _opentrons_port(configured or os.getenv("OT_SERIAL_PORT") or None) or ""


def require_port(configured: str | None = None) -> str:
    """resolve_port, but exit with an actionable message when nothing is there."""
    port = resolve_port(configured)
    if not port:
        raise SystemExit(
            "no OT serial port: there is no /dev/cu.usbmodem* node, so the board "
            "is off the USB bus rather than merely renumbered. Power it off, "
            "unplug USB, wait 5 s, replug."
        )
    return port
