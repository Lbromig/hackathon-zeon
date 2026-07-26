"""Capability interface for liquid handlers (e.g. the Opentrons).

The **task frame**, and why it is not the gantry frame
-----------------------------------------------------
Every millimetre that crosses this interface — :meth:`LiquidHandlerDriver.move_relative`'s
deltas, :class:`RelativeMoveReport`'s positions, :class:`MoveLimits`' envelope — is in the
**task frame**:

* ``x`` / ``y``: the gantry's own X and Y.
* ``z``: **positive is UP**, away from the deck.

A driver whose controller disagrees converts internally and nowhere else. The OT-One is exactly
such a controller: measured on the bench, ``G0 Z+6`` lowered the head and ``G0 Z+5`` released
``min_z``, so its raw ``Z+`` is *down* and ``min_z`` sits at the **top** of travel. That flip
lives in `drivers.opentrons.driver` (``Z_UP_SIGN``) and appears nowhere above it.

The reason the interface, rather than the controller, defines up: a sign error on Z drives the
pipette into the deck instead of away from it, and it is the single most dangerous defect in
this subsystem. It therefore gets one stated definition that every caller and every driver is
measured against, rather than a per-driver convention that callers have to remember. The plan's
closing retract is written ``dz=+40`` and it must mean *up* on every liquid handler there will
ever be. `core.sim.world` documents the same convention for the simulated side.

What Phase 5 added, and what it deliberately did not
----------------------------------------------------
Additive only (D28, and R-LH-6 is withdrawn): :meth:`move_relative`, :meth:`initialize`,
:meth:`retract_z`, :meth:`move_limits` and :meth:`position` are new. ``aspirate``, ``dispense``,
``pick_up_tip``, ``drop_tip``, ``home`` and ``move_to`` are untouched, by an explicit decision —
the interface is stable and removing it would be a breaking change for no gain.

The five new methods are **not** ``@abstractmethod``. They raise
:class:`~drivers.base.DriverError` by default instead, for two reasons: making them abstract
would break every existing concrete subclass at import time (which is not "additive"), and a
driver that cannot do relative motion should say so *loudly at the call site* rather than be
impossible to instantiate. What must never happen is the third option — a default that returns
``None`` and reports success. That is the failure R-LH-5 was written about: `connect()` used to
assign a dummy object and `_send()` used to return ``None``, so every liquid-handling call
reported success while doing nothing.
"""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from ..base import DriverError, InstrumentDriver, InstrumentKind

#: Axis order for every dict in this module. Lower case, matching
#: `OffsetOutputs.residual_offset_mm` and `core.sim.world`, so no consumer has to case-fold.
AXES: tuple[str, ...] = ("x", "y", "z")

#: How a driver knows where it is (R-LH-4). ``dead_reckoned`` is the honest answer for a
#: controller reporting its own step counts, and the safe default for a driver that does not
#: say: silence must never read as "measured".
Provenance = Literal["measured", "dead_reckoned"]


@dataclass
class DeckLocation:
    """A point the pipette can reach: a labware slot/well, or a taught XYZ."""
    slot: str | None = None
    well: str | None = None
    x: float | None = None
    y: float | None = None
    z: float | None = None


@dataclass
class MoveLimits:
    """What a driver will and will not accept, so a caller can be refused *before* moving.

    Two independent bounds, because they answer different questions:

    * ``max_step_mm`` bounds **one commanded move** per axis. It always applies, and it is what
      bounds the damage a wrong number can do on a machine with no absolute reference at all.
    * ``envelope_mm`` bounds the **resulting absolute position** per axis, ``(low, high)``, in
      whatever frame the driver's position readback uses. Optional, and absent by default on
      purpose: on an unhomed machine the origin is whatever the controller booted with, so a
      hardcoded envelope would be meaningless — or worse, wrong in a way that reads as a safety
      feature.

    Either bound being exceeded is a **refusal with a stated reason, never a silent clamp**
    (R-LH-3). Clamping turns a unit-conversion bug into a crash into the deck: a request of
    ``dz = -250`` because someone passed micrometres gets quietly executed as the driver's
    maximum descent, and the log says the move succeeded.
    """
    max_step_mm: dict[str, float] = field(default_factory=dict)
    envelope_mm: dict[str, tuple[float, float]] = field(default_factory=dict)

    def refusal_for(self, axis: str, delta: float,
                    position: dict[str, float] | None = None) -> str | None:
        """The reason ``axis`` may not move by ``delta``, or ``None`` if it may.

        Returns text rather than raising so a caller can pre-flight every axis and report all
        the problems, instead of failing on the first one and moving the rest.
        """
        step = self.max_step_mm.get(axis)
        if step is not None and abs(delta) > step:
            return (f"{axis}{delta:+g} mm exceeds the {step:g} mm single-move limit — "
                    f"refusing rather than clamping")
        span = self.envelope_mm.get(axis)
        if span is not None and position is not None and axis in position:
            low, high = float(span[0]), float(span[1])
            target = position[axis] + delta
            if not (low <= target <= high):
                return (f"{axis} target {target:.3f} mm is outside the configured envelope "
                        f"[{low:g}, {high:g}] — refusing rather than clamping")
        return None


@dataclass
class RelativeMoveReport:
    """What a relative move did. Maps onto `LHMoveOutputs` field for field.

    ``frame`` is recorded rather than assumed. It is always ``"task"`` for anything returned
    across this interface (+z up, see the module docstring), and naming it in the record is
    cheap insurance against a future driver that reports its controller's frame by accident.

    ``applied_mm`` is what the **driver believes** moved — from its own readback where it has
    one, and from what it commanded where it does not. On a machine without encoders those are
    the same number, and ``provenance`` is what says which. It is deliberately *not* ground
    truth: on the simulated path the world applies a gain error the driver cannot see, exactly
    as a real stalled axis is invisible to a step counter.
    """
    requested_mm: dict[str, float] = field(default_factory=dict)
    applied_mm: dict[str, float] = field(default_factory=dict)
    position_before: dict[str, float] = field(default_factory=dict)
    position_after: dict[str, float] = field(default_factory=dict)
    provenance: Provenance = "dead_reckoned"
    drift_mm: dict[str, float] = field(default_factory=dict)
    """Per axis, ``applied − requested``, for the axes where it is worth reporting. A commanded
    move an axis did not make is the failure that matters most here: every later offset would be
    computed against a position that was never reached."""
    moved: bool = False
    frame: Literal["task"] = "task"
    detail: dict[str, Any] = field(default_factory=dict)
    """Driver-specific extras (endstops, wire-level counts). Never load-bearing for a caller."""


class LiquidHandlerDriver(InstrumentDriver):
    kind = InstrumentKind.LIQUID_HANDLER

    @abstractmethod
    def home(self) -> None: ...

    @abstractmethod
    def pick_up_tip(self, location: DeckLocation) -> None: ...

    @abstractmethod
    def drop_tip(self, location: DeckLocation | None = None) -> None: ...

    @abstractmethod
    def aspirate(self, volume_ul: float, location: DeckLocation) -> None: ...

    @abstractmethod
    def dispense(self, volume_ul: float, location: DeckLocation) -> None: ...

    @abstractmethod
    def move_to(self, location: DeckLocation) -> None: ...

    # --- added in Phase 5, additive only (D28) -------------------------------

    def move_relative(self, dx: float = 0.0, dy: float = 0.0,
                      dz: float = 0.0) -> RelativeMoveReport:
        """Move the pipette head by a relative offset, mm, **task frame** (R-LH-1).

        The action the servo loop drives, and the single most load-bearing capability here.

        **Relative semantics at this interface, whatever the wire does.** A driver with a
        position readback is expected to implement it as read → check → command, and may issue
        an absolute command underneath; a driver without one issues a relative command. Either
        way the caller asks for a displacement, because that is what a vision solve produces and
        an unhomed machine has no absolute frame to express a target in.

        ``dz`` positive raises the head. See the module docstring — this is the one convention
        whose reversal breaks the pipette rather than the log.

        Refuses rather than clamps when :meth:`move_limits` says no (R-LH-3), and refuses a
        non-finite delta: every ``abs(delta) > limit`` comparison is False against NaN, so a
        naive check waves it straight through onto the hardware.
        """
        raise DriverError(
            f"{self.device_id}: this driver has no relative XYZ motion, so the servo loop "
            f"cannot drive it. (Refusing loudly rather than reporting a move that never "
            f"happened — see R-LH-5.)")

    def initialize(self, retract_z: bool = True) -> dict[str, Any]:
        """Prove each axis moves, then retract Z up (R-LH-2 / R-INIT-2).

        Each axis a small amount in **both** directions and back, so a stuck axis or an inverted
        direction is found while the moves are millimetres rather than during a 100 mm traverse.
        Then Z up, so the head ends clear of the deck and a later absolute mistake has further
        to fall.

        Named ``initialize`` exactly, because `lifecycle._init_liquid_handler` probes for that
        attribute by name and *warns and skips* when it is absent — a different name means the
        wiggle silently never runs.

        Plungers are never touched. On the OT-One the A/B axes are the pipette plungers, and
        driving a plunger into its stop is the failure that started this driver's rules.
        """
        raise DriverError(
            f"{self.device_id}: this driver has no initialize routine, so the axis wiggle and "
            f"Z retract of R-LH-2 cannot run")

    def retract_z(self, distance_mm: float | None = None) -> dict[str, Any]:
        """Raise the head by up to ``distance_mm``, stopping early at a hard stop if there is one.

        Bounded rather than open-ended: without an upper endstop the only thing standing between
        a retract and the mechanical top is the number, and a request far larger than the travel
        means something is wrong rather than something is far away.
        """
        raise DriverError(f"{self.device_id}: this driver cannot retract Z")

    def move_limits(self) -> MoveLimits:
        """The bounds :meth:`move_relative` enforces, so a caller can pre-flight.

        Empty limits are *not* a claim that everything is safe — they mean this driver states no
        bounds, which a caller may treat as a reason for its own.
        """
        return MoveLimits()

    def position(self) -> dict[str, Any]:
        """Where the head is, and **how that is known** (R-LH-4).

        ``{"actual": {x,y,z}, "commanded": {x,y,z}, "provenance": {...}}``, task frame. The two
        position dicts are separate because they diverge, and the difference is the diagnostic:
        after a limit halt the OT-One reported a commanded ``Z:2.000`` against an actual
        ``z:0.006`` — the planner had advanced 2 mm the axis never travelled.

        ``provenance`` exists because the servo loop's gain estimate is only as good as the
        distinction between a measurement and an assumption. A controller's own step counts are
        dead reckoning, not feedback, and a consumer must not be able to mistake one for the
        other.
        """
        raise DriverError(f"{self.device_id}: this driver has no position readback")


__all__ = ["AXES", "DeckLocation", "LiquidHandlerDriver", "MoveLimits", "Provenance",
           "RelativeMoveReport"]
