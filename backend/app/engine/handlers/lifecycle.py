"""Lifecycle action handlers: initialize and reconnect (R-INIT-1/2/7, R-ARM-7).

Two handlers, and both exist because D5 made initialization *a plan run on this engine*.
That is what buys R-INIT-5 (initialization is an indexed, observable plan) and R-INIT-7
("reinitialize device X" is literally the boot code with ``device`` set) without a second
imperative code path that can drift from the first.

The rule that shapes `initialize`
---------------------------------
**One device failing must not stop the others** (R-INIT-1). So every per-device step is
wrapped, the outcome is recorded either way, and the loop continues. That is the one place
in this slice where a handler catches an exception — not to hide it, but because the
per-device outcome *is* this action's output, and a single unreachable camera must not
prevent both arms from being enabled. Anything genuinely fatal (a broken contract, an abort)
still propagates.

Homing is the same shape, one step further: an arm with **no taught HOME** produces a
warning and is skipped (R-INIT-4). Not an error, and above all not a guessed home — the two
arms share a table and "roughly home" is a collision. `home_missing` in the outputs is what
carries that to the readiness panel, paired with a ``home_not_defined`` warning so the
frontend can match on a code rather than on message text (R-LOG-6).
"""
from __future__ import annotations

import os
from typing import Any

from core import speeds as speed_tiers
from core import waypoints
from core.config import settings
from drivers.base import InstrumentKind

from ..actions import Initialize, InitializeOutputs, Reconnect, ReconnectOutputs, handler
from ..context import ActionContext
from .arm import move_to_waypoint

#: Frames captured and thrown away before the one that is kept (R-INIT-2). The first frames
#: off a UVC camera are auto-exposure and auto-white-balance settling, so "the first frame"
#: is reliably the worst frame the camera will ever produce — and it is the one a detector
#: would be handed if this were zero.
CAMERA_SETTLE_FRAMES = 3

#: R-INIT-3: the home move runs at the `slow` tier, resolved per arm. Not the action's own
#: tier — `lifecycle.initialize` has no business running a home move fast because somebody
#: set the action to `fast`. Slow enough to watch an unexpected trajectory and reach e-stop.
HOME_TIER = "slow"


def _configured_device_ids(ctx: ActionContext) -> list[str]:
    """The devices this action covers: the named one, or every configured device.

    "Configured" comes from the fleet config, not from the set of drivers that happened to
    build successfully — R-INIT-1 says initialization *discovers* the configured devices and
    records a per-device outcome, and a device whose driver failed to construct is exactly
    the outcome worth recording. Reading it from the manager instead would make that device
    vanish from the report.
    """
    if ctx.action.device:
        return [ctx.action.device]
    return [str(entry["id"]) for entry in settings.fleet if entry.get("id")]


def _kind_of(driver: Any) -> InstrumentKind | None:
    """The device's capability class. ``kind`` is a class attribute on every capability ABC;
    ``info`` is the fallback, and is allowed to fail — an unclassifiable device is skipped
    with a warning rather than crashing the whole initialization."""
    kind = getattr(driver, "kind", None)
    if isinstance(kind, InstrumentKind):
        return kind
    try:
        return driver.info.kind
    except Exception:
        return None


# --- lifecycle.initialize ----------------------------------------------------------

def _init_arm(driver: Any, ctx: ActionContext) -> str:
    """Enable all axes and clear latched faults (R-INIT-2).

    Clear *then* enable, which is the opposite order from the requirement's prose: on an
    xArm a latched fault refuses the enable, so enabling first would fail on precisely the
    arm that needed initializing. It is the same order `reconnect(scope="engage")` uses, and
    the requirement lists the two as a set rather than a sequence.
    """
    driver.clear_errors()
    driver.enable(True)
    return "faults cleared, all axes enabled"


def _init_camera(driver: Any, ctx: ActionContext) -> str:
    """Capture and store one frame, discarding the settle frames (R-INIT-2)."""
    for _ in range(CAMERA_SETTLE_FRAMES):
        driver.capture()          # discarded: auto-exposure is still settling
    frame = driver.capture()
    detail = f"captured a frame after {CAMERA_SETTLE_FRAMES} settle frames"

    # Store it as an artifact when the run has somewhere to put it. JPEG off the driver
    # rather than an encode here: the bytes are what the UI serves, and it keeps this
    # handler free of an image library. A camera with no JPEG path still counts as
    # initialized — capturing is the check, storing is the record.
    if not ctx.artifact_dir:
        return f"{detail} (not stored: this run has no artifact directory)"
    try:
        data = driver.capture_jpeg()
        os.makedirs(ctx.artifact_dir, exist_ok=True)
        name = f"init_{driver.device_id}.jpg"
        path = os.path.join(ctx.artifact_dir, name)
        with open(path, "wb") as f:
            f.write(data)
        ctx.artifact("image/frame", path, camera=driver.device_id, label="initialize")
    except Exception as e:
        ctx.warn("init_frame_not_stored",
                 f"{driver.device_id} captured a frame but it could not be stored: {e}",
                 device=driver.device_id)
        return f"{detail} (not stored: {e})"
    shape = getattr(frame, "shape", None)
    return f"{detail}, stored{f' {shape[1]}x{shape[0]}' if shape and len(shape) >= 2 else ''}"


def _init_liquid_handler(driver: Any, ctx: ActionContext) -> str:
    """Delegate to the driver's own ``initialize`` if the capability exposes one.

    R-INIT-2 wants the gantry wiggled on each axis and Z retracted fully. That routine knows
    the envelope, the axis names and the retract height, so it belongs on the driver (S4,
    D28) — and it is additive there, so it may not exist yet. Missing is a **warning and a
    skip**, never a silent pass: an initialization that reports success while the pipette
    head was never moved is exactly the failure D6 was written about.
    """
    routine = getattr(driver, "initialize", None)
    if not callable(routine):
        ctx.warn("lh_initialize_unavailable",
                 f"{driver.device_id} has no `initialize` on its driver, so the axis wiggle "
                 f"and Z retract of R-INIT-2 did not run. Connected but not initialized.",
                 device=driver.device_id)
        return "connected; no driver initialize routine to run (skipped)"
    routine()
    return "driver initialize routine ran (axis wiggle, Z retracted)"


_ROUTINES = {
    InstrumentKind.ARM: _init_arm,
    InstrumentKind.CAMERA: _init_camera,
    InstrumentKind.LIQUID_HANDLER: _init_liquid_handler,
}


def _initialize_one(device_id: str, ctx: ActionContext) -> dict[str, Any]:
    """Connect and run the per-device routine, returning the outcome either way.

    The shape is `InitializeOutputs.devices`' contract: ``{device, connected, detail,
    simulated}``. `simulated` comes from the resolved configuration through
    ``ctx.devices.is_simulated`` (D25) and never from a driver's vendor string.
    """
    outcome: dict[str, Any] = {
        "device": device_id, "connected": False, "detail": "",
        "simulated": _simulated(ctx, device_id),
    }
    try:
        driver = ctx.devices.get(device_id)
    except Exception as e:
        outcome["detail"] = f"no driver: {e}"
        ctx.warn("device_unavailable",
                 f"{device_id} is configured but has no driver ({e}) — it cannot be "
                 f"initialized. The other devices continue.", device=device_id)
        return outcome

    try:
        driver.connect()
        outcome["connected"] = True
    except Exception as e:
        outcome["detail"] = f"connect failed: {e}"
        ctx.warn("device_init_failed", f"{device_id} failed to connect: {e}",
                 device=device_id)
        return outcome

    kind = _kind_of(driver)
    routine = _ROUTINES.get(kind) if kind else None
    if routine is None:
        outcome["detail"] = f"connected; no init routine for kind {kind!r}"
        ctx.warn("device_kind_unknown",
                 f"{device_id} connected but its capability kind ({kind!r}) has no "
                 f"initialization routine — nothing device-specific ran.", device=device_id)
        return outcome

    try:
        outcome["detail"] = routine(driver, ctx)
    except Exception as e:
        # Connected but not initialized: both facts are recorded, because "connected" alone
        # would read as ready.
        outcome["detail"] = f"connected, but init failed: {type(e).__name__}: {e}"
        ctx.warn("device_init_failed",
                 f"{device_id} connected but its initialization failed: {e}",
                 device=device_id)
    return outcome


def _simulated(ctx: ActionContext, device_id: str) -> bool:
    try:
        return bool(ctx.devices.is_simulated(device_id))
    except Exception:
        # A device the manager does not know cannot be asked; the run's own reality is the
        # honest answer, and it is the conservative one in a simulated run (D25/R-SIM-6).
        return bool(ctx.simulated)


def _home_arms(device_ids: list[str], ctx: ActionContext,
               homed: list[str], home_missing: list[str]) -> None:
    """Move each arm to its taught HOME, slowly (R-INIT-3), or warn and skip (R-INIT-4)."""
    for device_id in device_ids:
        ctx.checkpoint()
        try:
            arm = ctx.devices.require_arm(device_id)
        except Exception:
            continue                     # not an arm, or unavailable — already reported
        if _kind_of(arm) is not InstrumentKind.ARM:
            continue

        try:
            resolved = waypoints.resolve(device_id, waypoints.HOME)
        except waypoints.WaypointError as e:
            # Both refusals land here and both mean the same operationally: this arm has no
            # home it is allowed to move to. Warn, skip, do not fail (R-INIT-4).
            home_missing.append(device_id)
            ctx.warn("home_not_defined",
                     f"{device_id} has no taught {waypoints.HOME!r} — skipping its home "
                     f"move. Nothing is guessed: a guessed home on a shared table is a "
                     f"collision. Teach it in the teach tab. ({e})", device=device_id)
            continue

        tier = speed_tiers.for_arm(HOME_TIER, getattr(arm, "limits", None))
        try:
            path = move_to_waypoint(arm, resolved,
                                    joint_speed=tier.angular, linear_speed=tier.linear)
        except Exception as e:
            # A home that is taught but unreachable is a different problem from one that is
            # not taught, so it is not reported as `home_missing`. Still not fatal: the
            # other arm's home move must still run (R-INIT-1's rule, one level down).
            ctx.warn("home_move_failed",
                     f"{device_id} has a taught {waypoints.HOME!r} but the move to it "
                     f"failed: {e}", device=device_id)
            continue
        homed.append(device_id)
        ctx.progress(f"{device_id} at {waypoints.HOME} ({tier.tier}, {path})",
                     waypoint=waypoints.HOME, motion_path=path)


@handler("lifecycle.initialize")
def initialize(action: Initialize, ctx: ActionContext) -> InitializeOutputs:
    """Initialize every configured device, or one named device (R-INIT-1/2/7).

    ``device=None`` means all of them. Per-device: connect, then the routine for its
    capability — arms enable and clear faults, cameras capture and store a frame after
    discarding the settle frames, the liquid handler delegates to its own routine. Then, if
    ``home_after``, each arm moves to its taught HOME at the `slow` tier.

    Never raises for a device-level failure; that is the whole point (R-INIT-1). The
    outcomes are in ``devices``, the reasons are in the action's warnings, and the readiness
    state that R-INIT-6 derives from them is the caller's to compute.
    """
    device_ids = _configured_device_ids(ctx)
    devices: list[dict[str, Any]] = []
    for device_id in device_ids:
        ctx.checkpoint()               # per device, so a long fleet is pausable
        outcome = _initialize_one(device_id, ctx)
        devices.append(outcome)
        ctx.progress(f"{device_id}: {outcome['detail'] or 'initialized'}",
                     initialized=outcome["connected"])

    homed: list[str] = []
    home_missing: list[str] = []
    if action.home_after:
        ready = [o["device"] for o in devices if o["connected"]]
        _home_arms(ready, ctx, homed, home_missing)

    return InitializeOutputs(devices=devices, homed=homed, home_missing=home_missing)


# --- lifecycle.reconnect -----------------------------------------------------------

def _latched_faults(arm: Any) -> list[str]:
    """The fault/warning codes about to be cleared, read before clearing them.

    Reported in `ReconnectOutputs.cleared_errors` so the record says *what* was wrong. An
    unreadable status is itself worth recording — "cleared nothing" and "could not tell"
    are different claims.
    """
    try:
        status = arm.status() or {}
    except Exception as e:
        return [f"status unreadable: {e}"]
    out = []
    for key in ("error_code", "warn_code"):
        value = status.get(key)
        if value:
            out.append(f"{key}={value}")
    return out


@handler("lifecycle.reconnect")
def reconnect(action: Reconnect, ctx: ActionContext) -> ReconnectOutputs:
    """Re-connect, re-enable, or re-engage an arm (R-ARM-7).

    The scopes are cumulative, because on an xArm these are calls in a fixed order and doing
    one without the ones before it leaves the arm in a state nothing else expects:

    * ``connect`` — open the connection.
    * ``enable`` — connect, then enable all axes.
    * ``engage`` — connect, ``clear_errors()``, ``enable(True)``, then **verify** by
      commanding a zero-distance move (Q5). "Enabled" and "actually accepts a move" are not
      the same claim, which is why `verified` is a separate field: a latched fault or a
      controller still in the wrong mode reports enabled and then refuses the first real
      motion, somewhere less convenient.

    A verification that fails raises, with what did succeed in the message — the runner turns
    that into a failed action, and an engage that cannot move the arm has not recovered it.
    """
    driver = ctx.devices.get(action.device)
    out = ReconnectOutputs(scope=action.scope)

    driver.connect()
    out.connected = True
    ctx.progress(f"{action.device}: connected", scope=action.scope)
    if action.scope == "connect":
        return out

    arm = ctx.devices.require_arm(action.device)

    if action.scope == "engage":
        out.cleared_errors = _latched_faults(arm)
        arm.clear_errors()
        ctx.progress(f"{action.device}: cleared {out.cleared_errors or 'no latched faults'}",
                     scope=action.scope)

    arm.enable(True)
    out.enabled = True
    ctx.progress(f"{action.device}: enabled", scope=action.scope)
    if action.scope == "enable":
        return out

    # The verification. A zero-distance move is the smallest command that still goes all the
    # way through the controller's accept path, so it distinguishes "enabled" from "will
    # move" without moving anything.
    tier = speed_tiers.for_arm(HOME_TIER, getattr(arm, "limits", None))
    try:
        arm.move_relative(dx=0.0, dy=0.0, dz=0.0, speed=tier.linear, wait=True)
    except Exception as e:
        raise RuntimeError(
            f"{action.device} connected, faults cleared "
            f"({', '.join(out.cleared_errors) or 'none latched'}) and enabled, but it "
            f"refused a zero-distance move: {e}. It is not engaged — do not start a run."
        ) from e
    out.verified = True
    ctx.progress(f"{action.device}: verified with a zero-distance move", scope=action.scope)
    return out


__all__ = ["initialize", "reconnect", "CAMERA_SETTLE_FRAMES", "HOME_TIER"]
