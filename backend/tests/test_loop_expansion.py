"""The servo loop: per-iteration materialization, the bounds, and four honest outcomes.

Materializing every iteration into the flat plan (D8) is what makes "what did the loop see on
iteration 4" answerable at all (R-VIS-8, R-UI-2..5) — iteration 4 is rows, with their own
outputs, artifacts and logs. The price is that a loop can grow the plan, so D14 bounds it three
ways and this file asserts each of them: `max_iterations`, the global materialized ceiling, and
no nested loops.

Termination is the named predicate `offset_within_threshold` on `watch_slot` against
`threshold_mm` (Q4/D16). It terminates on the **remaining offset** and never on inter-view
disagreement, because views that never agree that closely would keep a converged loop running
forever. Outcomes: `converged`, `stalled`, `exhausted`, `aborted`. There is no fifth, and in
particular there is no "still going" — a loop that cannot decide has to say so.
"""
from __future__ import annotations

import threading
import time

import pytest

from backend.app.engine import actions as A
from backend.app.engine import plan as plan_module
from backend.app.engine.plan import Plan
from backend.app.engine.runner import Runner

FLEET = {"left", "right", "ot", "handover_cam"}


class _Devices:
    def get(self, device_id: str):
        if device_id not in FLEET:
            raise KeyError(device_id)
        return object()

    def require_arm(self, device_id: str):
        return self.get(device_id)

    def require_liquid_handler(self, device_id: str):
        return self.get(device_id)

    def is_simulated(self, device_id: str) -> bool:
        return True


def wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


@pytest.fixture
def handlers(monkeypatch):
    def install(kind: str, fn) -> None:
        monkeypatch.setitem(A._HANDLERS, kind, fn)
    return install


@pytest.fixture
def runner_factory(tmp_path):
    made: list[Runner] = []

    def make(actions, **kwargs) -> Runner:
        plan = Plan(actions, name=kwargs.pop("name", "servo"))
        runner = Runner(plan, devices=_Devices(), known_devices=FLEET,
                        artifact_root=str(tmp_path), **kwargs)
        made.append(runner)
        return runner

    yield make
    for runner in made:
        runner.abort()
        runner.join(10.0)


class World:
    """The one piece of shared simulated state a servo loop needs (D3, in miniature).

    The liquid-handler move *decrements* the offset and the solve reports what is left, so the
    loop converges because the commanded moves close it — which is what makes a wrong-sign
    control law a reproducible test failure rather than a bench surprise. Here it stands in for
    `core/sim/world.py`, which belongs to S4.
    """

    def __init__(self, offset: float = 8.0, gain: float = 2.0) -> None:
        self.offset = offset
        self.gain = gain
        self.moves = 0
        self.solves = 0
        self.disagreement: float | None = None
        self.observable = True

    def install(self, handlers) -> "World":
        def move(action, ctx):
            self.moves += 1
            self.offset = max(0.0, self.offset - self.gain)
            return A.LHMoveOutputs(applied_mm={"z": -self.gain})

        def solve(action, ctx):
            self.solves += 1
            magnitude = self.offset if self.observable else None
            outputs = A.OffsetOutputs(
                residual_offset_mm={"x": 0.0, "y": 0.0, "z": magnitude},
                magnitude_mm=magnitude,
                sigma_mm={"x": 0.1, "y": 0.1, "z": 0.3},
                view_disagreement_mm=self.disagreement,
                observed_axes=["x", "y", "z"] if self.observable else [],
                method="tag_3d" if self.observable else "refused")
            ctx.blackboard.set("selected_offset", outputs)
            return outputs

        handlers("lh.move_relative", move)
        handlers("vision.solve_offset", solve)
        return self


def _loop(**kw) -> dict:
    body = kw.pop("body", None) or [{"kind": "lh.move_relative", "device": "ot", "dz": -1.0},
                                    {"kind": "vision.solve_offset"}]
    return {"kind": "control.loop", "body": body, "threshold_mm": kw.pop("threshold_mm", 1.5),
            "max_iterations": kw.pop("max_iterations", 8), **kw}


def _iterations(runner: Runner) -> list:
    return [e for e in runner.sink.events() if e.type == "loop_iteration"]


# --- the converging case -----------------------------------------------------

def test_each_iteration_becomes_real_indexed_rows_with_their_own_outputs(handlers,
                                                                        runner_factory):
    """D8/R-VIS-8. Iteration 4's snapshot has to be a row with its own result, or "what did the
    loop see on iteration 4" is unanswerable and the workflow tab has nothing to show."""
    World(offset=8.0, gain=2.0).install(handlers)
    runner = runner_factory([_loop(threshold_mm=1.5, max_iterations=8)])
    loop_aid = runner.plan.aids()[0]
    assert runner.run_to_completion(timeout=10.0) == "complete"

    outputs = runner.plan.result(loop_aid).outputs
    assert outputs.outcome == "converged"
    assert outputs.iterations == 4 and outputs.materialized == 8
    assert outputs.final_magnitude_mm == 0.0 and outputs.threshold_mm == 1.5

    members = runner.plan.members_of(loop_aid)
    assert len(members) == 8
    assert [m.iteration for m in members] == [1, 1, 2, 2, 3, 3, 4, 4]
    assert {m.parent_aid for m in members} == {loop_aid}
    assert {m.origin for m in members} == {"expand"}
    assert [m.index for m in members] == list(range(1, 9))
    # Each iteration's solve carries the offset *that* iteration saw.
    seen = [runner.plan.result(m.aid).outputs.magnitude_mm
            for m in members if m.kind == "vision.solve_offset"]
    assert seen == [6.0, 4.0, 2.0, 0.0]
    assert all(runner.plan.state(m.aid) == "complete" for m in members)


def test_the_loop_reports_its_metrics_per_iteration(handlers, runner_factory):
    """`loop_iteration` exists so the UI can show iteration 4's numbers without joining across
    its rows, and so the decision to continue is auditable after the fact."""
    World(offset=6.0, gain=2.0).install(handlers)
    runner = runner_factory([_loop(threshold_mm=1.0, max_iterations=6)])
    assert runner.run_to_completion(timeout=10.0) == "complete"
    events = _iterations(runner)
    assert [e.iteration for e in events] == [1, 2, 3]
    assert [e.magnitude_mm for e in events] == [4.0, 2.0, 0.0]
    assert all(e.threshold_mm == 1.0 for e in events)
    assert all(e.improving for e in events)
    assert events[0].sigma_mm == {"x": 0.1, "y": 0.1, "z": 0.3}


def test_the_plan_after_the_loop_still_runs(handlers, runner_factory):
    """The cursor has to step over the whole materialized region, not land back inside it."""
    World(offset=4.0, gain=2.0).install(handlers)
    runner = runner_factory([_loop(threshold_mm=1.0),
                             {"kind": "lh.move_relative", "device": "ot", "dz": 30.0}])
    after = runner.plan.aids()[1]
    assert runner.run_to_completion(timeout=10.0) == "complete"
    assert runner.plan.state(after) == "complete"
    assert runner.plan.by_aid(after).index == len(runner.plan) - 1
    assert runner.cursor == len(runner.plan)


# --- the bounds (D14) --------------------------------------------------------

def test_a_loop_that_never_converges_cannot_grow_the_plan_without_bound(handlers,
                                                                       runner_factory):
    """The requirement in one test. The loop improves every iteration but never reaches the
    threshold, so nothing stops it except `max_iterations` — and the outcome is `exhausted`,
    which is deliberately distinct from `stalled`: still improving is not the same as stuck."""
    World(offset=100.0, gain=0.5).install(handlers)
    runner = runner_factory([_loop(threshold_mm=1.0, max_iterations=6)])
    loop_aid = runner.plan.aids()[0]
    assert runner.run_to_completion(timeout=15.0) == "failed"

    outputs = runner.plan.result(loop_aid).outputs
    assert outputs.outcome == "exhausted"
    assert outputs.iterations == 6
    assert outputs.materialized == 12
    assert len(runner.plan) == 1 + 12, "bounded at body x max_iterations, and no more"
    assert runner.plan.result(loop_aid).status == "failed"
    assert runner.plan.result(loop_aid).error.type == "LoopNotConverged"


def test_the_global_ceiling_ends_the_loop_as_exhausted_rather_than_growing_the_plan(
        handlers, runner_factory, monkeypatch):
    """D14's second bound, which is the one that matters for OOM: `max_iterations` is per loop
    and a plan may hold several. The ceiling is patched down here so the test is fast; the real
    constant is asserted against in `test_plan.py`.
    """
    monkeypatch.setattr(plan_module, "MAX_MATERIALIZED_ACTIONS", 6)
    World(offset=100.0, gain=0.5).install(handlers)
    runner = runner_factory([_loop(threshold_mm=1.0, max_iterations=20)])
    loop_aid = runner.plan.aids()[0]
    assert runner.run_to_completion(timeout=15.0) == "failed"
    outputs = runner.plan.result(loop_aid).outputs
    assert outputs.outcome == "exhausted"
    assert outputs.materialized == 6
    assert runner.plan.materialized_count() == 6


def test_a_stalled_loop_stops_after_the_configured_run_of_no_progress(handlers,
                                                                     runner_factory):
    """R-VIS-7's third outcome. A loop making no progress must stop saying so — never spin."""
    World(offset=8.0, gain=0.0).install(handlers)
    runner = runner_factory([_loop(threshold_mm=1.0, max_iterations=20,
                                   no_progress_abort=3)])
    loop_aid = runner.plan.aids()[0]
    assert runner.run_to_completion(timeout=15.0) == "failed"
    outputs = runner.plan.result(loop_aid).outputs
    assert outputs.outcome == "stalled"
    assert outputs.iterations == 4, "one improving iteration, then three without progress"
    assert [e.improving for e in _iterations(runner)] == [True, False, False, False]


def test_noise_below_the_epsilon_does_not_count_as_progress(handlers, runner_factory):
    """Otherwise `no_progress_abort` is a bound that never binds: a detector that jitters by
    0.01 mm would read as forever improving."""
    World(offset=8.0, gain=0.01).install(handlers)
    runner = runner_factory([_loop(threshold_mm=1.0, max_iterations=20,
                                   no_progress_abort=2)])
    assert runner.run_to_completion(timeout=15.0) == "failed"
    outputs = runner.plan.result(runner.plan.aids()[0]).outputs
    assert outputs.outcome == "stalled" and outputs.iterations == 3


def test_a_nested_loop_cannot_be_authored():
    """D14, refused at validation so an LLM-authored plan is rejected with a reason instead of
    silently truncated at runtime."""
    with pytest.raises(Exception) as exc:
        Plan([_loop(body=[_loop()])])
    assert "nested" in str(exc.value).lower()


def test_a_loop_cannot_be_injected_into_a_running_loops_iteration(handlers, runner_factory):
    """The route no validator sees. The body was fine when it was authored; the loop arrives
    afterwards, by injection, into a materialized iteration."""
    entered = threading.Event()
    release = threading.Event()
    world = World(offset=8.0, gain=2.0)
    world.install(handlers)

    def slow_move(action, ctx):
        entered.set()
        assert release.wait(10.0)
        world.offset = max(0.0, world.offset - world.gain)
        return A.LHMoveOutputs(applied_mm={"z": -world.gain})

    handlers("lh.move_relative", slow_move)
    runner = runner_factory([_loop(threshold_mm=1.0)])
    loop_aid = runner.plan.aids()[0]
    runner.start()
    assert entered.wait(5.0)
    first_member = runner.plan.members_of(loop_aid)[0]
    with pytest.raises(Exception) as exc:
        runner.inject(_loop(), after_aid=first_member.aid)
    assert "loop" in str(exc.value).lower()
    release.set()


# --- what terminates it, and what deliberately does not ----------------------

def test_the_loop_terminates_on_the_remaining_offset_not_on_view_disagreement(handlers,
                                                                             runner_factory):
    """Q4/D16. `view_disagreement_mm` is advisory: gating on it can leave a perfectly converged
    loop running forever because the views never agree that closely."""
    world = World(offset=2.0, gain=2.0).install(handlers)
    world.disagreement = 50.0                     # the views wildly disagree...
    runner = runner_factory([_loop(threshold_mm=1.5, max_iterations=4)])
    assert runner.run_to_completion(timeout=10.0) == "complete"
    outputs = runner.plan.result(runner.plan.aids()[0]).outputs
    assert outputs.outcome == "converged", "...and the remaining offset is what decides"
    assert outputs.iterations == 1


def test_an_unobserved_offset_is_not_a_converged_one(handlers, runner_factory):
    """`None` and `0.0` mean opposite things to a servo loop (R-VIS-4). An axis nobody can see
    must never read as an axis that is already aligned."""
    world = World(offset=8.0, gain=2.0).install(handlers)
    world.observable = False
    runner = runner_factory([_loop(threshold_mm=1.5, max_iterations=10,
                                   no_progress_abort=2)])
    assert runner.run_to_completion(timeout=10.0) == "failed"
    outputs = runner.plan.result(runner.plan.aids()[0]).outputs
    assert outputs.outcome == "stalled"
    assert outputs.final_magnitude_mm is None
    assert [e.magnitude_mm for e in _iterations(runner)] == [None, None]


def test_an_axis_that_was_never_observed_cannot_converge_the_loop(handlers, runner_factory):
    """B9. `magnitude_mm` is documented as "over the observed axes only", and nothing checked
    `observed_axes` — so a solve that saw x and y and reported 1.2 mm with z unobservable
    terminated the loop as `converged`. That is R-VIS-4's failure mode in the one place the
    design did not guard it: the side view occluded by the jaws mid-approach (D27), the liquid
    handler commanding no z, and the tab showing success for a height nobody measured."""
    def solve(action, ctx):
        outputs = A.OffsetOutputs(
            residual_offset_mm={"x": 0.8, "y": 0.9, "z": None},
            magnitude_mm=1.2,                       # a partial norm, under the threshold
            sigma_mm={"x": 0.1, "y": 0.1},
            observed_axes=["x", "y"], method="axis_decoupled_jacobian")
        ctx.blackboard.set("selected_offset", outputs)
        return outputs

    handlers("lh.move_relative", lambda a, c: A.LHMoveOutputs(applied_mm={"z": 0.0}))
    handlers("vision.solve_offset", solve)
    runner = runner_factory([_loop(threshold_mm=1.5, max_iterations=10, no_progress_abort=2)])
    loop_aid = runner.plan.aids()[0]
    assert runner.run_to_completion(timeout=10.0) == "failed"

    result = runner.plan.result(loop_aid)
    assert result.outputs.outcome == "stalled", "not converged, and not a crash either"
    assert result.outputs.final_magnitude_mm is None
    assert [e.magnitude_mm for e in _iterations(runner)] == [None, None]
    # With a stated reason, once — not once per iteration.
    warned = [w for w in result.warnings if w.code == "unobserved_axis"]
    assert len(warned) == 1 and "z" in warned[0].message
    # sigma still travels, so the operator can see how well the axes that *were* seen are known.
    assert _iterations(runner)[0].sigma_mm == {"x": 0.1, "y": 0.1}


def test_a_magnitude_is_trusted_when_every_corrected_axis_is_observed(handlers,
                                                                     runner_factory):
    """The other side of B9: the gate must not stall a loop whose solve did see all three axes,
    which is the normal case and the one every existing convergence test depends on."""
    World(offset=2.0, gain=2.0).install(handlers)
    runner = runner_factory([_loop(threshold_mm=1.5, max_iterations=4)])
    assert runner.run_to_completion(timeout=10.0) == "complete"
    result = runner.plan.result(runner.plan.aids()[0])
    assert result.outputs.outcome == "converged"
    assert [w.code for w in result.warnings] == []


def test_a_per_camera_watch_slot_warns_rather_than_guessing_which_view_to_believe(
        handlers, runner_factory):
    """The three per-camera slots hold one value per view and no single magnitude. Picking one
    is the "best single view" strategy D15 deleted, because it guarantees a stalled loop."""
    World(offset=8.0, gain=2.0).install(handlers)
    runner = runner_factory([_loop(threshold_mm=1.5, max_iterations=10,
                                   no_progress_abort=1, watch_slot="tip")])
    loop_aid = runner.plan.aids()[0]
    assert runner.run_to_completion(timeout=10.0) == "failed"
    result = runner.plan.result(loop_aid)
    assert result.outputs.outcome == "stalled"
    assert [w.code for w in result.warnings] == ["watch_slot_per_camera"]


# --- the blackboard between iterations ---------------------------------------
#
# B2. `blackboard.clear` documented that a loop iteration clears the per-camera slots, and no
# caller existed. The consequence the review reproduced (probe 8) is the oscillation the
# freshness contract is for: an iteration whose capture did not happen solved against the
# previous iteration's frame and re-commanded an offset that had already been applied.


def test_each_iteration_starts_with_the_per_camera_slots_and_the_watch_slot_empty(
        handlers, runner_factory):
    """The invariant, asserted directly. It has to be structural: a contract every handler must
    remember is one that a handler returning early on `found=False` breaks silently."""
    seen: list = []

    def snapshot(action, ctx):
        seen.append((ctx.blackboard.peek("frame", device=action.device),
                     ctx.blackboard.peek("tip", device=action.device),
                     ctx.blackboard.peek("selected_offset")))
        ctx.blackboard.set("frame", {"n": len(seen)}, device=action.device)
        ctx.blackboard.set("tip", {"n": len(seen)}, device=action.device)
        return A.SnapshotOutputs(width=4, height=4, captured_at="now")

    World(offset=8.0, gain=2.0).install(handlers)
    handlers("camera.snapshot", snapshot)
    runner = runner_factory([_loop(threshold_mm=1.5, max_iterations=8,
                                   body=[{"kind": "camera.snapshot",
                                          "device": "handover_cam"},
                                         {"kind": "lh.move_relative", "device": "ot",
                                          "dz": -1.0},
                                         {"kind": "vision.solve_offset"}])])
    loop_aid = runner.plan.aids()[0]
    assert runner.run_to_completion(timeout=10.0) == "complete"
    assert len(seen) == 4, "four iterations, and every one of them started from empty"
    assert seen == [(None, None, None)] * 4
    # The last iteration's value still survives the loop, for anything after it to read.
    assert runner.blackboard.peek("selected_offset") is not None
    assert runner.plan.result(loop_aid).outputs.outcome == "converged"


def test_an_iteration_whose_capture_failed_cannot_solve_against_the_previous_frame(
        handlers, runner_factory):
    """Probe 8, and the reason this matters rather than merely being untidy. The capture is
    marked `on_failure="continue"`, so the iteration carries on after it fails — and the solve
    must then find nothing rather than compute a plausible offset from the last iteration's
    frame and hand the liquid handler a correction it has already applied."""
    world = World(offset=8.0, gain=2.0)
    captures = {"n": 0}

    def snapshot(action, ctx):
        captures["n"] += 1
        if captures["n"] == 2:
            raise RuntimeError("the camera child did not answer the snapshot request")
        # The frame carries the offset visible at capture time, which is what makes a stale
        # frame a *wrong number* rather than merely an old one.
        ctx.blackboard.set("frame", {"offset": world.offset}, device=action.device)
        return A.SnapshotOutputs(width=4, height=4, captured_at="now")

    def solve(action, ctx):
        frame = ctx.blackboard.get("frame", device="handover_cam")   # SlotEmpty when skipped
        outputs = A.OffsetOutputs(
            residual_offset_mm={"x": 0.0, "y": 0.0, "z": frame["offset"]},
            magnitude_mm=frame["offset"], observed_axes=["x", "y", "z"], method="tag_3d")
        ctx.blackboard.set("selected_offset", outputs)
        return outputs

    def move(action, ctx):
        world.moves += 1
        world.offset = max(0.0, world.offset - world.gain)
        return A.LHMoveOutputs(applied_mm={"z": -world.gain})

    handlers("camera.snapshot", snapshot)
    handlers("vision.solve_offset", solve)
    handlers("lh.move_relative", move)
    runner = runner_factory([_loop(threshold_mm=1.5, max_iterations=8,
                                   body=[{"kind": "camera.snapshot",
                                          "device": "handover_cam",
                                          "on_failure": "continue"},
                                         {"kind": "vision.solve_offset"},
                                         {"kind": "lh.move_relative", "device": "ot",
                                          "dz": -1.0}])])
    loop_aid = runner.plan.aids()[0]
    assert runner.run_to_completion(timeout=10.0) == "failed"

    rows = {(r.iteration, r.kind): r for r in runner.plan.rows() if r.parent_aid == loop_aid}
    assert rows[(2, "camera.snapshot")].state == "failed"
    solve_2 = rows[(2, "vision.solve_offset")]
    assert solve_2.state == "failed", "no solve against iteration 1's frame"
    assert solve_2.result.error.type == "SlotEmpty"
    assert rows[(2, "lh.move_relative")].state == "planned"
    assert world.moves == 1, "iteration 1's offset was never commanded a second time"
    assert runner.plan.result(loop_aid).outputs.iterations == 2


# --- pause, abort and injection inside a loop --------------------------------

def test_a_pause_inside_a_loop_lands_between_iterations_rows(handlers, runner_factory):
    """The loop body is exactly where a pause is most likely to be pressed, so the gate has to
    be checked between the rows of an iteration and not only around the whole loop."""
    entered = [threading.Event() for _ in range(20)]
    release = [threading.Event() for _ in range(20)]
    world = World(offset=20.0, gain=2.0)
    world.install(handlers)
    calls = {"n": 0}

    def gated_move(action, ctx):
        i = calls["n"]
        calls["n"] += 1
        entered[i].set()
        assert release[i].wait(10.0)
        world.offset = max(0.0, world.offset - world.gain)
        return A.LHMoveOutputs(applied_mm={"z": -world.gain})

    handlers("lh.move_relative", gated_move)
    runner = runner_factory([_loop(threshold_mm=1.0, max_iterations=10)])
    loop_aid = runner.plan.aids()[0]
    runner.start()
    assert entered[0].wait(5.0)
    assert runner.pause() is True
    assert runner.state == "pausing"
    release[0].set()
    assert wait_until(lambda: runner.state == "paused")
    assert not entered[1].is_set(), "the next iteration's move did not start"
    assert runner.plan.state(loop_aid) == "running", "the loop itself has not finished"
    # The move finished and the run stopped at the *next row's* boundary — inside iteration 1,
    # before its solve. That is the finest granularity D9 offers without a handler cooperating.
    assert world.solves == 0
    assert runner.plan.next_unrun_in_region(loop_aid).kind == "vision.solve_offset"

    for event in release:
        event.set()
    runner.resume()
    assert wait_until(lambda: runner.state == "complete", timeout=15.0)
    assert runner.plan.result(loop_aid).outputs.outcome == "converged"


def test_injecting_into_an_iteration_that_already_ran_is_refused_as_the_past(handlers,
                                                                            runner_factory):
    """B3. While the run is paused between two rows of an iteration, `running_aid` is the
    **loop** — whose index is before its whole materialized region — so the running-action rule
    alone accepts every row of every finished iteration. Reproduced: paused in iteration 3, an
    injection after iteration 1's first row was accepted, labelled `iteration=1`, and executed
    *before* iteration 3's pending row, with the cursor jumping backwards.

    The positive half of §1.5's truth table is asserted here too, because the fix must not close
    the case it exists for: an injection into the **current** iteration is still accepted and
    still runs before that iteration's remaining rows.
    """
    world = World(offset=40.0, gain=2.0)
    world.install(handlers)
    box: list = []

    def pausing_move(action, ctx):
        if action.iteration == 3:
            box[0].pause()
        world.offset = max(0.0, world.offset - world.gain)
        return A.LHMoveOutputs(applied_mm={"z": -world.gain})

    handlers("lh.move_relative", pausing_move)
    runner = runner_factory([_loop(threshold_mm=1.0, max_iterations=8)])
    box.append(runner)
    loop_aid = runner.plan.aids()[0]
    runner.start()
    assert wait_until(lambda: runner.state == "paused", timeout=10.0)
    assert runner.running_aid == loop_aid, "the loop, not the row — this is why B3 was possible"
    members = runner.plan.members_of(loop_aid)
    assert [m.iteration for m in members] == [1, 1, 2, 2, 3, 3]

    with pytest.raises(plan_module.InjectRefused) as exc:
        runner.inject({"kind": "arm.gripper", "device": "right", "state": "open"},
                      after_aid=members[0].aid)
    assert "past" in exc.value.reason
    assert len(runner.plan) == 7, "nothing was inserted into iteration 1"
    assert [e.reason for e in runner.sink.events()
            if e.type == "inject_rejected"] == [exc.value.reason]

    # The current iteration still accepts one, and it runs before that iteration's solve.
    at_cursor = runner.plan.at(runner.cursor)
    assert at_cursor.iteration == 3
    injected = runner.inject({"kind": "control.checkpoint", "message": "look"},
                             after_aid=at_cursor.aid)[0]
    assert injected.parent_aid == loop_aid and injected.iteration == 3
    assert runner.plan.next_unrun_in_region(loop_aid).aid == injected.aid


def test_an_abort_inside_a_loop_is_aborted_and_not_a_failed_loop(handlers, runner_factory):
    entered = threading.Event()
    release = threading.Event()
    world = World(offset=20.0, gain=2.0)
    world.install(handlers)

    def gated_move(action, ctx):
        entered.set()
        assert release.wait(10.0)
        ctx.checkpoint()
        return A.LHMoveOutputs(applied_mm={"z": -world.gain})

    handlers("lh.move_relative", gated_move)
    runner = runner_factory([_loop(threshold_mm=1.0)])
    loop_aid = runner.plan.aids()[0]
    runner.start()
    assert entered.wait(5.0)
    runner.abort()
    release.set()
    assert wait_until(lambda: runner.state == "aborted")
    result = runner.plan.result(loop_aid)
    assert result.status == "aborted" and result.outputs.outcome == "aborted"


def test_a_failing_row_inside_a_loop_halts_the_run_with_the_loop_recorded(handlers,
                                                                         runner_factory):
    world = World(offset=8.0, gain=2.0)
    world.install(handlers)

    def bad_move(action, ctx):
        raise RuntimeError("deck envelope refused the move")

    handlers("lh.move_relative", bad_move)
    runner = runner_factory([_loop(threshold_mm=1.0),
                             {"kind": "lh.move_relative", "device": "ot", "dz": 30.0}])
    loop_aid, after = runner.plan.aids()
    assert runner.run_to_completion(timeout=10.0) == "failed"
    result = runner.plan.result(loop_aid)
    assert result.status == "failed"
    assert "inside the loop failed" in result.error.message
    assert runner.plan.state(after) == "planned", "the run halted rather than carrying on"
    # And the failing row itself is on the plan, with its own error, ready to be injected past.
    failed_rows = [r for r in runner.plan.rows() if r.state == "failed"]
    assert any(r.parent_aid == loop_aid for r in failed_rows)


# --- recovering from a failure *inside* the loop ------------------------------
#
# The review reproduced both halves of this and both were unacceptable: injecting a fix after
# the failed step was refused as "in the past" — the single headline promise of the feature
# (D22) — and `resume()` skipped the rest of the loop and executed the action *after* it, which
# on the handover plan retracts the pipette while the tube may still be misaligned.


def _three_row_loop(**kw) -> dict:
    """A loop body shaped like the real servo loop: capture, correct, measure."""
    return _loop(body=[{"kind": "camera.snapshot", "device": "handover_cam"},
                       {"kind": "lh.move_relative", "device": "ot", "dz": -1.0},
                       {"kind": "vision.solve_offset"}], **kw)


def _failing_in_iteration(world: World, handlers, iteration: int, ran: list) -> None:
    """Trace every row of the body, and make `lh.move_relative` raise on one iteration.

    `ran` records `(kind, iteration)` per call, which is the only way to tell "the loop carried
    on" from "the run walked out of the region and did the next thing".
    """
    def snapshot(action, ctx):
        ran.append((action.kind, action.iteration))
        ctx.blackboard.set("frame", {"path": "f.png"}, device=action.device)
        return A.SnapshotOutputs(width=4, height=4, captured_at="now")

    def move(action, ctx):
        ran.append((action.kind, action.iteration))
        if action.iteration == iteration:
            raise RuntimeError("deck envelope refused the move")
        world.moves += 1
        world.offset = max(0.0, world.offset - world.gain)
        return A.LHMoveOutputs(applied_mm={"z": -world.gain})

    solve = A._HANDLERS["vision.solve_offset"]

    def traced_solve(action, ctx):
        ran.append((action.kind, action.iteration))
        return solve(action, ctx)

    handlers("camera.snapshot", snapshot)
    handlers("lh.move_relative", move)
    handlers("vision.solve_offset", traced_solve)


def test_a_failure_inside_a_loop_leaves_the_cursor_on_the_failed_row(handlers,
                                                                     runner_factory):
    """B4's first half. The cursor is where the run stopped, and where the run stopped is the
    row that failed — not the end of the loop's region, and certainly not the retract that
    follows it. Everything else about recovery follows from this: `refusal_for_insert` compares
    against the cursor, and so does `resume()`."""
    world = World(offset=8.0, gain=2.0).install(handlers)
    ran: list = []
    _failing_in_iteration(world, handlers, 2, ran)
    runner = runner_factory([_three_row_loop(threshold_mm=1.5, max_iterations=8),
                             {"kind": "lh.move_relative", "device": "ot", "dz": 40.0}])
    loop_aid, retract_aid = runner.plan.aids()
    assert runner.run_to_completion(timeout=10.0) == "failed"

    failed = [r for r in runner.plan.rows() if r.state == "failed" and r.parent_aid == loop_aid]
    assert [r.iteration for r in failed] == [2], "iteration 2's move is the one that failed"
    start, end = runner.plan.region_of(loop_aid)
    assert runner.cursor == failed[0].index
    assert start <= runner.cursor < end, "inside the region, not past it"
    assert runner.plan.state(retract_aid) == "planned", "the retract was not reached"

    # And therefore the one injection that matters is accepted rather than refused as "in the
    # past" (D22): a fix immediately after the step that failed.
    injected = runner.inject({"kind": "camera.snapshot", "device": "handover_cam"},
                             after_aid=failed[0].aid)[0]
    assert injected.index == runner.cursor + 1
    assert injected.parent_aid == loop_aid and injected.iteration == 2, "part of iteration 2"


def test_resuming_a_mid_loop_failure_continues_the_loop_instead_of_running_past_it(
        handlers, runner_factory):
    """B4's second half, and the dangerous one. An operator sees a red row inside the loop and
    presses the Resume the design offers. The answer must be "carry on with the loop", not
    "skip the rest of it and retract the pipette"."""
    world = World(offset=8.0, gain=2.0).install(handlers)
    ran: list = []
    _failing_in_iteration(world, handlers, 2, ran)
    runner = runner_factory([_three_row_loop(threshold_mm=1.5, max_iterations=8),
                             {"kind": "lh.move_relative", "device": "ot", "dz": 40.0}])
    loop_aid, retract_aid = runner.plan.aids()
    assert runner.run_to_completion(timeout=10.0) == "failed"
    assert ran == [("camera.snapshot", 1), ("lh.move_relative", 1), ("vision.solve_offset", 1),
                   ("camera.snapshot", 2), ("lh.move_relative", 2)]

    failed = [r for r in runner.plan.rows()
              if r.state == "failed" and r.parent_aid == loop_aid][0]
    injected = runner.inject({"kind": "camera.snapshot", "device": "handover_cam"},
                             after_aid=failed.aid)[0]

    assert runner.resume() is True
    assert wait_until(lambda: runner.state in ("failed", "complete"), timeout=15.0)

    # The fix ran, then the rest of iteration 2, then further iterations — and the retract
    # after the loop ran last, once the loop had actually converged.
    assert runner.plan.state(injected.aid) == "complete"
    assert ran[5:8] == [("camera.snapshot", 2), ("vision.solve_offset", 2),
                        ("camera.snapshot", 3)]
    assert [r for r in ran if r[1] is None] == [("lh.move_relative", None)], \
        "the retract ran exactly once, and only after the loop"
    assert ran[-1] == ("lh.move_relative", None)
    outputs = runner.plan.result(loop_aid).outputs
    assert outputs.outcome == "converged"
    assert outputs.iterations >= 4, "the loop kept iterating after the recovery"
    assert runner.plan.state(retract_aid) == "complete"
    assert runner.cursor == len(runner.plan)


def test_a_resumed_loop_does_not_re_materialize_the_iteration_it_stopped_in(handlers,
                                                                            runner_factory):
    """Re-entering `_run_loop` must rebuild its iteration counter from the rows that exist.
    Starting from zero again would materialize a second copy of iteration 1, and every bound
    D14 places on the loop would be counted from the wrong base."""
    world = World(offset=8.0, gain=2.0).install(handlers)
    ran: list = []
    _failing_in_iteration(world, handlers, 2, ran)
    runner = runner_factory([_three_row_loop(threshold_mm=1.5, max_iterations=8)])
    loop_aid = runner.plan.aids()[0]
    assert runner.run_to_completion(timeout=10.0) == "failed"
    assert [m.iteration for m in runner.plan.members_of(loop_aid)] == [1, 1, 1, 2, 2, 2]

    assert runner.resume() is True
    assert wait_until(lambda: runner.state in ("failed", "complete"), timeout=15.0)
    iterations = [m.iteration for m in runner.plan.members_of(loop_aid)]
    assert iterations == sorted(iterations), "the region stays ordered by iteration"
    assert iterations.count(1) == 3 and iterations.count(2) == 3, "no duplicate iteration"
    outputs = runner.plan.result(loop_aid).outputs
    assert outputs.iterations == max(iterations)
    assert outputs.materialized == len(iterations)


def test_an_action_injected_into_the_running_iteration_is_executed_by_it(handlers,
                                                                        runner_factory):
    """Not stepped over. An accepted injection that never runs is a silent skip (R-ENG-17), and
    an action injected into iteration 1 belongs to iteration 1 — the UI nests it there."""
    entered = threading.Event()
    release = threading.Event()
    world = World(offset=4.0, gain=2.0)
    world.install(handlers)
    ran: list[int] = []

    def gated_move(action, ctx):
        ran.append(action.aid)
        if action.dz == -1.0 and not entered.is_set():
            entered.set()
            assert release.wait(10.0)
        world.offset = max(0.0, world.offset - world.gain)
        return A.LHMoveOutputs(applied_mm={"z": -world.gain})

    handlers("lh.move_relative", gated_move)
    runner = runner_factory([_loop(threshold_mm=1.0, max_iterations=4)])
    loop_aid = runner.plan.aids()[0]
    runner.start()
    assert entered.wait(5.0)
    first_member = runner.plan.members_of(loop_aid)[0]
    injected = runner.inject({"kind": "control.checkpoint", "message": "look"},
                             after_aid=first_member.aid)[0]
    assert injected.parent_aid == loop_aid and injected.iteration == 1
    release.set()

    # The injected checkpoint pauses the run from inside the iteration — which is the point:
    # it is a boundary the operator chose, in the middle of a loop.
    assert wait_until(lambda: runner.state == "paused")
    assert runner.plan.state(injected.aid) == "running"
    runner.resume()
    assert wait_until(lambda: runner.state == "complete", timeout=15.0)
    assert runner.plan.result(injected.aid).outputs.acknowledged is True
    assert runner.plan.result(loop_aid).outputs.outcome == "converged"
