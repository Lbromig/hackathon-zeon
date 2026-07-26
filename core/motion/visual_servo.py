"""Drive the gantry to a target seen by a camera.

This is the production caller that `deck_homography` and `ot_hand_eye` did not have:
until now both were imported only by their own tests, so a detection could never
become a move.

The loop is deliberately the simplest thing that closes:

    observe the carriage   ->  observe the target
    map both to the deck   ->  jog the difference
    re-observe                 repeat until inside tolerance

Three properties of this machine shape the design, and all three are the reason it
looks more defensive than a servo loop normally would.

**No datum is needed and none is used.** Every correction is relative: "move 12 mm
that way" requires no origin. This matters because the gantry cannot be homed on one
axis at all and its counters re-zero unpredictably, and an earlier reading of that
constraint wrongly concluded vision-guided motion had to wait for a datum. It does
not. Returning to a remembered place needs an origin; closing a visual error does
not.

**A wrong direction is a collision, silently.** There are no endstops and no current
sensing, so a sign error drives the carriage into a hard stop while every software
signal reports success. The loop therefore checks that the error actually shrinks and
stops if it does not, rather than trusting the transform.

**Observation can fail at any iteration.** A lost marker is not a small error, it is
no information, and continuing on the last known offset is how a servo runs away. Any
iteration that cannot see both points stops the loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, Sequence

import numpy as np

from core.calibration.deck_homography import DeckFit, correction_mm

# Never command a single jog larger than this. A larger correction is CLAMPED to it
# and the loop iterates, rather than being refused: a bounded step size is a safety
# property, not a limit on how far the servo can travel. An earlier version refused
# outright whenever the computed move exceeded this, which made the loop useless for
# any error bigger than one step and would have been discovered at the bench.
MAX_STEP_MM = 15.0

# Refuse before moving if the target is further away than this. Unlike the step
# limit, this is a sanity bound rather than a safety one: a correction this large
# usually means the fit is wrong or the target was mis-detected, and on a machine
# that cannot detect a collision that deserves a human rather than a long traverse.
MAX_REACH_MM = 150.0

# Below this the carriage is on target. Coarser than the gantry's resolution on
# purpose: chasing a smaller error than the detector can resolve produces dither.
DEFAULT_TOLERANCE_MM = 1.5

# An iteration that does not reduce the error by at least this fraction of the
# remaining distance has not made progress.
MIN_PROGRESS_FRACTION = 0.15

DEFAULT_MAX_ITERATIONS = 20


class Outcome(str, Enum):
    """Why the loop stopped. Only ARRIVED means the carriage is on target."""

    ARRIVED = "arrived"
    LOST_SIGHT = "lost_sight"          # a required point was not visible
    NOT_CONVERGING = "not_converging"  # error stopped shrinking, or grew
    STEP_TOO_LARGE = "step_too_large"  # computed move exceeded the safety limit
    EXHAUSTED = "exhausted"            # ran out of iterations while still improving
    REFUSED = "refused"                # preconditions not met; nothing was commanded


@dataclass
class ServoStep:
    """One iteration, recorded so a run can be audited after the fact."""

    iteration: int
    carriage_uv: tuple[float, float] | None = None
    error_mm: float = 0.0
    commanded_mm: tuple[float, float] = (0.0, 0.0)


@dataclass
class ServoResult:
    outcome: Outcome
    detail: str = ""
    steps: list[ServoStep] = field(default_factory=list)
    final_error_mm: float | None = None

    @property
    def ok(self) -> bool:
        """True only for ARRIVED. Every other outcome leaves position unknown."""
        return self.outcome is Outcome.ARRIVED

    @property
    def moved(self) -> bool:
        """Whether anything was commanded. REFUSED runs must not have moved."""
        return any(s.commanded_mm != (0.0, 0.0) for s in self.steps)


class Jogger(Protocol):
    """Anything that can move an axis by a relative amount, in millimetres.

    Narrow on purpose. The concrete driver's full interface is not needed, a mock
    satisfies this in one line, and the loop is therefore testable with no hardware.
    Relative by construction: there is no absolute variant here, so this loop cannot
    accidentally depend on a datum.
    """

    def jog(self, axis: str, delta_mm: float) -> None: ...


class PixelObserver(Protocol):
    """Reports where something is in the image, or None when it cannot see it.

    Returning None is a first-class answer and callers must handle it. A detector
    that substitutes its last known position when it loses the target converts a
    visible failure into a servo that drives on stale data.
    """

    def observe(self) -> tuple[float, float] | None: ...


def servo_to_target(
    fit: DeckFit,
    jogger: Jogger,
    carriage: PixelObserver,
    target_uv: Sequence[float],
    *,
    tolerance_mm: float = DEFAULT_TOLERANCE_MM,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    max_step_mm: float = MAX_STEP_MM,
    max_reach_mm: float = MAX_REACH_MM,
    axis_x: str = "X",
    axis_y: str = "Y",
) -> ServoResult:
    """Jog the carriage until it is over `target_uv`, in image coordinates.

    `carriage` is re-observed every iteration. That is the whole point: the counters
    cannot be trusted, so the camera, not the controller, decides when the loop is
    done. It also means a stalled axis is caught, because a stall shows up as an
    error that does not shrink while the controller reports every move as complete.
    """
    steps: list[ServoStep] = []

    if not fit.trustworthy:
        return ServoResult(
            Outcome.REFUSED,
            f"calibration not trustworthy: {fit.why_not_trustworthy()}",
            steps,
        )

    try:
        # Validate the target maps to the deck before moving. The value is not
        # needed; the raise is the point. A target on the plane's horizon has no
        # finite deck coordinate, and discovering that mid-loop would already have
        # commanded motion toward it.
        fit.to_deck(target_uv)
    except ValueError as exc:
        return ServoResult(Outcome.REFUSED, f"target is not on the deck plane: {exc}", steps)

    previous_error: float | None = None

    for i in range(1, max_iterations + 1):
        seen = carriage.observe()
        if seen is None:
            return ServoResult(
                Outcome.LOST_SIGHT,
                f"carriage marker not visible at iteration {i}; stopping rather than "
                "continuing on a stale position",
                steps,
                previous_error,
            )

        try:
            delta = correction_mm(fit, seen, target_uv)
        except ValueError as exc:
            return ServoResult(Outcome.REFUSED, f"cannot map observation: {exc}", steps, previous_error)

        error = float(np.linalg.norm(delta))

        if error <= tolerance_mm:
            steps.append(ServoStep(i, tuple(seen), error, (0.0, 0.0)))
            return ServoResult(
                Outcome.ARRIVED,
                f"within {tolerance_mm} mm after {i - 1} correction(s)",
                steps,
                error,
            )

        if i == 1 and error > max_reach_mm:
            return ServoResult(
                Outcome.STEP_TOO_LARGE,
                f"target is {error:.1f} mm away, beyond the {max_reach_mm} mm reach "
                "limit. A correction that large usually means the calibration is "
                "wrong or the target was mis-detected. Move the carriage closer by "
                "hand, or raise the limit deliberately.",
                steps,
                error,
            )

        # Progress check before commanding anything. A sign error, a bad fit and a
        # stalled axis all present identically here: the error fails to shrink. On a
        # machine with no endstops that is the only signal available before a
        # collision, so it is terminal rather than retried.
        #
        # The comparison is against the progress a CLAMPED step could achieve, not
        # against the full remaining error. Otherwise every clamped step looks like a
        # failure to converge, and the loop would abort precisely when it was working
        # correctly on a distant target.
        if previous_error is not None:
            achievable = min(previous_error, max_step_mm)
            if (previous_error - error) < MIN_PROGRESS_FRACTION * achievable:
                return ServoResult(
                    Outcome.NOT_CONVERGING,
                    f"error went {previous_error:.2f} mm -> {error:.2f} mm at iteration "
                    f"{i}, which is not progress for a step of at most "
                    f"{achievable:.1f} mm. Possible causes: the calibration has the "
                    "wrong sign or axes, an axis is stalled, or the target moved. "
                    "Stopped without commanding further motion.",
                    steps,
                    error,
                )

        dx, dy = float(delta[0]), float(delta[1])
        if error > max_step_mm:
            # Clamp, preserving direction. Travelling far is fine; travelling far in
            # one uninterruptible move on a machine that cannot feel a collision is
            # not, because nothing re-observes until the move completes.
            scale = max_step_mm / error
            dx, dy = dx * scale, dy * scale

        steps.append(ServoStep(i, tuple(seen), error, (dx, dy)))
        jogger.jog(axis_x, dx)
        jogger.jog(axis_y, dy)
        previous_error = error

    return ServoResult(
        Outcome.EXHAUSTED,
        f"still improving after {max_iterations} iterations but not yet within "
        f"{tolerance_mm} mm; increase the budget or check the fit",
        steps,
        previous_error,
    )


def collect_calibration(
    jogger: Jogger,
    carriage: PixelObserver,
    offsets_mm: Sequence[tuple[float, float]],
    *,
    axis_x: str = "X",
    axis_y: str = "Y",
) -> tuple[list[list[float]], list[list[float]]]:
    """Drive a pattern of relative offsets, observing the carriage at each stop.

    Returns paired (image_points, deck_points) ready for `fit_deck_homography`. Deck
    coordinates are accumulated from the *commanded* offsets and are therefore
    relative to wherever the carriage started, which is all the homography needs: it
    fits a mapping between two frames, and the origin of either is arbitrary.

    A stop where the carriage cannot be seen is skipped rather than guessed. Skipping
    costs a correspondence; guessing corrupts the fit for every future move, and the
    fit's own residual would not necessarily reveal it.
    """
    image_points: list[list[float]] = []
    deck_points: list[list[float]] = []
    x = y = 0.0

    first = carriage.observe()
    if first is not None:
        image_points.append([float(first[0]), float(first[1])])
        deck_points.append([0.0, 0.0])

    for dx, dy in offsets_mm:
        jogger.jog(axis_x, float(dx))
        jogger.jog(axis_y, float(dy))
        x += float(dx)
        y += float(dy)
        seen = carriage.observe()
        if seen is None:
            continue
        image_points.append([float(seen[0]), float(seen[1])])
        deck_points.append([x, y])

    return image_points, deck_points


# A spread pattern rather than a line or a raster edge. `fit_deck_homography` refuses
# collinear correspondences, and a raster's first row is collinear, so a naive sweep
# produces a refusal at the end of a slow physical procedure. These offsets are
# relative steps, each within MAX_STEP_MM.
DEFAULT_CALIBRATION_OFFSETS_MM: tuple[tuple[float, float], ...] = (
    (12.0, 0.0),
    (0.0, 12.0),
    (-12.0, 6.0),
    (10.0, -14.0),
    (-14.0, -8.0),
    (6.0, 10.0),
)
