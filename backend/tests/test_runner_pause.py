"""The runner: pause, abort, failure classification, the wrapper, and reconnect.

Pause is the control that must never be wrong, so most of this file is about the difference
between `pausing` and `paused` (D9/R-ENG-8). A UI claiming an instant stop while a
multi-second move finishes is lying about where the arm is; a UI stuck on "pausing…" while a
handler has actually parked is lying the other way. Both are tested here.

The handlers in this module are **test-only and installed by monkeypatching the registry
dict**, never through `@handler`. Two reasons: `@handler` refuses a duplicate registration on
purpose (that is a merge conflict it exists to catch), and the real handlers for these kinds
belong to other slices — S1 must be provable without them.
"""
from __future__ import annotations

import threading
import time

import pytest

from backend.app.engine import actions as A
from backend.app.engine.context import ActionAborted
from backend.app.engine.plan import Plan
from backend.app.engine.runner import (DeviceBusy, NoHandlerError, Runner, StartRefused,
                                       device_claims)
from core.obs import log as obs

FLEET = {"left", "right", "ot", "handover_cam"}


# --- harness -----------------------------------------------------------------

class _Devices:
    """The `DeviceAccess` slice a handler may use, stubbed.

    A Protocol on purpose (see `context.py`), so a runner test needs no driver, no fleet and
    no possibility of reaching an instrument.
    """

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


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


@pytest.fixture
def handlers(monkeypatch):
    """Install a handler for one kind, restoring the registry afterwards.

    `monkeypatch.setitem` rather than `@handler`: the decorator refuses a duplicate, and the
    real handler for a kind belongs to whichever slice owns that device. This keeps a test
    override from ever being the collision the guard is there to detect.
    """
    def install(kind: str, fn) -> None:
        monkeypatch.setitem(A._HANDLERS, kind, fn)
    return install


@pytest.fixture
def runner_factory(tmp_path):
    """A `Runner` over a fresh plan, with artifacts pointed somewhere harmless.

    Teardown aborts and joins, so a test that fails while a worker is parked on the pause gate
    cannot leave a thread behind for the next test to inherit.
    """
    made: list[Runner] = []

    def make(actions, **kwargs) -> Runner:
        plan = Plan(actions, name=kwargs.pop("name", "test"))
        runner = Runner(plan, devices=_Devices(), known_devices=FLEET,
                        artifact_root=str(tmp_path), **kwargs)
        made.append(runner)
        return runner

    yield make
    for runner in made:
        runner.abort()
        runner.join(5.0)


def _move(dz: float = 1.0, **kw) -> dict:
    return {"kind": "lh.move_relative", "device": "ot", "dz": dz, **kw}


def _lh_outputs(action) -> A.LHMoveOutputs:
    return A.LHMoveOutputs(applied_mm={"z": action.dz}, requested_mm={"z": action.dz})


def _states(runner: Runner) -> list[str]:
    return [e.state for e in runner.sink.events() if e.type == "run_state"]


# --- pause at the action boundary --------------------------------------------

def test_pause_during_a_blocking_move_stops_at_the_next_boundary(handlers, runner_factory):
    """The headline requirement (R-ENG-8/D9). Three properties, in order:

    * the state is `pausing` while the move in flight finishes — not `paused`, because the arm
      is still travelling, and not a mid-trajectory stop, because that is abort;
    * the run stops *before* the next action starts;
    * resume continues from there.
    """
    entered = [threading.Event() for _ in range(3)]
    release = [threading.Event() for _ in range(3)]

    def move(action, ctx):
        i = int(action.dz)
        entered[i].set()
        assert release[i].wait(10.0), "handler stood in for move_joints(wait=True)"
        return _lh_outputs(action)

    handlers("lh.move_relative", move)
    runner = runner_factory([_move(0), _move(1), _move(2)])
    aids = runner.plan.aids()
    runner.start()

    assert entered[0].wait(5.0)
    assert runner.pause() is True
    assert runner.state == "pausing", "the move in flight has not finished yet"
    assert not entered[1].is_set()

    release[0].set()
    assert wait_until(lambda: runner.state == "paused")
    assert not entered[1].is_set(), "stopped at the boundary, before the next action began"
    assert runner.plan.state(aids[0]) == "complete"
    assert runner.plan.state(aids[1]) == "planned"
    assert runner.cursor == 1

    assert "pausing" in _states(runner)
    assert _states(runner).index("pausing") < _states(runner).index("paused")
    paused = [e for e in runner.sink.events() if e.type == "run_paused"]
    assert paused and paused[-1].reason == "operator"

    release[1].set()
    release[2].set()
    assert runner.resume() is True
    assert entered[2].wait(5.0)
    assert wait_until(lambda: runner.state == "complete")
    assert runner.plan.completed() == 3


def test_the_move_in_flight_runs_to_completion_rather_than_being_cut_short(
        handlers, runner_factory):
    """Pause is deliberately not `driver.stop()`. A real move takes seconds and the runner has
    no business interrupting it — the record must show the action completed."""
    finished = []

    def move(action, ctx):
        time.sleep(0.3)                     # a commanded move the controller is executing
        finished.append(action.aid)
        return _lh_outputs(action)

    handlers("lh.move_relative", move)
    runner = runner_factory([_move(0), _move(1)])
    runner.start()
    assert wait_until(lambda: runner.state == "running")
    assert runner.pause() is True
    assert wait_until(lambda: runner.state == "paused")
    assert finished == [runner.plan.aids()[0]]
    assert runner.plan.result(finished[0]).status == "complete"
    assert runner.plan.result(finished[0]).duration_ms >= 250


def test_pause_returns_false_when_there_is_nothing_to_pause(handlers, runner_factory):
    handlers("lh.move_relative", lambda a, c: _lh_outputs(a))
    runner = runner_factory([_move()])
    assert runner.pause() is False
    assert runner.run_to_completion(timeout=5.0) == "complete"
    assert runner.pause() is False


# --- pause inside an action, through ctx.checkpoint ---------------------------

def test_checkpoint_lets_a_handler_pause_between_its_own_sub_steps(handlers, runner_factory):
    """D9's other half. A decap ratchet or a traverse has natural boundaries of its own, and
    `ctx.checkpoint()` is how a handler cooperates at them.

    The moment the handler parks the state becomes `paused`, not `pausing`: nothing is moving,
    and reporting otherwise would be the mirror-image lie.
    """
    reached = [threading.Event() for _ in range(4)]
    proceed = [threading.Event() for _ in range(4)]

    def ratchet(action, ctx):
        for bite in range(4):
            ctx.checkpoint()               # between bites, never mid-bite
            ctx.progress(f"bite {bite + 1}/4")
            reached[bite].set()
            assert proceed[bite].wait(10.0)
        return _lh_outputs(action)

    handlers("lh.move_relative", ratchet)
    runner = runner_factory([_move(0)])
    aid = runner.plan.aids()[0]
    runner.start()

    assert reached[0].wait(5.0)
    runner.pause()
    proceed[0].set()
    assert wait_until(lambda: runner.state == "paused"), "parked in checkpoint, so: paused"
    assert not reached[1].is_set()
    assert runner.plan.state(aid) == "running", "the action itself has not finished"

    for event in proceed:
        event.set()
    assert runner.resume() is True
    assert wait_until(lambda: runner.state == "complete")
    assert reached[3].is_set()
    logs = [e for e in runner.sink.events() if e.type == "action_log"]
    assert len(logs) == 4 and logs[0].aid == aid


def test_checkpoint_raises_on_abort_and_that_is_not_a_failure(handlers, runner_factory):
    """R-ENG-10. `ActionAborted` is `status="aborted"`, never `"failed"` — an operator
    stopping a run must never read as a broken step."""
    entered = threading.Event()
    proceed = threading.Event()
    reached_end = []

    def move(action, ctx):
        entered.set()
        assert proceed.wait(10.0)
        ctx.checkpoint()
        reached_end.append(action.aid)
        return _lh_outputs(action)

    handlers("lh.move_relative", move)
    runner = runner_factory([_move(0), _move(1)])
    aids = runner.plan.aids()
    runner.start()
    assert entered.wait(5.0)
    assert runner.abort() is True
    proceed.set()

    assert wait_until(lambda: runner.state == "aborted")
    assert reached_end == [], "checkpoint raised rather than returning"
    result = runner.plan.result(aids[0])
    assert result.status == "aborted" and result.error.type == "ActionAborted"
    assert runner.plan.state(aids[1]) == "planned", "never started, and never marked skipped"
    finished = [e for e in runner.sink.events() if e.type == "run_finished"]
    assert finished and finished[-1].status == "aborted"


def test_abort_wakes_a_handler_parked_on_the_pause_gate(handlers, runner_factory):
    """The gate has to be openable by abort, or an aborted run would sit paused forever."""
    entered = threading.Event()
    proceed = threading.Event()

    def move(action, ctx):
        entered.set()
        assert proceed.wait(10.0)
        ctx.checkpoint()
        return _lh_outputs(action)

    handlers("lh.move_relative", move)
    runner = runner_factory([_move(0)])
    runner.start()
    assert entered.wait(5.0)
    assert runner.pause() is True
    proceed.set()
    assert wait_until(lambda: runner.state == "paused")
    runner.abort()
    assert wait_until(lambda: runner.state == "aborted")
    assert runner.resume() is False, "an aborted run does not resume"


def test_the_aborting_flag_is_readable_without_raising(handlers, runner_factory):
    """`ctx.aborting` exists for cleanup paths: a handler that has to put something down
    before it stops needs to know without being interrupted."""
    seen = []
    entered = threading.Event()
    proceed = threading.Event()

    def move(action, ctx):
        entered.set()
        assert proceed.wait(10.0)
        seen.append(ctx.aborting)
        return _lh_outputs(action)

    handlers("lh.move_relative", move)
    runner = runner_factory([_move(0)])
    runner.start()
    assert entered.wait(5.0)
    runner.abort()
    proceed.set()
    assert runner.join(5.0)
    assert seen == [True]


# --- the plan's own pause point ----------------------------------------------

def test_a_checkpoint_action_pauses_the_run_and_is_acknowledged_on_resume(
        handlers, runner_factory):
    """`control.checkpoint` is the plan saying "stop here and let the operator look" — the
    same gate as the button, so there is one pause mechanism and one place it can be wrong."""
    handlers("lh.move_relative", lambda a, c: _lh_outputs(a))
    runner = runner_factory([{"kind": "control.checkpoint", "message": "tube seated?"},
                             _move(1)])
    aids = runner.plan.aids()
    runner.start()
    assert wait_until(lambda: runner.state == "paused")
    paused = [e for e in runner.sink.events() if e.type == "run_paused"]
    assert paused[-1].reason == "checkpoint" and paused[-1].aid == aids[0]
    assert runner.plan.state(aids[1]) == "planned"

    runner.resume()
    assert wait_until(lambda: runner.state == "complete")
    outputs = runner.plan.result(aids[0]).outputs
    assert outputs.acknowledged is True and outputs.message == "tube seated?"


# --- failure classification --------------------------------------------------

def test_an_ordinary_exception_fails_the_action_and_halts_the_run(handlers, runner_factory):
    """R-ENG-15. The failure is recorded on the action, the run stops there, and the plan is
    left inspectable and injectable so the operator can recover."""
    def boom(action, ctx):
        raise RuntimeError("gripper reported 'not ready'")

    handlers("lh.move_relative", boom)
    runner = runner_factory([_move(0), _move(1)])
    aids = runner.plan.aids()
    assert runner.run_to_completion(timeout=5.0) == "failed"

    result = runner.plan.result(aids[0])
    assert result.status == "failed"
    assert result.error.type == "RuntimeError" and "not ready" in result.error.message
    assert runner.plan.state(aids[1]) == "planned", "halted, so the next action did not run"
    assert runner.cursor == 1, "the cursor sits where an injected fix will land (D22)"
    paused = [e for e in runner.sink.events() if e.type == "run_paused"]
    assert paused and paused[-1].reason == "action_failed"


def test_on_failure_continue_carries_on_but_the_run_still_reports_failed(
        handlers, runner_factory):
    ran = []

    def sometimes(action, ctx):
        if action.dz == 0:
            raise RuntimeError("nope")
        ran.append(action.aid)
        return _lh_outputs(action)

    handlers("lh.move_relative", sometimes)
    runner = runner_factory([_move(0, on_failure="continue"), _move(1)])
    assert runner.run_to_completion(timeout=5.0) == "failed"
    assert len(ran) == 1, "the second action ran"
    assert runner.plan.failed() == 1 and runner.plan.completed() == 1


def test_retries_are_bounded_and_recorded_per_attempt(handlers, runner_factory):
    """R-ENG-15. Bounded because a retry loop around a move that fails for a structural
    reason is a machine repeating a mistake."""
    calls = []

    def flaky(action, ctx):
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError("controller did not answer")
        return _lh_outputs(action)

    handlers("lh.move_relative", flaky)
    runner = runner_factory([_move(0, max_attempts=3)])
    aid = runner.plan.aids()[0]
    assert runner.run_to_completion(timeout=5.0) == "complete"
    attempts = runner.plan.attempts(aid)
    assert [r.attempt for r in attempts] == [1, 2, 3]
    assert [r.status for r in attempts] == ["failed", "failed", "complete"]
    assert attempts[0].error.retriable is True


def test_retries_stop_at_the_bound(handlers, runner_factory):
    calls = []

    def always_fails(action, ctx):
        calls.append(1)
        raise RuntimeError("still no")

    handlers("lh.move_relative", always_fails)
    runner = runner_factory([_move(0, max_attempts=2)])
    assert runner.run_to_completion(timeout=5.0) == "failed"
    assert len(calls) == 2


def test_on_failure_retry_is_not_a_no_op_against_the_default_attempt_count(
        handlers, runner_factory):
    """`max_attempts` defaults to 1, so `on_failure="retry"` has to ask for at least one
    retry or the policy would silently mean "halt"."""
    calls = []

    def once_then_fine(action, ctx):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first try")
        return _lh_outputs(action)

    handlers("lh.move_relative", once_then_fine)
    runner = runner_factory([_move(0, on_failure="retry")])
    assert runner.run_to_completion(timeout=5.0) == "complete"
    assert len(calls) == 2


def test_a_handler_returning_the_wrong_outputs_model_is_a_failure_not_a_wrong_row(
        handlers, runner_factory):
    handlers("lh.move_relative", lambda a, c: A.GripperOutputs(state="open"))
    runner = runner_factory([_move(0)])
    assert runner.run_to_completion(timeout=5.0) == "failed"
    error = runner.plan.result(runner.plan.aids()[0]).error
    assert error.type == "TypeError" and "LHMoveOutputs" in error.message


def test_a_missing_handler_reports_that_the_step_cannot_run(monkeypatch, runner_factory):
    """R-ENG-17. Pre-flight normally catches this; with pre-flight skipped the wrapper must
    still refuse rather than treat "nothing to call" as "nothing to do".

    The absent handler is *removed* here rather than borrowed from an unimplemented slice.
    This test used to name `camera.search_code`, which was unhandled at the time — so it
    started failing the moment that slice landed, testing the state of the codebase instead of
    the wrapper's behaviour. Every action kind now has a handler, so there is no gap to borrow.
    """
    # `raising=False` because whether that slice's module has been imported yet depends on
    # which other test files this run selected. Either way the kind ends up unhandled, which
    # is the only precondition this test has.
    monkeypatch.delitem(A._HANDLERS, "camera.search_code", raising=False)
    runner = runner_factory([{"kind": "camera.search_code", "device": "handover_cam",
                              "marker_id": 225}])
    runner.run_to_completion(timeout=5.0, skip_preflight=True)
    error = runner.plan.result(runner.plan.aids()[0]).error
    assert error.type == NoHandlerError.__name__ and "cannot run" in error.message


def test_start_is_refused_with_a_stated_reason_when_preflight_blocks(runner_factory):
    """R-ENG-2/16: refused, with every problem, not the first one."""
    runner = runner_factory([{"kind": "arm.waypoint", "device": "left", "waypoint": "HOME"}])
    with pytest.raises(StartRefused) as exc:
        runner.start()
    assert "HOME" in str(exc.value)
    assert runner.state == "failed" and runner.readiness == "failed"
    assert exc.value.report.missing_waypoints() == [("left", "HOME")]


def test_an_advisory_problem_needs_an_explicit_confirmation(runner_factory):
    """R-ENG-2's `degraded` path: start is allowed, but only when the caller says so."""
    runner = runner_factory([])
    with pytest.raises(StartRefused):
        runner.start()
    assert runner.readiness == "degraded"
    other = runner_factory([])
    assert other.run_to_completion(timeout=5.0, allow_degraded=True) == "complete"


# --- what the wrapper does so a handler cannot forget ------------------------

def test_the_wrapper_binds_the_log_context_for_the_handler(handlers, runner_factory):
    """R-LOG-5/D23. A handler cannot emit an unattributable record, because the context is
    already bound when it is called — including for anything it calls into in `drivers/`."""
    seen = {}

    def move(action, ctx):
        seen.update(obs.current_context())
        ctx.log.info("moving")
        return _lh_outputs(action)

    handlers("lh.move_relative", move)
    runner = runner_factory([_move(0)])
    aid = runner.plan.aids()[0]
    assert runner.run_to_completion(timeout=5.0) == "complete"
    assert seen["run_id"] == runner.run_id
    assert seen["aid"] == aid and seen["index"] == 0
    assert seen["action_kind"] == "lh.move_relative" and seen["device"] == "ot"
    assert runner.plan.result(aid).log_ref.aid == aid


def test_the_wrapper_records_inputs_outputs_and_timing(handlers, runner_factory):
    handlers("lh.move_relative", lambda a, c: _lh_outputs(a))
    runner = runner_factory([_move(2.5)])
    assert runner.run_to_completion(timeout=5.0) == "complete"
    result = runner.plan.result(runner.plan.aids()[0])
    assert result.inputs["dz"] == 2.5
    assert result.outputs.applied_mm == {"z": 2.5}
    assert result.started_at and result.finished_at and result.duration_ms >= 0
    started = [e for e in runner.sink.events() if e.type == "action_started"]
    finished = [e for e in runner.sink.events() if e.type == "action_finished"]
    assert len(started) == len(finished) == 1
    assert finished[0].result.status == "complete"


def test_the_wrapper_records_the_slot_a_move_derived_its_numbers_from(handlers,
                                                                     runner_factory):
    """"From the offset slot" is not a record of what was commanded; the value is."""
    handlers("lh.move_relative", lambda a, c: _lh_outputs(a))
    runner = runner_factory([_move(0.0, from_slot="selected_offset")])
    runner.blackboard.set("selected_offset",
                          A.OffsetOutputs(magnitude_mm=3.0, method="tag_3d"))
    assert runner.run_to_completion(timeout=5.0) == "complete"
    inputs = runner.plan.result(runner.plan.aids()[0]).inputs
    assert inputs["from_slot"] == "selected_offset"
    assert inputs["from_slot_value"]["magnitude_mm"] == 3.0


def test_the_wrapper_collects_artifacts_and_warnings_off_the_context(handlers,
                                                                    runner_factory):
    def move(action, ctx):
        ctx.warn("dead_reckoned", "position is dead-reckoned, not measured")
        ctx.artifact("json", "runs/x/offset.json", label="solve")
        return _lh_outputs(action)

    handlers("lh.move_relative", move)
    runner = runner_factory([_move(0)])
    assert runner.run_to_completion(timeout=5.0) == "complete"
    result = runner.plan.result(runner.plan.aids()[0])
    assert [w.code for w in result.warnings] == ["dead_reckoned"]
    assert result.warnings[0].device == "ot"
    assert [a.label for a in result.artifacts] == ["solve"]


# --- D25: simulated comes from configuration ---------------------------------

def test_a_pure_compute_action_in_a_simulated_run_reports_simulated(handlers,
                                                                    runner_factory):
    """R-SIM-6/D25. `vision.solve_offset` with no device touches nothing, so the
    vendor-string approach reported "real" — in the dangerous direction. With simulation the
    default (D29), this field is what stops a simulated run reading as a real one."""
    seen = {}

    def solve(action, ctx):
        seen["simulated"] = ctx.simulated
        return A.OffsetOutputs(magnitude_mm=0.2, method="tag_3d")

    handlers("vision.solve_offset", solve)
    runner = runner_factory([{"kind": "vision.solve_offset"}])
    assert runner.plan.actions[0].device is None
    assert runner.run_to_completion(timeout=5.0) == "complete"
    assert seen["simulated"] is True
    assert runner.plan.result(runner.plan.aids()[0]).simulated is True


def test_a_pure_compute_action_in_a_real_run_reports_real(handlers, runner_factory):
    handlers("vision.solve_offset", lambda a, c: A.OffsetOutputs(magnitude_mm=0.2,
                                                                 method="tag_3d"))
    runner = runner_factory([{"kind": "vision.solve_offset"}],
                            is_simulated=lambda device: False)
    assert runner.run_to_completion(timeout=5.0) == "complete"
    assert runner.plan.result(runner.plan.aids()[0]).simulated is False


def test_the_run_states_which_devices_are_simulated_up_front_and_on_the_snapshot(
        handlers, runner_factory):
    handlers("lh.move_relative", lambda a, c: _lh_outputs(a))
    runner = runner_factory([_move(0)], is_simulated=lambda d: d != "left")
    assert runner.run_to_completion(timeout=5.0) == "complete"
    started = [e for e in runner.sink.events() if e.type == "run_started"][0]
    assert started.simulated["ot"] is True and started.simulated["left"] is False
    assert runner.snapshot().simulated == started.simulated


# --- device claims, and the e-stop that must never be gated ------------------

def test_a_device_is_claimed_for_the_duration_of_an_action_and_released_after(
        handlers, runner_factory):
    during = []

    def move(action, ctx):
        during.append(device_claims.holder("ot"))
        return _lh_outputs(action)

    handlers("lh.move_relative", move)
    runner = runner_factory([_move(0)])
    aid = runner.plan.aids()[0]
    assert runner.run_to_completion(timeout=5.0) == "complete"
    assert during == [f"aid{aid}"]
    assert device_claims.holder("ot") is None


def test_a_claim_is_released_even_when_the_action_fails(handlers, runner_factory):
    handlers("lh.move_relative", lambda a, c: (_ for _ in ()).throw(RuntimeError("no")))
    runner = runner_factory([_move(0)])
    assert runner.run_to_completion(timeout=5.0) == "failed"
    assert device_claims.holder("ot") is None


def test_a_claim_never_blocks_so_it_can_never_gate_an_emergency_stop():
    """D21/R-ENG-11. The operator's stop must stay reachable *while an action is blocking* —
    that is the only time it matters. So the claim registry has no blocking primitive at all:
    a second claim raises immediately, and there is no acquire/wait to be queued behind.

    If a future change adds a claim check to `POST /api/arms/{id}/stop`, this is the test that
    should have stopped it: that route bypasses the teach lock deliberately and carries a
    "do not fix that" comment.
    """
    device_claims.claim("left", "aid1")
    try:
        began = time.monotonic()
        with pytest.raises(DeviceBusy):
            device_claims.claim("left", "aid2")
        assert time.monotonic() - began < 0.5, "raised rather than waited"
        for blocking_api in ("acquire", "wait", "join", "__enter__"):
            assert not hasattr(device_claims, blocking_api)
    finally:
        device_claims.release("left", "aid1")
    assert device_claims.holder("left") is None


# --- D24: reconnect from the snapshot alone ----------------------------------

def test_a_reconnecting_client_reconstructs_full_state_from_the_snapshot_alone(
        handlers, runner_factory):
    """D24/R-ENG-18. An event stream carries deltas, so a client that connects mid-run can
    only show the future. Everything below is reconstructed from the snapshot object and
    nothing else — no earlier events, no plan fetch, no result fetch.
    """
    entered = [threading.Event() for _ in range(3)]
    release = [threading.Event() for _ in range(3)]

    def move(action, ctx):
        i = int(action.dz)
        entered[i].set()
        assert release[i].wait(10.0)
        ctx.artifact("json", f"runs/x/{i}.json", label=f"step {i}")
        return _lh_outputs(action)

    handlers("lh.move_relative", move)
    runner = runner_factory([_move(0), _move(1), _move(2)], name="handover")
    aids = runner.plan.aids()
    runner.start()
    assert entered[0].wait(5.0)
    release[0].set()
    assert entered[1].wait(5.0)
    runner.pause()
    release[1].set()
    assert wait_until(lambda: runner.state == "paused")

    snap = runner.snapshot()

    # A client with only this object knows what the run is, where it is, and what happened.
    assert snap.run_id == runner.run_id and snap.name == "handover"
    assert snap.state == "paused" and snap.cursor == 2
    assert snap.revision == runner.plan.revision
    assert snap.started_at and snap.readiness == "ready"
    assert snap.simulated == {"ot": True, "left": True, "right": True, "handover_cam": True}
    assert [row.aid for row in snap.actions] == list(aids)
    assert [row.index for row in snap.actions] == [0, 1, 2]
    assert [row.state for row in snap.actions] == ["complete", "complete", "planned"]
    assert [row.label for row in snap.actions] == [row.label for row in runner.plan.rows()]
    # The results so far, outputs and artifacts included, not just the states.
    assert snap.actions[0].result.outputs.applied_mm == {"z": 0.0}
    assert snap.actions[1].result.artifacts[0].label == "step 1"
    assert snap.actions[2].result is None

    # The handover to the live stream is race-free: nothing newer than the snapshot's seq is
    # outstanding, so applying later events cannot double-apply or skip.
    assert snap.seq == runner.sink.last_seq
    assert runner.sink.since(snap.seq) == []

    release[2].set()
    runner.resume()
    assert wait_until(lambda: runner.state == "complete")
    assert runner.sink.since(snap.seq), "and the client sees everything after it"


def test_every_event_carries_a_monotonic_sequence_number(handlers, runner_factory):
    """Strictly increasing and unique — but **not** starting at 1, because the sequence is
    process-wide rather than per run (B6). One socket carries every run, so a per-run counter
    made the client's documented "drop `seq <= last`" rule discard a whole second run. What a
    client keys a run on is `run_id`, which is on every event; see `test_event_sequence.py`."""
    handlers("lh.move_relative", lambda a, c: _lh_outputs(a))
    runner = runner_factory([_move(0), _move(1)])
    assert runner.run_to_completion(timeout=5.0) == "complete"
    seqs = [e.seq for e in runner.sink.events()]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert seqs[0] >= 1 and seqs[-1] == runner.sink.last_seq
    assert all(e.run_id == runner.run_id and e.ts for e in runner.sink.events())


def test_a_subscriber_sees_events_as_they_happen(handlers, runner_factory):
    """S8 attaches here. Subscribers are called on the worker thread, which is why an asyncio
    consumer has to hop the loop itself rather than expecting this module to know about one."""
    seen = []
    handlers("lh.move_relative", lambda a, c: _lh_outputs(a))
    runner = runner_factory([_move(0)])
    runner.sink.subscribe(lambda event: seen.append(event.type))
    assert runner.run_to_completion(timeout=5.0) == "complete"
    assert "run_started" in seen and "action_finished" in seen and "run_finished" in seen


def test_a_broken_subscriber_does_not_stop_the_run(handlers, runner_factory):
    handlers("lh.move_relative", lambda a, c: _lh_outputs(a))
    runner = runner_factory([_move(0)])
    runner.sink.subscribe(lambda event: (_ for _ in ()).throw(RuntimeError("bad client")))
    assert runner.run_to_completion(timeout=5.0) == "complete"


def test_an_aborted_action_is_never_reported_as_a_failure(handlers, runner_factory):
    """The distinction R-ENG-10 is about, stated once more at the boundary between the two:
    `ActionAborted` out of a handler is `aborted`, any other exception is `failed`."""
    def move(action, ctx):
        raise ActionAborted("stopped")

    handlers("lh.move_relative", move)
    runner = runner_factory([_move(0), _move(1)])
    assert runner.run_to_completion(timeout=5.0) == "aborted"
    assert runner.plan.result(runner.plan.aids()[0]).status == "aborted"
    assert runner.plan.state(runner.plan.aids()[1]) == "planned"
