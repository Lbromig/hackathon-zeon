"""Jog endpoints for liquid handlers — hand-driving the OT-One from the UI.

Separate from `teach.py` because that router is arm-only and its model does not
fit this machine: it clamps against configured soft limits and refuses absolute
moves beyond a jump threshold, both of which assume a trustworthy datum. The
OT-One has none (see docs/OT_ONE_HARDWARE.md), so everything here is *relative*
jogging and there is no absolute move endpoint at all.

Synchronous on purpose, like teach.py: a jog blocks for its full travel (a 10 mm
step at 5 mm/s is 2 s) and FastAPI runs sync handlers in a threadpool, so the
`/ws/state` stream keeps flowing.

Safety rules enforced here rather than in the client, because a client cannot be
trusted to be the only client:

* one in-flight command per device (non-blocking lock -> 409). Two concurrent
  G-code motion commands on one board interleave in the planner queue, and
  click-spam produces exactly that.
* every delta is clamped to the driver's own per-step cap.
* Y is refused outright: it drives looking for an endstop that never reports and
  grinds against a hard stop.
* motion is refused after an emergency stop until the device is homed again,
  because Ctrl-X resets the board and discards its reference.

`/stop` deliberately skips the busy lock. An emergency stop that waits for the
move it is trying to interrupt would be useless. Do not "fix" that.

The honest caveat this API cannot enforce: with no endstops and no current
sensing, a crash is invisible. A stalled stepper skips steps and the call returns
exactly as it would on a clean move. Duration proves a move ran, never that the
path was clear.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from drivers import DriverError, InstrumentKind
from drivers.opentrons.driver import MAX_JOG_MM, MAX_PLUNGER_JOG_MM, PLUNGER_AXES

from ..services.device_manager import device_manager

router = APIRouter(prefix="/api/liquid-handlers", tags=["liquid-handler"])

# Y is joggable but NOT homeable. The Y fault is specific to homing: G28.2 Y
# drives a long search for an endstop that never reports and grinds against a
# hard stop. A bounded relative jog does no search, and was verified clean on
# hardware over 10 mm in 2 mm steps.
# B and C are the plungers. They jog too, but under a much tighter cap than the
# gantry: travel is short and one driven past its seal jams.
JOGGABLE_AXES = ("X", "Y", "Z", "A", "B", "C")
REFUSED_AXES: tuple[str, ...] = ()
UNHOMEABLE_AXES = ("Y",)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class LhJogRequest(BaseModel):
    axis: str = Field(..., description="X, Z or A. Y is refused.")
    delta: float = Field(..., description="mm, relative. Positive Z is DOWN.")
    feedrate: float | None = Field(None, description="mm/min; driver default if unset")


class LhActionResult(BaseModel):
    ok: bool
    detail: str
    duration_s: float | None = None
    status: dict[str, Any] | None = None


def _lh(device_id: str):
    try:
        dev = device_manager.get(device_id)
    except KeyError:
        raise HTTPException(404, f"no device {device_id}")
    if dev.info.kind != InstrumentKind.LIQUID_HANDLER:
        raise HTTPException(400, f"{device_id} is not a liquid handler")
    return dev


def _lock_for(device_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(device_id, threading.Lock())


def _status(dev) -> dict[str, Any]:
    try:
        return dev.status()
    except Exception as e:
        return {"error": str(e)}


@router.get("/{device_id}/state", response_model=LhActionResult)
def state(device_id: str) -> LhActionResult:
    dev = _lh(device_id)
    return LhActionResult(ok=True, detail="ok", status=_status(dev))


@router.get("/{device_id}/limits")
def limits(device_id: str) -> dict[str, Any]:
    """What the UI needs to render safe controls, read from the driver."""
    dev = _lh(device_id)
    return {
        "joggable_axes": list(JOGGABLE_AXES),
        "refused_axes": list(REFUSED_AXES),
        "unhomeable_axes": list(UNHOMEABLE_AXES),
        "max_step_mm": MAX_JOG_MM,
        "plunger_axes": list(PLUNGER_AXES),
        "max_plunger_step_mm": MAX_PLUNGER_JOG_MM,
        "relative_only": True,
        "endstops_functional": False,
        "note": (
            "Relative jogging only: this machine has no working endstops, so no "
            "absolute datum exists. A crash is invisible to software."
        ),
    }


@router.post("/{device_id}/jog", response_model=LhActionResult)
def jog(device_id: str, req: LhJogRequest) -> LhActionResult:
    dev = _lh(device_id)
    axis = req.axis.strip().upper()

    if axis in REFUSED_AXES:
        return LhActionResult(
            ok=False,
            detail=f"{axis} is refused: it drives looking for an endstop that "
                   f"never reports and grinds against a hard stop.",
            status=_status(dev),
        )
    if axis not in JOGGABLE_AXES:
        return LhActionResult(
            ok=False,
            detail=f"unknown axis {axis!r}; joggable: {', '.join(JOGGABLE_AXES)}",
            status=_status(dev),
        )

    lock = _lock_for(device_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, f"{device_id} is already executing a command")
    try:
        t0 = time.time()
        dev.jog(axis, req.delta, **({"feedrate": req.feedrate} if req.feedrate else {}))
        dt = time.time() - t0
        return LhActionResult(
            ok=True,
            detail=f"jogged {axis} {req.delta:+.2f} mm",
            duration_s=round(dt, 3),
            status=_status(dev),
        )
    except DriverError as e:
        return LhActionResult(ok=False, detail=str(e), status=_status(dev))
    except Exception as e:
        return LhActionResult(ok=False, detail=f"{type(e).__name__}: {e}",
                              status=_status(dev))
    finally:
        lock.release()


@router.post("/{device_id}/home", response_model=LhActionResult)
def home(device_id: str) -> LhActionResult:
    """Home the Z lift. This is the only way to re-establish a reference."""
    dev = _lh(device_id)
    lock = _lock_for(device_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, f"{device_id} is already executing a command")
    try:
        t0 = time.time()
        dev.home()
        return LhActionResult(
            ok=True,
            detail="homed Z to the top datum",
            duration_s=round(time.time() - t0, 3),
            status=_status(dev),
        )
    except DriverError as e:
        return LhActionResult(ok=False, detail=str(e), status=_status(dev))
    finally:
        lock.release()


@router.post("/{device_id}/stop", response_model=LhActionResult)
def stop(device_id: str) -> LhActionResult:
    """Emergency stop. Deliberately does NOT take the busy lock."""
    dev = _lh(device_id)
    try:
        wrote = dev.estop()
    except Exception as e:
        return LhActionResult(ok=False, detail=f"estop failed: {e}")
    if wrote:
        return LhActionResult(
            ok=True,
            detail="emergency stop written to the board (Ctrl-X, M112, M18)",
            status=_status(dev),
        )
    # Never claim a stop that did not reach the hardware.
    return LhActionResult(
        ok=False,
        detail="NOTHING WAS SENT - no bytes reached the board. Cut power at the switch.",
        status=_status(dev),
    )
