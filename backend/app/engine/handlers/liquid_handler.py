"""Liquid-handler action handler: `lh.move_relative` (R-LH-1, S4/W2).

One plain blocking function, called as ``fn(action, ctx)``, returning ``LHMoveOutputs``. It
does not build an ``ActionResult``, emit events, catch its own errors or decide retries — the
runner does all of that, so a failure here is a raise and nothing else. It never raises
``ActionAborted`` by hand either: inside a loop that does *not* abort the run (review
non-blocking 2), so cooperative control goes through ``ctx.checkpoint()`` only.

.. _lh-sign-convention:

The sign convention — stated here because W3 codes against it
-------------------------------------------------------------
``dx``/``dy``/``dz`` here are in the **task frame**, and so is every millimetre on the servo
path: `OffsetOutputs.residual_offset_mm`, the blackboard's ``selected_offset``,
`core.sim.world`'s shared offset, and this action's ``requested_mm`` / ``applied_mm`` /
``position_before`` / ``position_after``.

1. **The offset is ``tube − tip``** — the displacement the pipette tip must still travel to
   reach the tube mouth. All three components zero means aligned. This is exactly what
   `core.perception.offset` solves on its primary path (``t_tube - t_tip``), so the solved
   offset and the commanded move are *the same quantity in the same direction*.
2. **Therefore command the offset; never negate it.** ``dx=offset.x, dy=offset.y, dz=offset.z``
   closes it. There is no minus sign anywhere on this path. If you are writing one, the bug is
   somewhere else.
3. **``z`` is positive UP**, away from the deck. A positive commanded ``dz`` raises the pipette
   head; the plan's closing ``dz=+40`` is a retract. A positive ``offset["z"]`` therefore means
   the tube mouth is *above* the tip.

The OT-One's gantry ``Z`` runs the other way — measured on the bench, ``G0 Z+6`` lowered the
head and ``G0 Z+5`` *released* ``min_z``, so raw ``Z+`` is down and ``min_z`` sits at the top of
travel. That flip lives in exactly one place, `OpentronsDriver.move_relative`, and never above
the driver. The same convention is stated in `drivers/capabilities/liquid_handler.py` and
`core/sim/world.py` — deliberately three times, because on Z a sign error drives the pipette
*into* the deck instead of away from it, and an unstated convention is how that gets
reintroduced.

The three things this handler must never do
-------------------------------------------
* **Move on a number the solve would not stand behind.** A ``method="refused"`` offset is not a
  broken step — it is a rig that currently cannot see (D17/D18) — so this reports it, commands
  nothing, and lets the loop's own no-progress rule end the run as ``stalled``. Raising would
  grade the loop "a step inside the loop failed", which reads as a broken engine.
* **Silently clamp.** A vision-derived step larger than ``clamp_mm`` is a detection failure
  wearing a command's clothes (O9/R-VIS-7), and `LHRelative.clamp_mm`'s own frozen contract says
  it is *refused rather than executed*. Executing a shortened version would hide the bad solve
  **and** move the head. So the row fails with the magnitude in the reason, and ``clamp_mm``
  exceedance never produces a partial move. Which means: ``requested_mm`` and ``applied_mm``
  differ only for reasons that are separately warned about — an axis the solve could not observe
  (``unobserved_axes``), a refused solve (``offset_refused``), or an axis that did not follow
  (``move_shortfall``). That is what the tab's "clamped" indicator renders from, and because
  every such case carries a warning, a shortened move can never look like a converging loop.
* **Report a dead-reckoned position as a reading.** The OT-One has no encoders: ``M114``'s
  "actual" values are the controller's own step counts, so a stalled axis counts as moved
  (measured — a 10 mm retract into the top switch counted 10 mm while the camera saw ~2 mm).
  ``provenance`` says which it is, and R-LH-4 exists because the loop's gain estimate is only as
  good as that distinction.

``None`` and ``0.0`` mean opposite things here. An axis the solve could not observe arrives as
``None`` and is **not moved**; treating it as "already aligned" is how a loop converges on an
axis it never measured (B9).

No speed tier reaches the driver. `LHRelative.speed` exists and would resolve through
``ctx.lh_speeds()`` like any other motion action, but the liquid-handler capability takes no
speed argument and ``LHMoveOutputs`` has no field to record one — the OT-One's gantry feed is
fixed at a deliberately gentle rate inside its driver. Resolving a tier and then discarding it
would be worse than not resolving one, so this does not.
"""
from __future__ import annotations

import math
from typing import Any

from drivers.capabilities.liquid_handler import MoveLimits, RelativeMoveReport

from ..actions import LHMoveOutputs, LHRelative, handler
from ..context import ActionContext
from . import camera as camera_handler

#: Axis order for reporting. Matches `OffsetOutputs.residual_offset_mm`'s keys and
#: `core.sim.world.AXES`.
AXES: tuple[str, ...] = ("x", "y", "z")

#: Below this, a commanded delta is not worth a wire command. Not a convergence tolerance —
#: that is the loop's `threshold_mm` — just the point where "move by 0.0001 mm" is noise.
MIN_STEP_MM = 1e-4


def _finite(name: str, value: float) -> float:
    """Refuse a non-finite delta. Every ``value > limit`` test is False against NaN, so a
    naive clamp check waves it straight through onto the hardware."""
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return float(value)


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` off an outputs model *or* its dumped dict — both shapes reach a slot."""
    got = getattr(value, name, None)
    if got is None and isinstance(value, dict):
        got = value.get(name)
    return default if got is None else got


def _deltas_from_slot(action: LHRelative,
                      ctx: ActionContext) -> tuple[dict[str, float], list[str], str]:
    """(deltas, skipped_axes, refusal) read out of the blackboard slot the action names.

    The slot holds an ``OffsetOutputs``-shaped value — the model or its dumped dict, since both
    reach a slot depending on who wrote it. There is no dotted-path resolver by design (review
    S2): the handler reads the fields in Python, where a typo is an error at development time
    rather than a ``None`` at runtime. Only the axes the solve actually observed are moved; the
    rest are reported as skipped rather than sent as zeros.

    A non-empty ``refusal`` means the *solve* declined. That is an outcome, not an error, so it
    comes back as a value. A slot holding the wrong *type* is a different thing entirely and does
    raise: that is a wiring bug, and a wiring bug that degraded to "moved nothing" would be
    invisible in a loop whose job is to move nothing once it has converged.
    """
    value = ctx.blackboard.get(action.from_slot)

    if _field(value, "method") == "refused":
        # A refusal moves nothing, but it is NOT a failed step. R-VIS-7's honest outcomes are
        # converged / stalled / aborted, and a refused solve is the road to *stalled*: the loop
        # keeps looking, makes no progress, and `no_progress_abort` ends it as stalled. Raising
        # here instead would end the loop as "a step inside the loop failed", which reads as a
        # broken engine rather than as a rig that cannot currently see.
        reason = _field(value, "refusal", "") or "no reason given"
        return {}, list(AXES), f"solve refused ({reason})"

    residual = _field(value, "residual_offset_mm")
    if not isinstance(residual, dict):
        raise ValueError(
            f"slot {action.from_slot!r} does not hold a solved offset "
            f"(got {type(value).__name__}); the solve must run before the nudge")

    deltas: dict[str, float] = {}
    skipped: list[str] = []
    for axis in AXES:
        raw = residual.get(axis)
        if raw is None:
            skipped.append(axis)
            continue
        deltas[axis] = _finite(f"{action.from_slot}.{axis}", float(raw))

    if not deltas:
        # No axis observable, and the solve did not say so. Refused rather than executed as a
        # zero move that would read as success: `Runner._watch` would then score an iteration
        # nobody measured as "no progress" against a number nobody produced.
        raise ValueError(
            f"slot {action.from_slot!r} reports no observable axis and did not refuse, so there "
            f"is no move to make and no stated reason for that. Slot value: {value!r}")
    return deltas, skipped, ""


def _limits(lh: Any) -> MoveLimits:
    """The driver's stated bounds, or none stated. Never a guess: empty limits mean "this driver
    states no bounds", which is a reason for the caller's own, not a claim of safety."""
    getter = getattr(lh, "move_limits", None)
    if not callable(getter):
        return MoveLimits()
    try:
        got = getter()
    except Exception:                                        # noqa: BLE001 - a driver that
        return MoveLimits()                                  # cannot state limits states none
    return got if isinstance(got, MoveLimits) else MoveLimits()


def _preflight(lh: Any, deltas: dict[str, float], before: dict[str, float]) -> None:
    """Refuse an out-of-envelope move **before** any axis moves (R-LH-3).

    Checked here as well as in the driver, for the same reason `arm._check_width` is: it names
    the offending axis and the bound in the refusal, and it holds for a driver that does not
    enforce its own limits. Both answers are refusals, so the two cannot disagree in the
    dangerous direction — and the refusal is the point. Clamping an out-of-envelope request turns
    a unit-conversion bug into a crash into the deck: micrometres passed as millimetres would be
    quietly executed as the largest legal descent, and the record would say it succeeded.

    Every axis is checked before anything is raised, so one refusal carries all the problems
    rather than only the first — otherwise correcting one number surfaces the next one on the
    next run, with the head somewhere else.
    """
    limits = _limits(lh)
    reasons = [r for r in (limits.refusal_for(axis, delta, before or None)
                           for axis, delta in deltas.items()) if r]
    if reasons:
        raise ValueError("; ".join(reasons))


def _command(lh: Any, deltas: dict[str, float]) -> RelativeMoveReport:
    """Ask the driver to move, normalising whatever comes back into a `RelativeMoveReport`.

    Two call shapes, preferred in this order:

    * ``move_relative(dx, dy, dz) -> RelativeMoveReport`` — the capability method added in
      Phase 5 (D28). **Task frame, +z up, by contract.**
    * ``move_by(dx, dy, dz) -> dict`` — the Opentrons driver's older, gantry-frame entry point,
      kept working so a driver or stub written against it is not silently broken. Only reached
      when ``move_relative`` is absent, which is why the frame difference is not a hazard here:
      the one driver whose ``move_by`` speaks a different frame is also the one that provides
      ``move_relative``, so the fallback never sees it.

    A driver with neither raises. What must never happen is the third option — degrading to "did
    nothing, reported success", which is the exact failure R-LH-5 was written about.
    """
    kwargs = {f"d{axis}": deltas.get(axis, 0.0) for axis in AXES}

    relative = getattr(lh, "move_relative", None)
    if callable(relative):
        report = relative(**kwargs)
        if isinstance(report, RelativeMoveReport):
            return report
        return _report_from_dict(report if isinstance(report, dict) else {}, deltas, lh)

    move_by = getattr(lh, "move_by", None)
    if callable(move_by):
        return _report_from_dict(move_by(**kwargs) or {}, deltas, lh)

    raise ValueError(
        f"{getattr(lh, 'device_id', 'this liquid handler')!r} has no relative XYZ motion "
        f"(`move_relative`), so the servo loop cannot drive it")


def _report_from_dict(raw: dict[str, Any], deltas: dict[str, float],
                      lh: Any) -> RelativeMoveReport:
    """Adapt the legacy dict shape: ``achieved`` / ``drift_mm``, upper-case axis keys.

    The legacy shape carries no provenance, so it is filled from the driver's own ``position()``
    rather than left at the dataclass default. Defaulting would silently downgrade a driver with
    real referenced feedback to ``dead_reckoned`` — the safe direction, but a lie in the other
    direction, and R-LH-4 is about the report matching the machine.
    """
    applied = {str(a).lower(): float(v) for a, v in (raw.get("achieved") or {}).items()}
    drift = {str(a).lower(): float(v) for a, v in (raw.get("drift_mm") or {}).items()}
    return RelativeMoveReport(
        requested_mm=dict(deltas), applied_mm=applied, drift_mm=drift,
        moved=bool(raw.get("moved", bool(applied))),
        provenance=_provenance(lh),                          # type: ignore[arg-type]
    )


def _numbers(raw: Any) -> dict[str, float]:
    """Lower-cased finite floats out of a position-ish mapping, dropping anything else.

    Non-finite values are dropped rather than passed on: ``OutputsBase`` sets
    ``allow_inf_nan=False``, so one ``inf`` in ``position_after`` would fail the row's own
    validation — and if it reached the wire, Python's ``json`` emits a bare ``Infinity`` that
    ``JSON.parse`` rejects, breaking the event stream for every connected client.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in raw.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out[str(key).lower()] = number
    return out


def _position(lh: Any) -> dict[str, float]:
    """Actual (as opposed to commanded) position, lower-cased, mm. Empty when unavailable.

    Reporting must never fail a move that already happened, so an unreadable position is an empty
    dict rather than an exception — and empty means *unknown*, which a renderer must not draw as
    the origin.
    """
    try:
        pos = lh.position()
    except Exception:                                        # noqa: BLE001 - report, not raise
        return {}
    return _numbers(pos.get("actual") if isinstance(pos, dict) else None)


def _provenance(lh: Any) -> str:
    """`measured` only if the driver claims a referenced position from real feedback.

    Defaults to `dead_reckoned`, which is the honest answer for this machine and the safe answer
    for any driver that does not say — R-LH-4 is about not mistaking an assumption for a reading,
    so silence must not read as "measured".
    """
    try:
        prov = lh.position().get("provenance") or {}
    except Exception:                                        # noqa: BLE001
        return "dead_reckoned"
    if not isinstance(prov, dict):
        return "dead_reckoned"
    if prov.get("encoders") and prov.get("referenced"):
        return "measured"
    return "dead_reckoned"


def _drift(report: RelativeMoveReport, commanded: dict[str, float]) -> float | None:
    """Worst per-axis shortfall in mm, or ``None`` when it cannot be computed.

    ``None`` rather than ``0.0`` for a driver that reports no achieved motion: "every axis
    followed" and "nobody measured whether they followed" are different claims, and
    ``LHMoveOutputs.drift_mm`` is optional precisely so the second one is expressible instead of
    being rounded up to reassurance.
    """
    if report.drift_mm:
        values = [abs(v) for v in report.drift_mm.values() if math.isfinite(v)]
        return max(values) if values else None
    if not report.applied_mm:
        return None
    worst = max(abs(report.applied_mm.get(a, 0.0) - d) for a, d in commanded.items())
    return worst if math.isfinite(worst) else None


@handler("lh.move_relative")
def move_relative(action: LHRelative, ctx: ActionContext) -> LHMoveOutputs:
    """Move the pipette head by a relative offset, from literals or from a solved offset.

    Literals are the plan's closing retract (R-VIS-11: ``dz=+40``, up). ``from_slot`` is the
    servo loop's nudge, and **resolving the slot into a motion is this handler's job**: the runner
    records the slot's value into ``ActionResult.inputs["from_slot_value"]`` and deliberately does
    not fill ``dx``/``dy``/``dz`` (`runner.py:806-807`). W5 must therefore read requested
    millimetres from ``LHMoveOutputs.requested_mm``, never from ``inputs``.
    """
    lh = ctx.devices.require_liquid_handler(action.device)

    if action.from_slot:
        deltas, skipped, refusal = _deltas_from_slot(action, ctx)
        if refusal:
            # Visible on the action, and through it on the loop that contains it: a stalled loop
            # must not look like a loop that quietly did nothing.
            ctx.warn("offset_refused", f"{refusal}; nothing moved", device=action.device)
            ctx.progress(f"no move: {refusal}")
        elif skipped:
            # Not a warning to be printed and forgotten: an axis the loop never closes is a
            # reason the loop may stall, and the operator needs it attached to the action.
            ctx.warn("unobserved_axes",
                     f"offset had no value for {', '.join(skipped)} — those axes not moved",
                     device=action.device)
    else:
        deltas = {"x": _finite("dx", action.dx), "y": _finite("dy", action.dy),
                  "z": _finite("dz", action.dz)}
        skipped = []

    moving = {a: v for a, v in deltas.items() if abs(v) >= MIN_STEP_MM}

    # O9 / R-VIS-7, on the magnitude of the whole step rather than per axis: three 10 mm
    # components are a 17 mm move, and this bound exists to cap how far one iteration can be
    # wrong. A refusal, not a clamp — see the module docstring.
    magnitude = math.sqrt(sum(v * v for v in moving.values()))
    if magnitude > action.clamp_mm:
        raise ValueError(
            f"requested move is {magnitude:.2f} mm over the {action.clamp_mm:g} mm "
            f"per-iteration clamp ({_fmt(deltas)}) — refusing rather than clamping, because a "
            f"step this size is a detection failure presented as a command (O9), and executing "
            f"a shortened version of it would hide the bad solve and move the head anyway")

    before = _position(lh)
    provenance = _provenance(lh)
    if not moving:
        # Nothing to do is a legitimate outcome — a converged axis set, or a refused solve — and
        # must not look like a failed move.
        if not refusal_reported(action, ctx):
            ctx.progress("offset already within tolerance on every observed axis; no move")
        return LHMoveOutputs(requested_mm=deltas, applied_mm={}, position_before=before,
                             position_after=before, provenance=provenance, drift_mm=None)

    if provenance == "dead_reckoned":
        # R-LH-4 / R-LOG-6: reachable on the action, not merely printed. Every `position_*`
        # number below, and the loop's implied gain, rest on step counts rather than feedback.
        ctx.warn("position_dead_reckoned",
                 f"{action.device}'s position is dead-reckoned from controller step counts, not "
                 f"measured — a stalled axis is counted as having moved. On this machine only "
                 f"the endstops are measurements.", device=action.device)

    _preflight(lh, moving, before)

    # A gantry move is seconds of motion, so the run gets its chance to pause or abort *before*
    # the command rather than noticing afterwards. Never `ActionAborted` raised by hand: inside a
    # loop that does not abort the run (review non-blocking 2).
    ctx.checkpoint()
    ctx.progress(f"nudging {action.device} by {_fmt(moving)}",
                 **{f"d{a}": v for a, v in moving.items()})

    report = _command(lh, moving)
    # Stamp the completion, so the next iteration's snapshot can tell a fresh frame from one
    # taken before this move (R-CAM-2/D20). Before the position readback, so a frame captured
    # during that readback still counts as post-move.
    camera_handler.mark_lh_move()

    applied = _numbers(report.applied_mm)
    after = _position(lh) or _numbers(report.position_after)
    drift = _drift(report, moving)

    if report.drift_mm:
        ctx.warn("move_shortfall",
                 f"commanded {_fmt(moving)} but achieved {_fmt(applied)} — an axis did not "
                 f"follow, so the next solve measures against a position that was not reached",
                 device=action.device)

    return LHMoveOutputs(
        requested_mm=deltas,
        applied_mm=applied,
        position_before=before or _numbers(report.position_before),
        position_after=after,
        # The driver's own claim when it actually moved something and said how it knows;
        # otherwise this handler's read, which defaults to the honest `dead_reckoned`.
        provenance=report.provenance if report.applied_mm else provenance,
        drift_mm=drift,
    )


def refusal_reported(action: LHRelative, ctx: ActionContext) -> bool:
    """Whether this action already carries a solve-refusal warning.

    Only used to keep the no-move path from reporting "already within tolerance" over the top of
    "the solve refused" — two very different reasons for not moving, and printing the reassuring
    one second is how a stalled loop reads as a converged one.
    """
    return any(w.code == "offset_refused" for w in ctx.collected_warnings())


def _fmt(deltas: dict[str, float]) -> str:
    return " ".join(f"{a.upper()}{v:+.2f}" for a, v in deltas.items()) or "nothing"


__all__ = ["move_relative", "AXES", "MIN_STEP_MM"]
