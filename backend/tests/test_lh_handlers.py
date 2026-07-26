"""`lh.move_relative` and the mock liquid handler, against the shared simulated world.

The distinguishing property of this suite is that the loop it tests **converges because the
commanded moves close the offset** (D3/R-SIM-5). Nothing here scripts a shrinking number: the
handler reads `core.sim.world`'s offset out of a blackboard slot, commands it, the mock applies it
with a gain error, and the world is re-read. So a wrong sign, a wrong frame, or a driver that
reports success while doing nothing all show up as a failing assertion rather than as a bench
surprise — and `test_an_inverted_world_diverges_through_the_handler_too` asserts the premise can
fail, which is what gives the convergence test its meaning.

The sign convention under test, in one line: the offset is ``tube − tip`` in the task frame,
``+z`` is up, and closing it means commanding ``+offset``. See
`backend/app/engine/handlers/liquid_handler.py` for the full statement.
"""
from __future__ import annotations

import logging

import pytest

from backend.app.engine.actions import LHMoveOutputs, LHRelative, OffsetOutputs
from backend.app.engine.blackboard import Blackboard
from backend.app.engine.context import ActionContext
from backend.app.engine.handlers import liquid_handler as lh_handler
from core.sim.world import SimWorld
from drivers.base import DriverError
from drivers.mock import MockLiquidHandlerDriver


class Devices:
    """The slice of the device manager a handler may use."""

    def __init__(self, **devices):
        self._d = devices

    def get(self, device_id):
        return self._d[device_id]

    def require_arm(self, device_id):
        return self._d[device_id]

    def require_liquid_handler(self, device_id):
        return self._d[device_id]

    def is_simulated(self, device_id):
        return True


def make_ctx(action, devices, blackboard=None):
    return ActionContext(run_id="test", action=action, devices=devices,
                         blackboard=blackboard or Blackboard(),
                         log=logging.getLogger("test"), simulated=True)


def codes(ctx) -> set[str]:
    return {w.code for w in ctx.collected_warnings()}


def make_lh(world: SimWorld | None = None, **config) -> MockLiquidHandlerDriver:
    """A connected mock handler on its own world, so no test can inherit another's offset."""
    lh = MockLiquidHandlerDriver("ot", {"sim_world": world or SimWorld(), **config})
    lh.connect()
    return lh


def put_offset(bb: Blackboard, **residual) -> None:
    """Write a solved offset into `selected_offset`, the slot the servo loop's nudge reads."""
    bb.set("selected_offset", OffsetOutputs(
        residual_offset_mm={a: residual.get(a) for a in ("x", "y", "z")},
        observed_axes=[a for a, v in residual.items() if v is not None],
        method="tag_3d"))


def nudge(lh, bb, **kwargs) -> LHMoveOutputs:
    """Run the handler exactly as the runner would, returning its typed outputs."""
    action = LHRelative(device="ot", **kwargs)
    ctx = make_ctx(action, Devices(ot=lh), bb)
    return lh_handler.move_relative(action, ctx), ctx


# --- literal deltas, and the frozen contract's fields ------------------------------

def test_literal_deltas_fill_every_output_field():
    """W5 renders these five, so every one of them has to be populated, not defaulted.

    `requested_mm` is what was asked, `applied_mm` what the driver says moved, the two positions
    bracket the move, `provenance` says how the position is known, `drift_mm` is the shortfall.
    A default here is not a small omission: the tab has no other source for "requested vs applied".
    """
    world = SimWorld({"x": 0.0, "y": 0.0, "z": 0.0}, gain=1.0, noise_mm=0.0)
    lh = make_lh(world)

    out, ctx = nudge(lh, Blackboard(), dz=20.0, clamp_mm=25.0)

    assert out.requested_mm == {"x": 0.0, "y": 0.0, "z": 20.0}
    assert out.applied_mm == {"z": 20.0}
    assert out.position_before == {"x": 0.0, "y": 0.0, "z": 0.0}
    assert out.position_after == {"x": 0.0, "y": 0.0, "z": 20.0}
    assert out.provenance == "dead_reckoned"
    assert out.drift_mm == pytest.approx(0.0)
    # Round-trips through validation, so no non-finite float can reach the websocket (B5).
    assert LHMoveOutputs.model_validate(out.model_dump()) == out


def test_a_positive_dz_is_up_and_the_offset_shrinks_for_it():
    """The convention W3 codes against, asserted through the whole stack.

    Reversed, the plan's closing retract becomes a descent into the deck, and the servo loop walks
    away from the tube while reporting success on every step.
    """
    world = SimWorld({"x": 0.0, "y": 0.0, "z": 6.0}, gain=1.0, noise_mm=0.0)
    lh = make_lh(world)

    nudge(lh, Blackboard(), dz=2.0)

    assert world.offset()["z"] == pytest.approx(4.0), "a positive dz must close a positive z offset"
    assert lh.position()["actual"]["z"] == pytest.approx(2.0)


def test_a_dead_reckoned_position_is_warned_about_not_just_reported():
    """R-LH-4/R-LOG-6: reachable on the action, because the loop's gain rests on it."""
    out, ctx = nudge(make_lh(), Blackboard(), dz=1.0)
    assert out.provenance == "dead_reckoned"
    assert "position_dead_reckoned" in codes(ctx)


# --- from_slot: resolving a solved offset into a motion ----------------------------

def test_from_slot_resolves_the_offset_into_a_move():
    """The runner records `inputs["from_slot_value"]` and nothing else — this is the handler's job."""
    world = SimWorld({"x": 3.0, "y": -2.0, "z": 1.0}, gain=1.0, noise_mm=0.0)
    lh = make_lh(world)
    bb = Blackboard()
    put_offset(bb, x=3.0, y=-2.0, z=1.0)

    out, ctx = nudge(lh, bb, from_slot="selected_offset")

    assert out.requested_mm == {"x": 3.0, "y": -2.0, "z": 1.0}
    assert lh.commanded == [{"x": 3.0, "y": -2.0, "z": 1.0}]
    assert world.magnitude_mm() == pytest.approx(0.0, abs=1e-9), \
        "commanding the solved offset must close it, with no negation anywhere"


def test_an_unobserved_axis_is_not_commanded_as_zero():
    """`None` and `0.0` mean opposite things: "cannot see it" versus "it is aligned" (B9)."""
    lh = make_lh()
    bb = Blackboard()
    put_offset(bb, x=2.0)                     # y and z unobserved

    out, ctx = nudge(lh, bb, from_slot="selected_offset")

    assert lh.commanded == [{"x": 2.0}], "an unobserved axis must not be moved"
    assert out.requested_mm == {"x": 2.0}, "nor appear in the request as a zero"
    assert "unobserved_axes" in codes(ctx)


def test_a_refused_solve_commands_nothing_and_is_not_a_failed_step():
    """A refusal is the road to `stalled`, not to a broken engine (D17/D18, R-VIS-7)."""
    lh = make_lh()
    bb = Blackboard()
    bb.set("selected_offset", OffsetOutputs(method="refused", refusal="low_observability"))

    out, ctx = nudge(lh, bb, from_slot="selected_offset")

    assert lh.commanded == []
    assert out.applied_mm == {}
    assert "offset_refused" in codes(ctx)


def test_a_slot_holding_the_wrong_type_raises():
    """A refusal is an outcome; a wiring bug is not, and one that degraded to "moved nothing"
    would be invisible in a loop whose job is to stop moving once it converges."""
    bb = Blackboard()
    bb.set("selected_offset", {"not": "an offset"})
    with pytest.raises(ValueError, match="does not hold a solved offset"):
        nudge(make_lh(), bb, from_slot="selected_offset")


def test_a_converged_offset_is_a_no_op_not_a_failure():
    lh = make_lh()
    bb = Blackboard()
    put_offset(bb, x=0.0, y=0.0, z=0.0)

    out, ctx = nudge(lh, bb, from_slot="selected_offset")

    assert lh.commanded == [] and out.applied_mm == {}
    assert out.position_before == out.position_after


# --- O9 and R-LH-3: bound the step, refuse the envelope ---------------------------

def test_a_step_over_the_clamp_is_refused_rather_than_shortened():
    """O9 bounds one iteration, and `clamp_mm`'s frozen contract says *refused*, not clamped.

    Executing a shortened version would hide the bad solve **and** move the head — and because the
    row would then show a smaller applied number, a detection failure would render as a converging
    loop. Refusing puts the magnitude and the reason on the row instead.
    """
    lh = make_lh()
    bb = Blackboard()
    put_offset(bb, x=40.0)

    with pytest.raises(ValueError, match="refusing rather than clamping"):
        nudge(lh, bb, from_slot="selected_offset", clamp_mm=15.0)
    assert lh.commanded == [], "a refusal must not be a partial move"


def test_the_clamp_measures_the_whole_step_not_one_axis():
    """Three 10 mm components are a 17 mm move; per-axis checks would wave it through."""
    lh = make_lh()
    bb = Blackboard()
    put_offset(bb, x=10.0, y=10.0, z=10.0)

    with pytest.raises(ValueError, match="17.32 mm over the 15 mm"):
        nudge(lh, bb, from_slot="selected_offset", clamp_mm=15.0)


def test_an_out_of_envelope_move_is_refused_with_a_reason(caplog):
    """R-LH-3. Clamping an out-of-envelope request turns a unit bug into a crash into the deck.

    A `dz` of 200 because someone passed micrometres would be quietly executed as the largest legal
    descent, and the record would say the move succeeded. The refusal names the axis, the computed
    target and the bound, which is what makes it actionable rather than merely safe.
    """
    lh = make_lh(envelope={"z": [-10.0, 10.0]})
    bb = Blackboard()

    with pytest.raises(ValueError) as excinfo:
        nudge(lh, bb, dz=20.0, clamp_mm=30.0)

    message = str(excinfo.value)
    assert "outside the configured envelope" in message
    assert "refusing rather than clamping" in message
    assert "[-10, 10]" in message and "z" in message
    assert lh.commanded == [], "nothing may move"

    # And a move that stays inside is still allowed — the bound must not be a blanket refusal.
    out, _ = nudge(lh, bb, dz=5.0)
    assert out.applied_mm == {"z": 5.0}


def test_a_non_finite_delta_is_refused_before_anything_moves():
    """Every `abs(delta) > limit` comparison is False against NaN, so a naive check lets it through
    onto the hardware — and `OutputsBase` would then reject the row it produced."""
    lh = make_lh()
    for bad in (float("nan"), float("inf")):
        action = LHRelative.model_construct(kind="lh.move_relative", device="ot",
                                            dx=bad, dy=0.0, dz=0.0, from_slot=None,
                                            clamp_mm=15.0)
        ctx = make_ctx(action, Devices(ot=lh), Blackboard())
        with pytest.raises(ValueError, match="finite"):
            lh_handler.move_relative(action, ctx)
    assert lh.commanded == []


def test_a_driver_without_relative_motion_raises_rather_than_reporting_success():
    """R-LH-5's failure shape: the one thing worse than refusing is a silent no-op."""
    class NoMotion:
        device_id = "ot"

        def position(self):
            return {"actual": {}, "provenance": {}}

    with pytest.raises(ValueError, match="no relative XYZ motion"):
        nudge(NoMotion(), Blackboard(), dz=1.0)


# --- the servo loop, closed through the world (R-SIM-5) ---------------------------

def _servo(lh, world, iterations: int, *, sign: float = +1.0, clamp_mm: float = 15.0):
    """One servo iteration per pass: read the world, publish it as a solve, command it.

    Standing in for the loop's vision half — a solve that reports the world exactly — because what
    is under test is the *actuation* half: that commanding a solved offset closes it. W3's tests
    cover the measurement half.
    """
    trace = []
    for _ in range(iterations):
        bb = Blackboard()
        put_offset(bb, **{a: sign * v for a, v in world.offset().items()})
        nudge(lh, bb, from_slot="selected_offset", clamp_mm=clamp_mm)
        trace.append(world.magnitude_mm())
    return trace


def test_the_mocks_move_measurably_reduces_the_shared_offset():
    """The single load-bearing behaviour of the mock: it moves the world, it does not fake it."""
    world = SimWorld({"x": 5.0, "y": 0.0, "z": 0.0}, gain=0.8, noise_mm=0.0)
    lh = make_lh(world)
    before = world.magnitude_mm()

    _servo(lh, world, 1)

    assert world.magnitude_mm() < before
    assert world.magnitude_mm() == pytest.approx(1.0), "5 mm commanded at gain 0.8 leaves 1 mm"


def test_repeated_nudges_converge_the_shared_offset():
    """The loop's real bounds: 12 iterations, 1.5 mm threshold, 15 mm clamp."""
    world = SimWorld({"x": 6.0, "y": -4.0, "z": 8.0})
    lh = make_lh(world)

    trace = _servo(lh, world, 12)

    assert trace[-1] < 1.5, f"did not converge under the plan's own bounds: {trace}"
    assert lh.commanded, "converged without commanding anything — the world moved on its own"


def test_an_inverted_world_diverges_through_the_handler_too():
    """The test that gives the one above its meaning.

    A negative gain is the wrong-signed jacobian: every correction moves the head away from the
    tube. If this converged, the convergence test would be measuring the harness rather than the
    code, and the sign bug would still reach the bench with a pipette in it.
    """
    world = SimWorld({"x": 3.0, "y": 0.0, "z": 0.0}, gain=-0.85, noise_mm=0.0)
    lh = make_lh(world)

    # A generous clamp, so the divergence is *observable* rather than immediately refused.
    trace = _servo(lh, world, 4, clamp_mm=60.0)

    assert trace == sorted(trace), f"an inverted world must diverge, got {trace}"
    assert trace[-1] > 3.0, f"expected the offset to grow, got {trace}"

    # And under the plan's real 15 mm clamp the runaway is refused rather than commanded: O9's
    # blast-radius bound is the thing that stops a wrong sign from becoming a 35 mm move.
    bb = Blackboard()
    put_offset(bb, **world.offset())
    with pytest.raises(ValueError, match="refusing rather than clamping"):
        nudge(lh, bb, from_slot="selected_offset", clamp_mm=15.0)


def test_negating_the_solved_offset_diverges():
    """The same failure from the caller's side, and the reason the docstrings say "never negate".

    A handler that commanded `-offset` would look perfectly reasonable in review; this is what
    makes it a test failure instead of a bench discovery.
    """
    world = SimWorld({"x": 3.0, "y": 0.0, "z": 0.0}, gain=0.85, noise_mm=0.0)
    lh = make_lh(world)

    trace = _servo(lh, world, 4, sign=-1.0, clamp_mm=60.0)

    assert trace[-1] > 3.0, f"commanding the negated offset must not converge, got {trace}"


def test_the_shared_world_is_what_the_mock_moves_by_default():
    """D3: one shared piece of state, so the camera renders what the handler moved."""
    from core.sim import shared_world

    shared_world().reset({"x": 4.0, "y": 0.0, "z": 0.0}, gain=1.0, noise_mm=0.0)
    lh = MockLiquidHandlerDriver("ot", {})       # no `sim_world`: shares the singleton
    lh.connect()
    try:
        nudge(lh, Blackboard(), dx=4.0)
        assert shared_world().offset()["x"] == pytest.approx(0.0, abs=1e-9)
    finally:
        shared_world().reset()


# --- R-LH-2: initialize ------------------------------------------------------------

def test_initialize_wiggles_every_axis_both_ways_and_ends_retracted():
    """R-LH-2, the mock's half. A retract that never happened is a head left over the deck."""
    world = SimWorld({"x": 0.0, "y": 0.0, "z": 0.0}, gain=1.0, noise_mm=0.0)
    lh = make_lh(world, retract_z_mm=25.0)

    report = lh.initialize()

    for axis in ("x", "y", "z"):
        assert report["axes"][axis]["net_mm"] == pytest.approx(0.0), f"{axis} wiggle not net-zero"
        assert report["axes"][axis]["directions"] == ["+", "-"]
    assert report["retract"]["retracted_mm"] == pytest.approx(25.0)
    assert lh.position()["actual"]["z"] == pytest.approx(25.0), "+z is up: ends raised, not lowered"


def test_initialize_never_touches_the_plungers():
    """A and B are the pipette plungers on the real machine, and driving one into its stop is the
    failure that produced this driver's rules. The mock must not grow a path to them by accident."""
    lh = make_lh()
    lh.initialize()

    moved_axes = {axis for move in lh.commanded for axis in move}
    assert moved_axes <= {"x", "y", "z"}, f"initialize touched {moved_axes - {'x', 'y', 'z'}}"
    assert not hasattr(lh, "plunger"), "no plunger surface exists to be driven"


def test_initialize_really_moves_the_world_rather_than_skipping_the_physics():
    """The wiggle goes through `move_relative`, so the simulated head really travels and returns.

    Net-zero by construction rather than by exemption: a wiggle that bypassed the world would be a
    routine that cannot fail, which is the opposite of what an initialization check is for.
    """
    world = SimWorld({"x": 0.0, "y": 0.0, "z": 0.0}, gain=1.0, noise_mm=0.0)
    lh = make_lh(world, retract_z_on_init=False)

    lh.initialize()

    assert world.moves == 6, "three axes, two directions each"
    assert world.offset() == pytest.approx({"x": 0.0, "y": 0.0, "z": 0.0}, abs=1e-9)


def test_retract_z_refuses_a_non_positive_distance():
    """Silently doing nothing when asked to move clear of the deck is worth refusing loudly."""
    lh = make_lh()
    for bad in (0.0, -5.0, float("nan")):
        with pytest.raises(DriverError, match="must be positive"):
            lh.retract_z(bad)


def test_the_mock_refuses_rather_than_clamping_at_the_driver_too():
    """Belt and braces with the handler's own check: both answers are refusals, so the two cannot
    disagree in the dangerous direction."""
    lh = make_lh(max_step_mm={"z": 10.0})
    with pytest.raises(DriverError, match="refusing rather than clamping"):
        lh.move_relative(dz=12.0)
    assert lh.commanded == []
