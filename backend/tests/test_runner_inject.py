"""Injection: at the cursor, after a completed action, after the last — and refusals.

D22 overturned the draft's caution here, and both halves matter. Injection **must** be able to
land at the cursor, because that is exactly where a failure leaves the run and therefore the
only place a recovery step is useful (R-ENG-15). And it must **not** be refused merely because
a long move is in flight, since a blocking `move_joints(wait=True)` is the normal state of a
run rather than an exceptional one.

What is refused is refused *with a stated reason* and never relocated (R-ENG-13): the past,
and anything that would land where the run has already gone past — because accepting that
would be a silent skip wearing an accepted request's clothes.
"""
from __future__ import annotations

import threading
import time

import pytest
from pydantic import ValidationError

from backend.app.engine import actions as A
from backend.app.engine.plan import InjectRefused, Plan
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


def wait_until(predicate, timeout: float = 5.0) -> bool:
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
        plan = Plan(actions, name=kwargs.pop("name", "inject"))
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


def _outputs(action) -> A.LHMoveOutputs:
    return A.LHMoveOutputs(applied_mm={"z": action.dz})


def _rejections(runner: Runner) -> list:
    return [e for e in runner.sink.events() if e.type == "inject_rejected"]


# --- the three placements that must work -------------------------------------

def test_injecting_at_the_cursor_after_a_failure_runs_next(handlers, runner_factory):
    """The case D22 exists for. A step fails, the run halts with the plan inspectable, the
    operator injects a fix, and it is the very next thing that runs."""
    ran = []

    def move(action, ctx):
        if action.dz == 0:
            raise RuntimeError("gripper reported 'not ready'")
        ran.append(action.dz)
        return _outputs(action)

    handlers("lh.move_relative", move)
    runner = runner_factory([_move(0), _move(2)])
    failed_aid, last_aid = runner.plan.aids()
    assert runner.run_to_completion(timeout=5.0) == "failed"
    assert runner.cursor == 1

    injected = runner.inject(_move(9), after_aid=failed_aid)[0]
    assert injected.index == runner.cursor == 1, "landed at the cursor"
    assert injected.origin == "inject"

    assert runner.resume() is True
    assert wait_until(lambda: runner.state in ("failed", "complete"))
    assert ran == [9, 2], "the injected action ran first, then the rest of the plan"
    assert runner.plan.state(injected.aid) == "complete"
    assert runner.plan.state(last_aid) == "complete"


def test_injecting_after_a_completed_action_while_paused(handlers, runner_factory):
    entered = [threading.Event() for _ in range(3)]
    release = [threading.Event() for _ in range(3)]
    ran = []

    def move(action, ctx):
        # dz doubles as the gate index; the injected 0.5 truncates to 0, whose gate is already
        # open by the time it runs.
        i = min(int(action.dz), 2)
        entered[i].set()
        assert release[i].wait(10.0)
        ran.append(action.dz)
        return _outputs(action)

    handlers("lh.move_relative", move)
    runner = runner_factory([_move(0), _move(1), _move(2)])
    first = runner.plan.aids()[0]
    runner.start()
    assert entered[0].wait(5.0)
    runner.pause()
    release[0].set()
    assert wait_until(lambda: runner.state == "paused")

    injected = runner.inject(_move(0.5), after_aid=first)[0]
    assert injected.index == 1
    for event in release:
        event.set()
    runner.resume()
    assert wait_until(lambda: runner.state == "complete", timeout=8.0)
    assert ran[0] == 0 and ran[1] == 0.5, "the injection ran in the position it was given"


def test_injecting_after_the_last_action_appends(handlers, runner_factory):
    entered = threading.Event()
    release = threading.Event()
    ran = []

    def move(action, ctx):
        if action.dz == 0:
            entered.set()
            assert release.wait(10.0)
        ran.append(action.dz)
        return _outputs(action)

    handlers("lh.move_relative", move)
    runner = runner_factory([_move(0), _move(1)])
    last = runner.plan.aids()[-1]
    runner.start()
    assert entered.wait(5.0)
    injected = runner.inject(_move(7), after_aid=last)[0]
    assert injected.index == 2
    release.set()
    assert wait_until(lambda: runner.state == "complete")
    assert ran == [0, 1, 7]


def test_injecting_after_the_action_in_flight_is_accepted(handlers, runner_factory):
    """D22: "do not 409 an injection merely because a long move is in flight". The move is the
    normal state of a run, and the next boundary is a perfectly good place to land."""
    entered = threading.Event()
    release = threading.Event()

    def move(action, ctx):
        entered.set()
        assert release.wait(10.0)
        return _outputs(action)

    handlers("lh.move_relative", move)
    runner = runner_factory([_move(0), _move(1)])
    runner.start()
    assert entered.wait(5.0)
    running = runner.running_aid
    assert running == runner.plan.aids()[0]
    injected = runner.inject(_move(3), after_aid=running)[0]
    assert injected.index == 1, "immediately after the move that is still executing"
    assert _rejections(runner) == []
    release.set()


def test_injecting_at_the_front_before_anything_has_run(handlers, runner_factory):
    handlers("lh.move_relative", lambda a, c: _outputs(a))
    runner = runner_factory([_move(1)])
    injected = runner.inject(_move(0), after_aid=None)[0]
    assert injected.index == 0
    assert runner.run_to_completion(timeout=5.0) == "complete"


# --- the refusals ------------------------------------------------------------

def test_injecting_into_the_past_is_refused_with_a_reason(handlers, runner_factory):
    entered = [threading.Event() for _ in range(3)]
    release = [threading.Event() for _ in range(3)]

    def move(action, ctx):
        i = int(action.dz)
        entered[i].set()
        assert release[i].wait(10.0)
        return _outputs(action)

    handlers("lh.move_relative", move)
    runner = runner_factory([_move(0), _move(1), _move(2)])
    first = runner.plan.aids()[0]
    runner.start()
    assert entered[0].wait(5.0)
    release[0].set()
    assert entered[1].wait(5.0)
    runner.pause()
    release[1].set()
    assert wait_until(lambda: runner.state == "paused")

    with pytest.raises(InjectRefused) as exc:
        runner.inject(_move(9), after_aid=first)
    assert "past" in exc.value.reason
    assert len(runner.plan) == 3, "nothing was inserted anywhere"
    rejected = _rejections(runner)
    assert rejected and rejected[-1].reason == exc.value.reason
    assert rejected[-1].after_aid == first


def test_injecting_in_front_of_the_action_in_flight_is_refused_not_relocated(
        handlers, runner_factory):
    """It would land behind the cursor, where the run has already gone past — accepting it
    would be a silent skip. Refused with the reason, and *not* moved to a place that works."""
    entered = [threading.Event() for _ in range(2)]
    release = [threading.Event() for _ in range(2)]

    def move(action, ctx):
        i = int(action.dz)
        entered[i].set()
        assert release[i].wait(10.0)
        return _outputs(action)

    handlers("lh.move_relative", move)
    runner = runner_factory([_move(0), _move(1)])
    first, second = runner.plan.aids()
    runner.start()
    assert entered[0].wait(5.0)
    release[0].set()
    assert entered[1].wait(5.0)

    with pytest.raises(InjectRefused) as exc:
        runner.inject(_move(9), after_aid=first)
    assert "executing now" in exc.value.reason and f"aid {second}" in exc.value.reason
    assert len(runner.plan) == 2
    release[1].set()


def test_injecting_after_an_identity_that_does_not_exist_is_refused(handlers,
                                                                    runner_factory):
    handlers("lh.move_relative", lambda a, c: _outputs(a))
    runner = runner_factory([_move(1)])
    with pytest.raises(InjectRefused) as exc:
        runner.inject(_move(2), after_aid=404)
    assert "404" in exc.value.reason
    assert len(runner.plan) == 1


def test_an_invalid_injected_action_is_refused_before_it_reaches_the_plan(handlers,
                                                                         runner_factory):
    """The LLM/API gate (R-UI-7). A misspelled field is an ignored field is a move to the
    wrong place reporting success, so it must be a refusal — and the refusal has to be visible
    to every connected client, not only to the caller."""
    handlers("lh.move_relative", lambda a, c: _outputs(a))
    runner = runner_factory([_move(1)])
    with pytest.raises((InjectRefused, ValidationError)):
        runner.inject({"kind": "lh.move_relative", "device": "ot", "dzz": 5.0},
                      after_aid=runner.plan.aids()[0])
    assert len(runner.plan) == 1
    assert _rejections(runner), "refused with a reason on the event stream too"


def test_a_nan_offset_cannot_be_injected(handlers, runner_factory):
    handlers("lh.move_relative", lambda a, c: _outputs(a))
    runner = runner_factory([_move(1)])
    with pytest.raises((InjectRefused, ValidationError)):
        runner.inject({"kind": "lh.move_relative", "device": "ot", "dz": float("nan")},
                      after_aid=None)
    assert len(runner.plan) == 1


# --- what must survive the renumber ------------------------------------------

def test_injection_preserves_every_completed_actions_result(handlers, runner_factory):
    """R-ENG-4, the part that is a silent data bug rather than a crash: an index-keyed result
    store would hand action 2's outputs to whatever landed on index 1, and the UI would render
    a plausible wrong answer."""
    entered = [threading.Event() for _ in range(4)]
    release = [threading.Event() for _ in range(4)]

    def move(action, ctx):
        i = int(action.dz)
        entered[i].set()
        assert release[i].wait(10.0)
        return _outputs(action)

    handlers("lh.move_relative", move)
    runner = runner_factory([_move(0), _move(1), _move(2)])
    aids = runner.plan.aids()
    runner.start()
    for i in (0, 1):
        assert entered[i].wait(5.0)
        release[i].set()
    assert entered[2].wait(5.0)
    runner.pause()
    release[2].set()
    assert wait_until(lambda: runner.state == "paused")

    before = {aid: runner.plan.result(aid).outputs.applied_mm for aid in aids}
    revision = runner.plan.revision
    injected = runner.inject(_move(3), after_aid=aids[-1])[0]

    for aid in aids:
        assert runner.plan.result(aid).outputs.applied_mm == before[aid]
        assert runner.plan.result(aid).index == runner.plan.index_of(aid)
        assert runner.plan.result(aid).aid == aid
    assert runner.plan.revision == revision + 1
    replaced = [e for e in runner.sink.events() if e.type == "plan_replaced"][-1]
    assert replaced.revision == runner.plan.revision
    assert [row["aid"] for row in replaced.actions] == list(aids) + [injected.aid]
    assert replaced.cursor == runner.cursor


def test_an_injection_mid_run_does_not_displace_the_action_executing(handlers,
                                                                    runner_factory):
    """R-ENG-13: "cannot silently displace the action currently executing". The running action
    keeps its identity and its result; only its display index moves."""
    entered = threading.Event()
    release = threading.Event()

    def move(action, ctx):
        if action.dz == 0:
            entered.set()
            assert release.wait(10.0)
        return _outputs(action)

    handlers("lh.move_relative", move)
    runner = runner_factory([_move(0), _move(1)])
    first = runner.plan.aids()[0]
    runner.start()
    assert entered.wait(5.0)
    runner.inject(_move(5), after_aid=first)
    assert runner.running_aid == first
    assert runner.plan.by_aid(first).index == 0
    release.set()
    assert wait_until(lambda: runner.state == "complete")
    assert runner.plan.result(first).status == "complete"
    assert runner.plan.completed() == 3
