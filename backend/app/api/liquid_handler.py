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
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from drivers import DriverError, InstrumentKind
from drivers.opentrons.driver import MAX_JOG_MM, MAX_PLUNGER_JOG_MM, PLUNGER_AXES

from core import teach_poses

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


# --- taught points -----------------------------------------------------------
# A taught point is only as good as the datum it was recorded against, and on
# this unit the datum does NOT survive a power cycle. Measured 2026-07-25: the
# counters read X=300 before a replug and X=0 after, with the carriage never
# having moved. A point saved in one counter epoch therefore names a different
# physical place in the next one, and nothing on the board reports the change.
#
# So every point carries the datum state it was saved under, and goto refuses
# when that state cannot be reproduced rather than driving to a coordinate whose
# meaning has silently changed. This is the same discipline as the verification
# agents: an unknown state escalates, it does not average out to a pass.
#
# Y is stored but never replayed. G28.2 Y searches for an endstop that never
# reports, so Y has no physical reference at all and a Y coordinate cannot be
# returned to. Vision is what supplies Y's datum -- see
# core/calibration/ot_hand_eye.py -- and until that is fitted, Y is record-only.

class OtDatum(BaseModel):
    homed: bool = Field(..., description="was Z homed in the session that saved this")
    y_referenced: bool = Field(False, description="always False: Y cannot be homed")
    counters: dict[str, float] = Field(default_factory=dict,
                                       description="raw axis counters at save time")


class OtPoint(BaseModel):
    name: str
    # Same {x, y, z} shape core.teach_poses.get reads, so a workflow can look an
    # OT point up by name exactly as it looks up an arm pose.
    pose: dict[str, float] = Field(default_factory=dict)
    datum: OtDatum | None = None
    note: str | None = None
    saved_at: str | None = None


class OtPointRequest(BaseModel):
    name: str
    note: str | None = None


_points_guard = threading.Lock()

REPLAYABLE_AXES = ("X", "Z")


def _is_homed(dev) -> bool:
    return bool(getattr(dev, "_homed", False))


def _positionable(dev):
    """A handler that can report machine coordinates, or a 501 saying it cannot.

    Not every liquid handler is coordinate-addressable, and an AttributeError
    surfacing as a 500 would read like a server fault rather than the capability
    gap it is.
    """
    if not hasattr(dev, "machine_position"):
        raise HTTPException(
            501, f"{dev.device_id} does not report machine coordinates, so points "
                 f"cannot be taught on it")
    return dev


def _points(device_id: str) -> dict[str, dict[str, Any]]:
    return teach_poses.load().get(device_id, {})


@router.get("/{device_id}/points", response_model=list[OtPoint])
def list_points(device_id: str) -> list[OtPoint]:
    _lh(device_id)
    return [OtPoint(**p) for p in _points(device_id).values()]


@router.post("/{device_id}/points", response_model=list[OtPoint])
def save_point(device_id: str, req: OtPointRequest) -> list[OtPoint]:
    """Record where the OT is right now, under a name.

    Saving is always allowed, homed or not: an un-homed point still carries the
    relative geometry of the deck, which is worth keeping. What the datum block
    decides is whether it can ever be replayed.
    """
    dev = _positionable(_lh(device_id))
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "a point needs a name")
    try:
        counters = dev.machine_position()
    except DriverError as e:
        raise HTTPException(503, f"could not read the position counters: {e}")

    entry = OtPoint(
        name=name,
        pose={k.lower(): float(v) for k, v in counters.items() if k in ("X", "Y", "Z")},
        datum=OtDatum(homed=_is_homed(dev), y_referenced=False, counters=counters),
        note=req.note,
        saved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    with _points_guard:
        store = teach_poses.load()
        store.setdefault(device_id, {})[name] = entry.model_dump()
        teach_poses.write(store)
    return [OtPoint(**p) for p in _points(device_id).values()]


@router.delete("/{device_id}/points/{name}", response_model=list[OtPoint])
def delete_point(device_id: str, name: str) -> list[OtPoint]:
    _lh(device_id)
    with _points_guard:
        store = teach_poses.load()
        if store.get(device_id, {}).pop(name, None) is None:
            raise HTTPException(404, f"no taught point {name!r} for {device_id}")
        teach_poses.write(store)
    return [OtPoint(**p) for p in _points(device_id).values()]


@router.post("/{device_id}/points/{name}/goto", response_model=LhActionResult)
def goto_point(device_id: str, name: str) -> LhActionResult:
    """Drive X and Z back to a taught point, if the datum still means the same thing.

    Refuses rather than guessing. The failure this prevents is specific: replay a
    point saved before a power cycle and the machine drives to a coordinate that
    now sits somewhere else entirely, at full speed, with no endstop to stop it.
    """
    dev = _positionable(_lh(device_id))
    saved = _points(device_id).get(name)
    if saved is None:
        raise HTTPException(404, f"no taught point {name!r} for {device_id}")
    entry = OtPoint(**saved)

    if entry.datum is None or not entry.datum.homed:
        raise HTTPException(
            409,
            f"{name!r} was taught without a homed datum, so its coordinates are "
            f"relative to a zero that no longer exists. Re-teach it after homing."
        )
    if not _is_homed(dev):
        raise HTTPException(
            409,
            f"{device_id} has not been homed in this session, so its counters are "
            f"not comparable with the ones {name!r} was saved against. POST "
            f"/api/liquid-handlers/{device_id}/home first."
        )

    target = {a: entry.pose[a.lower()] for a in REPLAYABLE_AXES
              if entry.pose.get(a.lower()) is not None}
    if not target:
        raise HTTPException(400, f"{name!r} has no replayable X or Z coordinate")

    lock = _lock_for(device_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, f"{device_id} is already executing a command")
    try:
        t0 = time.time()
        dev.move_to_machine(**target)
        axes = ", ".join(f"{a}={v:.1f}" for a, v in sorted(target.items()))
        return LhActionResult(
            ok=True,
            detail=f"went to {name!r} ({axes}); Y not replayed, it has no datum",
            duration_s=round(time.time() - t0, 3),
            status=_status(dev),
        )
    except DriverError as e:
        return LhActionResult(ok=False, detail=str(e), status=_status(dev))
    finally:
        lock.release()
