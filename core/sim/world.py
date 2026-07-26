"""The shared simulated world: one number, the tip↔tube offset (D3, R-SIM-5).

Why this module exists
----------------------
A servo loop that converges because a counter ticks proves nothing. The whole value of
simulating this workflow is that the loop must converge **because the commanded liquid-handler
moves actually close the offset** — so that a wrong-signed jacobian, a wrong-signed Z, or a
gain applied in the wrong direction is a *reproducible pytest failure* rather than a bench
surprise with a pipette going through a plate.

So there is exactly one piece of shared simulated state: the vector from the pipette tip to
the tube mouth. Two consumers, and they must not each invent their own:

* the **mock liquid handler** (`drivers/mock`) decrements it on every relative move, with a
  gain error and a little noise, because a perfect actuator would hide the very class of bug
  this exists to catch;
* the **synthetic camera / detector path** (W3) renders or reports it, so the vision solve
  measures the same world the mover moved.

Nothing else may hold a private copy. If a second source of truth appears, the loop can
converge in one of them while diverging in the other, which is exactly the bench surprise this
prevents.

.. _sim-sign-convention:

The sign convention — read this before touching either side
------------------------------------------------------------
Stated here once, in the module both sides import, because an unstated convention is precisely
how the sign bug gets reintroduced.

**The offset is `tube − tip`, in the task frame, in millimetres.**

``offset_mm = {"x": …, "y": …, "z": …}`` is the displacement the **pipette tip must still
travel** to arrive at the tube mouth: ``p_tube − p_tip``. All three zero means aligned. This
matches `core.perception.offset`'s primary path exactly, which solves ``t_tube - t_tip``, so
``OffsetOutputs.residual_offset_mm`` and this offset are the *same quantity in the same
direction* and neither side needs a negation.

**Therefore: to close the offset, command the offset.** ``lh.move_relative(dx=offset.x,
dy=offset.y, dz=offset.z)`` reduces it. There is no minus sign anywhere on the servo path. If
you find yourself writing one, the bug is elsewhere.

**The task frame's axes.** ``x`` and ``y`` are the OT-One gantry's ``X`` and ``Y``. ``z`` is
**positive UP**, away from the deck — which is *not* the gantry's raw ``Z``:

    task  +z  (up, retract)   ==   gantry  Z−     (``drivers.opentrons.driver.Z_UP_SIGN``)

That flip is measured, not assumed (camera: ``G0 Z+6`` lowered the head; endstop: ``G0 Z+5``
released ``min_z``, so ``min_z`` is at the *top* of travel), and it lives in exactly one place:
`OpentronsDriver.move_relative`, which converts task frame to wire. Everything above the
driver — this world, the offset solver, the blackboard, `lh.move_relative`, the plan's closing
``dz=+40`` retract — speaks the task frame, where **+z is up**.

Read that last paragraph as: *a positive commanded ``dz`` raises the pipette head.* A positive
``offset_mm["z"]`` therefore means the tube mouth is **above** the tip, and the head must rise
to reach it.

Why a gain error and noise are on by default
--------------------------------------------
``gain`` is the fraction of a commanded move the world actually applies. It defaults below 1
because a loop that closes in a single step tests nothing about iteration, and because the real
gain is a measured px/mm that is never exact. ``noise_mm`` is small and *seeded*, so the tests
stay reproducible while the loop still has to be robust to a solve that is never quite right.

A ``gain`` above 1 overshoots but still converges (0 < gain < 2). A **negative** gain models
the wrong-signed jacobian, and the loop then diverges — there is a test that asserts exactly
that, because "the premise can fail" is the only evidence that the passing test means anything.
"""
from __future__ import annotations

import random
import threading
from typing import Any, Iterable, Mapping

#: Axis order everywhere in this module. Matches `OffsetOutputs.residual_offset_mm`'s keys.
AXES: tuple[str, ...] = ("x", "y", "z")

#: Fraction of a commanded move the world applies, per axis, by default. Below 1 so the loop
#: needs several iterations: an actuator that lands exactly on the target does not exercise
#: iteration, and no real gain is exact.
DEFAULT_GAIN = 0.85

#: Zero-mean noise added to each applied axis, mm. Small enough that the loop still converges
#: below a 1.5 mm threshold, large enough that a solve is never exactly right.
DEFAULT_NOISE_MM = 0.05

#: Where the offset starts when nothing says otherwise: a few mm out on every axis, which is
#: the situation after the arm has presented the tube and before the loop has run.
DEFAULT_OFFSET_MM: dict[str, float] = {"x": 6.0, "y": -4.0, "z": 8.0}


def _as_offset(value: Mapping[str, float] | Iterable[float] | None) -> dict[str, float]:
    """Coerce a mapping, a 3-sequence or ``None`` into a full ``{x, y, z}`` of floats.

    Absent axes become ``0.0`` here — and *only* here. Everywhere downstream of the world,
    ``None`` means "not observable" and must never be silently read as zero (that is the
    distinction `OffsetOutputs.residual_offset_mm` exists to preserve); but the world is ground
    truth, and ground truth has a value on every axis by definition.
    """
    if value is None:
        return dict(DEFAULT_OFFSET_MM)
    if isinstance(value, Mapping):
        return {a: float(value.get(a, 0.0) or 0.0) for a in AXES}
    seq = list(value)
    if len(seq) != len(AXES):
        raise ValueError(f"an offset needs {len(AXES)} components, got {len(seq)}")
    return {a: float(v) for a, v in zip(AXES, seq)}


def _as_gain(value: Mapping[str, float] | float | None) -> dict[str, float]:
    """Per-axis gain from a scalar, a mapping, or ``None``."""
    if value is None:
        return {a: DEFAULT_GAIN for a in AXES}
    if isinstance(value, Mapping):
        return {a: float(value.get(a, DEFAULT_GAIN)) for a in AXES}
    return {a: float(value) for a in AXES}


class SimWorld:
    """The tip↔tube offset, and what a commanded move does to it. Thread-safe.

    Thread-safe because the engine's worker thread moves the mock handler while the API thread
    may read a snapshot for the UI — the same reason `Blackboard` holds a lock. A torn read
    here would show an offset that never existed.

    Resettable because tests must not leak state into each other: a world left at 0.2 mm by one
    test makes the next one's "it converges" assertion vacuous.

    See the module docstring for the sign convention. In one line:
    ``offset_mm`` is ``tube − tip`` in the task frame (+z up), and commanding ``+offset``
    closes it.
    """

    def __init__(self, offset_mm: Mapping[str, float] | Iterable[float] | None = None, *,
                 gain: Mapping[str, float] | float | None = None,
                 noise_mm: float = DEFAULT_NOISE_MM,
                 seed: int | None = 0) -> None:
        self._lock = threading.RLock()
        self._offset = _as_offset(offset_mm)
        self._gain = _as_gain(gain)
        self._noise_mm = float(noise_mm)
        # Seeded by default: a reproducible failure is worth more than a realistic one, and an
        # unseeded flake in the convergence test would get the test deleted rather than the bug
        # fixed. `seed=None` gives real randomness for a soak run.
        self._rng = random.Random(seed)
        self._moves = 0

    # --- reading -------------------------------------------------------------
    def offset(self) -> dict[str, float]:
        """A copy of the current offset, ``tube − tip``, task frame, mm."""
        with self._lock:
            return dict(self._offset)

    def magnitude_mm(self) -> float:
        """Euclidean norm of the offset — the number the loop's threshold is compared to.

        Always a real number: the world knows every axis, so there is no "unobservable" case
        here. A *solve* may report ``None`` for an axis it could not see; that is a statement
        about the measurement, not about the world.
        """
        with self._lock:
            return sum(v * v for v in self._offset.values()) ** 0.5

    @property
    def moves(self) -> int:
        """How many relative moves have been applied. For asserting a loop actually iterated."""
        with self._lock:
            return self._moves

    @property
    def gain(self) -> dict[str, float]:
        with self._lock:
            return dict(self._gain)

    def snapshot(self) -> dict[str, Any]:
        """Plain-dict view for a log record or the UI. Never the internal dicts."""
        with self._lock:
            return {"offset_mm": dict(self._offset), "magnitude_mm": self.magnitude_mm(),
                    "gain": dict(self._gain), "noise_mm": self._noise_mm,
                    "moves": self._moves}

    # --- writing -------------------------------------------------------------
    def apply_move(self, dx: float = 0.0, dy: float = 0.0, dz: float = 0.0, *,
                   noisy: bool = True) -> dict[str, float]:
        """Move the tip by ``(dx, dy, dz)`` in the task frame; return what the world applied.

        The tip moves, so the offset ``tube − tip`` **decrements** by the applied motion. That
        subtraction is the load-bearing line of this module: it is what makes commanding
        ``+offset`` converge and commanding ``−offset`` diverge, which is the sign bug worth
        catching.

        The applied motion is ``gain × commanded (+ noise)``, not the commanded motion — the
        return value is therefore ground truth about the world, and is deliberately *not* what
        the mock driver reports as ``applied_mm``. The driver has no encoders (like the real
        OT-One), so what it reports is what it commanded; the discrepancy is only ever visible
        through the camera, which is exactly the situation on the bench.
        """
        commanded = {"x": float(dx), "y": float(dy), "z": float(dz)}
        with self._lock:
            applied: dict[str, float] = {}
            for axis in AXES:
                step = commanded[axis]
                if step == 0.0:
                    applied[axis] = 0.0
                    continue
                moved = self._gain[axis] * step
                if noisy and self._noise_mm:
                    moved += self._rng.uniform(-self._noise_mm, self._noise_mm)
                applied[axis] = moved
                self._offset[axis] -= moved
            self._moves += 1
            return applied

    def set_offset(self, offset_mm: Mapping[str, float] | Iterable[float]) -> None:
        """Place the tube relative to the tip. Used to stage a test, not by a driver."""
        with self._lock:
            self._offset = _as_offset(offset_mm)

    def reset(self, offset_mm: Mapping[str, float] | Iterable[float] | None = None, *,
              gain: Mapping[str, float] | float | None = None,
              noise_mm: float | None = None, seed: int | None = 0) -> "SimWorld":
        """Back to a known state, returning self so a fixture can chain.

        Everything not named is restored to its default rather than kept: a reset that
        preserves a previous test's inverted gain is worse than no reset at all.
        """
        with self._lock:
            self._offset = _as_offset(offset_mm)
            self._gain = _as_gain(gain)
            self._noise_mm = DEFAULT_NOISE_MM if noise_mm is None else float(noise_mm)
            self._rng = random.Random(seed)
            self._moves = 0
        return self

    def __repr__(self) -> str:
        o = self.offset()
        return (f"<SimWorld offset=({o['x']:+.2f}, {o['y']:+.2f}, {o['z']:+.2f}) mm "
                f"|{self.magnitude_mm():.2f}| moves={self._moves}>")


#: The process-wide world. A module-level singleton rather than something passed through the
#: engine because the two sides that must agree — a driver deep in the fleet and a camera
#: elsewhere in it — have no common owner to hand it to, and D3's whole point is that they
#: share *one* piece of state. A driver may still be handed its own via
#: ``config["sim_world"]``, which is how a test isolates itself without touching the singleton.
_WORLD = SimWorld()


def world() -> SimWorld:
    """The shared world. Both the mock liquid handler and the synthetic camera call this."""
    return _WORLD


def reset_world(offset_mm: Mapping[str, float] | Iterable[float] | None = None, *,
                gain: Mapping[str, float] | float | None = None,
                noise_mm: float | None = None, seed: int | None = 0) -> SimWorld:
    """Reset the shared world. Call this in a fixture, not in shipping code."""
    return _WORLD.reset(offset_mm, gain=gain, noise_mm=noise_mm, seed=seed)


__all__ = ["AXES", "DEFAULT_GAIN", "DEFAULT_NOISE_MM", "DEFAULT_OFFSET_MM", "SimWorld",
           "world", "reset_world"]
