"""The shared simulated world: the sign convention, and that the loop's premise can fail.

`core.sim.world` exists so the servo loop converges *because the commanded moves close the
offset* (D3/R-SIM-5), not because a counter ticks. A test suite for it therefore has to do two
things, and the second matters more than the first:

1. show that commanding the offset converges it;
2. show that a **wrong-signed** world diverges — otherwise (1) proves only that the numbers
   shrank, which a script would also achieve, and a wrong-signed jacobian would still reach the
   bench.

The sign convention under test, stated once (see the module docstring for the full version):
``offset_mm`` is ``tube − tip`` in the task frame, ``+z`` is up, and closing it means commanding
``+offset``.
"""
from __future__ import annotations

import threading

import pytest

from core.sim.world import SimWorld, reset_world, world


@pytest.fixture(autouse=True)
def _clean_world():
    """No test may inherit another's offset — an offset left at 0.2 mm makes the next test's
    "it converged" assertion vacuous."""
    reset_world()
    yield
    reset_world()


# --- the sign convention ------------------------------------------------------------

def test_the_offset_is_tube_minus_tip_so_commanding_it_closes_it():
    """The one convention W3 codes against: command the offset, never its negation."""
    w = SimWorld({"x": 4.0, "y": -3.0, "z": 2.0}, gain=1.0, noise_mm=0.0)
    offset = w.offset()

    w.apply_move(**{f"d{a}": v for a, v in offset.items()})

    assert w.offset() == pytest.approx({"x": 0.0, "y": 0.0, "z": 0.0}, abs=1e-9)
    assert w.magnitude_mm() == pytest.approx(0.0, abs=1e-9)


def test_a_positive_dz_reduces_a_positive_z_offset():
    """+z is UP, and a positive `offset["z"]` means the tube mouth is above the tip.

    This is the assertion that pins the direction a wrong sign would flip. Getting it backwards
    on the real machine drives the pipette into the deck rather than away from it, so it is
    asserted on its own rather than only inside the vector case above.
    """
    w = SimWorld({"x": 0.0, "y": 0.0, "z": 5.0}, gain=1.0, noise_mm=0.0)
    w.apply_move(dz=+2.0)
    assert w.offset()["z"] == pytest.approx(3.0)

    w.apply_move(dz=-2.0)
    assert w.offset()["z"] == pytest.approx(5.0), "a negative dz must move the head back down"


def test_the_applied_motion_is_the_gain_not_the_command():
    """The world applies `gain × commanded`; the driver never sees that discrepancy.

    That asymmetry is the point: on a machine without encoders a stalled or short axis is
    invisible to the step counter, so the *only* way the error shows up is through the camera —
    which is exactly the bench situation this simulates.
    """
    w = SimWorld({"x": 10.0, "y": 0.0, "z": 0.0}, gain=0.5, noise_mm=0.0)
    applied = w.apply_move(dx=10.0)

    assert applied["x"] == pytest.approx(5.0)
    assert w.offset()["x"] == pytest.approx(5.0)


# --- convergence, and its failure --------------------------------------------------

def _servo(w: SimWorld, iterations: int, *, sign: float = +1.0) -> list[float]:
    """Run the loop's arithmetic against the world. One list entry per iteration.

    Deliberately not going through the handler or a driver: this is the *premise* under test —
    "commanding the measured offset closes it" — and mixing in a device would make a failure
    ambiguous between the premise and the plumbing.
    """
    trace = []
    for _ in range(iterations):
        offset = w.offset()
        w.apply_move(**{f"d{a}": sign * v for a, v in offset.items()})
        trace.append(w.magnitude_mm())
    return trace


def test_repeated_moves_converge_below_the_loops_threshold():
    """With the default gain error and noise, twelve iterations is comfortably enough.

    12 is `max_iterations` and 1.5 mm is `threshold_mm` from the real plan, so this is the bound
    the workflow actually runs under rather than a friendly one.

    Monotonicity is asserted only while the offset is still *above* the noise floor. Below it the
    residual bounces around a few hundredths of a millimetre, which is correct behaviour and not
    a loop getting worse — and the engine agrees: `Runner._watch` has a progress epsilon exactly
    so noise does not read as progress in either direction.
    """
    w = SimWorld({"x": 6.0, "y": -4.0, "z": 8.0})
    trace = _servo(w, 12)

    meaningful = [m for m in trace if m > 0.5]
    assert meaningful == sorted(meaningful, reverse=True), \
        f"an iteration above the noise floor made it worse: {trace}"
    assert trace[-1] < 1.5, f"did not converge below the 1.5 mm threshold: {trace}"
    first_converged = next(i for i, m in enumerate(trace, 1) if m < 1.5)
    assert first_converged <= 12, f"needed more than max_iterations: {trace}"


def test_a_wrong_signed_world_diverges_so_the_premise_can_fail():
    """The test that gives the one above its meaning.

    A negative gain is the wrong-signed jacobian: every commanded correction moves the head away
    from the tube. The offset must then *grow* — if it did not, convergence would be an artefact
    of the harness and the sign bug would still reach the bench with a pipette in it.
    """
    w = SimWorld({"x": 3.0, "y": 0.0, "z": 0.0}, gain=-0.85, noise_mm=0.0)
    trace = _servo(w, 5)

    assert trace == sorted(trace), f"an inverted world must diverge, got {trace}"
    assert trace[-1] > 3.0 * 4, f"expected runaway growth, got {trace}"


def test_commanding_the_negated_offset_diverges_too():
    """The same failure from the caller's side: a handler that negates the offset diverges.

    Both halves matter, because the sign can be wrong in the world (a mis-wired axis) or in the
    caller (a `-offset` that "looked right"), and the two are indistinguishable from the outcome.
    """
    w = SimWorld({"x": 3.0, "y": 0.0, "z": 0.0}, gain=0.85, noise_mm=0.0)
    trace = _servo(w, 5, sign=-1.0)

    assert trace[-1] > 3.0, f"negating the command must not converge, got {trace}"


def test_an_overshooting_gain_still_converges():
    """0 < gain < 2 converges; this pins that a gain above 1 is overshoot, not divergence."""
    w = SimWorld({"x": 5.0, "y": 0.0, "z": 0.0}, gain=1.4, noise_mm=0.0)
    trace = _servo(w, 10)
    assert abs(trace[-1]) < 0.1, f"an overshooting gain should still settle, got {trace}"


# --- housekeeping the tests depend on ----------------------------------------------

def test_noise_is_seeded_so_a_convergence_failure_is_reproducible():
    """An unseeded flake in the convergence test gets the test deleted, not the bug fixed."""
    a = _servo(SimWorld({"x": 5.0, "y": 5.0, "z": 5.0}, seed=7), 6)
    b = _servo(SimWorld({"x": 5.0, "y": 5.0, "z": 5.0}, seed=7), 6)
    assert a == b

    c = _servo(SimWorld({"x": 5.0, "y": 5.0, "z": 5.0}, seed=8), 6)
    assert c != a, "different seeds must give different noise, or the seed is being ignored"


def test_noise_is_applied_only_to_axes_that_were_commanded():
    """A zero command must be exactly zero motion — otherwise an axis nobody moved drifts, and
    the loop chases noise on an axis it never commanded."""
    w = SimWorld({"x": 1.0, "y": 1.0, "z": 1.0}, noise_mm=0.5)
    applied = w.apply_move(dx=1.0)
    assert applied["y"] == 0.0 and applied["z"] == 0.0
    assert w.offset()["y"] == 1.0 and w.offset()["z"] == 1.0


def test_reset_restores_the_defaults_rather_than_keeping_them():
    """A reset that preserved a previous test's inverted gain would be worse than none."""
    w = SimWorld({"x": 1.0, "y": 0.0, "z": 0.0}, gain=-2.0, noise_mm=0.0)
    w.apply_move(dx=1.0)
    assert w.moves == 1

    w.reset()
    assert w.moves == 0
    assert all(g > 0 for g in w.gain.values()), "an inverted gain survived a reset"
    assert w.offset() != {"x": 1.0, "y": 0.0, "z": 0.0}


def test_the_shared_world_is_one_object_and_resettable():
    """D3: one shared piece of state. Two callers must not each get their own."""
    assert world() is world()
    world().set_offset({"x": 9.0, "y": 0.0, "z": 0.0})
    assert world().offset()["x"] == 9.0

    reset_world({"x": 1.0, "y": 2.0, "z": 3.0}, gain=1.0, noise_mm=0.0)
    assert world().offset() == {"x": 1.0, "y": 2.0, "z": 3.0}
    assert world().gain == {"x": 1.0, "y": 1.0, "z": 1.0}


def test_concurrent_moves_do_not_lose_any_of_them():
    """Thread-safe because the engine's worker moves while the API thread reads a snapshot."""
    w = SimWorld({"x": 0.0, "y": 0.0, "z": 0.0}, gain=1.0, noise_mm=0.0)
    threads = [threading.Thread(target=lambda: [w.apply_move(dx=1.0) for _ in range(50)])
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert w.moves == 8 * 50
    assert w.offset()["x"] == pytest.approx(-400.0)


def test_a_snapshot_never_hands_out_the_internal_state():
    w = SimWorld({"x": 1.0, "y": 0.0, "z": 0.0}, noise_mm=0.0)
    snap = w.snapshot()
    snap["offset_mm"]["x"] = 999.0
    assert w.offset()["x"] == 1.0

    grabbed = w.offset()
    grabbed["x"] = 999.0
    assert w.offset()["x"] == 1.0


def test_an_offset_may_be_given_as_a_sequence_or_a_partial_mapping():
    assert SimWorld([1.0, 2.0, 3.0], noise_mm=0.0).offset() == {"x": 1.0, "y": 2.0, "z": 3.0}
    # A partial mapping fills the rest with 0.0 — the world is ground truth and has a value on
    # every axis by definition. `None` meaning "unobservable" is a property of a *measurement*.
    assert SimWorld({"z": 4.0}).offset() == {"x": 0.0, "y": 0.0, "z": 4.0}
    with pytest.raises(ValueError, match="3 components"):
        SimWorld([1.0, 2.0])
