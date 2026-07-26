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
    world = World(offset=8.0, gain=2.0).install(handlers)
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
