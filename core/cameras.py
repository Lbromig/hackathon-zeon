"""The bench's physical cameras, identified by keys that survive a replug.

This is the source of truth for *which camera is which viewpoint*. It is code, not
configuration, on purpose — see "Why hardcoded" below.

Identifying a camera on macOS
-----------------------------
Three candidate keys, only two of which are identities:

===================  ==============================================================
key                  verdict
===================  ==============================================================
device index         **NOT an identity.** macOS renumbers video devices on any
                     add/remove/replug, and measured on this bench, between two
                     consecutive enumerations seconds apart with nothing touched:
                     the built-in MacBook camera moved from index 2 to index 0. An
                     index-addressed slot can therefore come up aimed at a different
                     camera — or at the operator's face — with no config change and
                     no error.
device name          Unique for 3 of the 4 units, useless for the other two: both
                     D405 arm cameras report the byte-identical name
                     "Intel(R) RealSense(TM) Depth Camera 405  Depth", so a name
                     binds to whichever of them enumerates first.
AVFoundation         **Identity.** Stable per physical USB port, and its high bytes
uniqueID             are the USB locationID, which ioreg maps to the unit's USB
                     serial number. Needs no root. This is what we bind to.
===================  ==============================================================

``uniqueID`` names a *port*; ``usb_serial`` names the *unit* plugged into it. Both are
recorded: the uniqueID is what opens the device, and the serial is what detects that
someone moved a camera to a different port (the driver warns rather than serving frames
from the wrong viewpoint silently).

Why hardcoded
-------------
These four units are bolted to this bench. The mapping is a physical fact about the cell,
not a per-developer preference, and every time it lived in ``.env`` it drifted: an index
that was correct when written silently came to mean a different camera, including Apple's
own always-present cameras. Putting it in the repo means the mapping is reviewed,
versioned, and identical on every checkout.

Replacing a unit is a code change here — deliberately, since it is also a change to what
the world model is looking at. Get the new values from::

    .venv/bin/python scripts/identify_cameras.py --snapshot

Recorded 2026-07-26 from that script's output.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BenchCamera:
    """One physical camera and the viewpoint it provides."""

    slot: str            # fleet id — the vocabulary the twin, API and UI share
    unique_id: str       # AVFoundation uniqueID: what actually opens the device
    usb_serial: str      # the unit itself; survives being moved to another port
    model: str           # D435 / D435i / D405
    node: str            # which UVC function macOS exposes: "RGB" or "IR/depth"
    name: str            # human label for the UI
    note: str            # what this viewpoint is for, and what its frames look like


# Ordered as the workflow thinks about them: the two fixed viewpoints, then the arms.
BENCH_CAMERAS: tuple[BenchCamera, ...] = (
    BenchCamera(
        slot="overview_cam",
        unique_id="0x124100080860b3a",
        usb_serial="250343061180",
        model="D435i",
        node="IR/depth",
        name="Overview cam (both devices)",
        # The only mono unit: macOS exposes this one's *Depth* function, so frames are
        # infrared, three identical channels, with the projector's dot pattern
        # superimposed on every surface. Tags are still detectable; the dots are not dirt.
        note="wide view of both arms and the bench; mono IR with visible projector dots",
    ),
    BenchCamera(
        slot="handover_cam",
        unique_id="0x124200080860b07",
        usb_serial="125123020017",
        model="D435",
        node="RGB",
        name="Handover cam (arm->OT)",
        # The only true-colour unit on the bench, and the only one whose macOS-exposed
        # function is the RGB module. Delivers 1920x1080 even when 1280x720 is requested.
        note="Opentrons deck head-on, incl. the deck tag strips; the only colour feed",
    ),
    BenchCamera(
        slot="gripper_cam",
        unique_id="0x124300080860b5b",
        usb_serial="351623070085",
        model="D405",
        node="IR/depth",
        name="Gripper cam (right arm)",
        note="eye-in-hand on the RIGHT arm; gripper fingers visible at frame bottom",
    ),
    BenchCamera(
        slot="gripper_left_cam",
        unique_id="0x124400080860b5b",
        usb_serial="351623070042",
        model="D405",
        node="IR/depth",
        name="Gripper cam (left arm)",
        note="eye-in-hand on the LEFT arm",
    ),
)

BY_SLOT: dict[str, BenchCamera] = {cam.slot: cam for cam in BENCH_CAMERAS}


def fleet_entries(width: int = 1280, height: int = 720) -> list[dict]:
    """The camera half of the fleet, as driver config dicts.

    The D435's RGB module ignores a 1280x720 request and delivers 1920x1080 anyway; that
    is the device's business, and the driver reports whatever arrives rather than pretending.
    """
    return [
        {
            "type": "avf",
            "id": cam.slot,
            "name": cam.name,
            "unique_id": cam.unique_id,
            "usb_serial": cam.usb_serial,
            "model": cam.model,
            "width": width,
            "height": height,
        }
        for cam in BENCH_CAMERAS
    ]
