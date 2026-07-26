"""The plan: identity that survives mutation, derived indices, and pre-flight.

The property under test throughout is D4/R-ENG-4: `aid` is assigned once and never changes,
`index` is derived and recomputed, and a completed action's recorded outputs stay attached to
the action that produced them across any number of renumbers. The bug this guards against is
not a crash — it is an injection quietly re-attributing every completed step's outputs one row
down, which looks entirely plausible in the UI and is wrong.
"""
from __future__ import annotations

import json

import pytest

from backend.app.engine import actions as A
from backend.app.engine.plan import (ENGINE_KINDS, InjectRefused, MaterializationCapped,
                                     NestedLoopRefused, Plan, PlanError, UnknownActionError,
                                     default_label, params_of, preflight, waypoint_pairs)

DEVICES = {"left", "right", "ot", "handover_cam", "gripper_left_cam"}


def _move(dz: float = 1.0, **kw) -> dict:
    return {"kind": "lh.move_relative", "device": "ot", "dz": dz, **kw}


def _loop(iterations: int = 3, body: list | None = None, **kw) -> dict:
    return {"kind": "control.loop", "max_iterations": iterations,
            "body": body or [_move(-1.0), {"kind": "vision.solve_offset"}], **kw}


def _result(plan: Plan, aid: int, *, magnitude: float = 4.0) -> A.ActionResult:
    action = plan.by_aid(aid)
    return A.ActionResult(
        aid=aid, index=action.index, kind=action.kind, device=action.device,
        status="complete", simulated=True,
        outputs=A.OffsetOutputs(magnitude_mm=magnitude, method="tag_3d"))


# --- identity and derived index ----------------------------------------------

def test_identity_is_assigned_once_and_index_is_derived():
    plan = Plan([_move(1.0), _move(2.0), _move(3.0)])
    assert plan.aids() == (1, 2, 3)
    assert [a.index for a in plan.actions] == [0, 1, 2]
    assert plan.index_of(3) == 2


def test_an_insertion_renumbers_indices_and_changes_no_identity():
    plan = Plan([_move(1.0), _move(2.0), _move(3.0)])
    before = plan.aids()
    plan.insert_after(1, _move(9.0))
    assert plan.aids() == (before[0], 4, before[1], before[2])
    assert [a.index for a in plan.actions] == [0, 1, 2, 3]
    # Every original identity still resolves, and to the same action.
    assert plan.by_aid(2).dz == 2.0 and plan.by_aid(3).dz == 3.0


def test_an_insertion_never_loses_a_completed_actions_result():
    """R-ENG-4's actual requirement. Results are keyed on `aid`, so a renumber moves the row
    and takes the outputs with it — an index-keyed store would have handed action 2's outputs
    to whatever landed on index 1."""
    plan = Plan([_move(1.0), _move(2.0), _move(3.0)])
    plan.record_result(_result(plan, 1, magnitude=7.5))
    plan.record_result(_result(plan, 2, magnitude=2.5))
    plan.insert_after(None, _move(0.0))

    assert plan.result(1).outputs.magnitude_mm == 7.5
    assert plan.result(2).outputs.magnitude_mm == 2.5
    # The recorded index is derived too, so it follows the renumber rather than going stale.
    assert plan.result(1).index == plan.index_of(1) == 1
    assert plan.result(2).index == plan.index_of(2) == 2
    assert plan.state(1) == "complete"


def test_aids_are_never_reused():
    plan = Plan([_move()])
    first = plan.append(_move()).aid
    second = plan.insert_after(None, _move())[0].aid
    assert len({1, first, second}) == 3


def test_adopting_an_action_does_not_mutate_the_callers_object():
    """Copy-on-adopt. Without it, materializing a loop body would stamp an aid onto the
    template and every iteration after the first would carry the first one's identity."""
    action = A.validate_action(_move())
    plan = Plan([action])
    plan.append(action)
    assert action.aid == 0
    assert plan.aids() == (1, 2)


def test_an_unknown_aid_raises_rather_than_defaulting():
    plan = Plan([_move()])
    for call in (lambda: plan.by_aid(99), lambda: plan.index_of(99),
                 lambda: plan.state(99)):
        with pytest.raises(UnknownActionError):
            call()


def test_revision_increments_on_every_mutation():
    plan = Plan([_move()])
    start = plan.revision
    plan.append(_move())
    plan.insert_after(1, _move())
    assert plan.revision == start + 2


# --- insertion rules ---------------------------------------------------------

def test_insert_after_none_lands_at_the_front():
    plan = Plan([_move(1.0)])
    inserted = plan.insert_after(None, _move(9.0))
    assert inserted[0].index == 0 and plan.by_aid(1).index == 1


def test_insert_after_the_last_action_lands_at_the_end():
    plan = Plan([_move(1.0), _move(2.0)])
    inserted = plan.insert_after(2, _move(9.0))
    assert inserted[0].index == 2 and len(plan) == 3


def test_landing_at_the_cursor_is_allowed():
    """D22. Injecting after the last completed action makes the new action next — which is
    exactly where a failure leaves the run, and the single most useful place to inject."""
    plan = Plan([_move(1.0), _move(2.0), _move(3.0)])
    plan.cursor = 1                                  # action 0 done, action 1 next
    assert plan.refusal_for_insert(1, cursor=1, running_aid=None) is None
    inserted = plan.insert_after(1, _move(9.0))
    assert inserted[0].index == plan.cursor == 1


def test_the_past_is_refused_with_a_reason_and_not_relocated():
    plan = Plan([_move(1.0), _move(2.0), _move(3.0)])
    plan.cursor = 2
    reason = plan.refusal_for_insert(1, cursor=2, running_aid=None)
    assert reason and "past" in reason
    assert len(plan) == 3, "a refused injection must not have inserted anything"


def test_a_long_move_in_flight_is_not_a_reason_to_refuse():
    """D22's other half: refusing merely because a blocking move is in progress is the
    behaviour the review overturned. Injecting after the running action is accepted."""
    plan = Plan([_move(1.0), _move(2.0), _move(3.0)])
    plan.cursor = 1
    assert plan.refusal_for_insert(2, cursor=1, running_aid=2) is None


def test_inserting_in_front_of_the_running_action_is_refused_as_unreachable():
    plan = Plan([_move(1.0), _move(2.0), _move(3.0)])
    reason = plan.refusal_for_insert(1, cursor=1, running_aid=2)
    assert reason and "executing now" in reason and "aid 2" in reason


def test_an_unknown_target_identity_is_refused_with_a_reason():
    plan = Plan([_move()])
    reason = plan.refusal_for_insert(99, cursor=0, running_aid=None)
    assert reason and "99" in reason


# --- loop materialization ----------------------------------------------------

def test_a_materialized_iteration_is_real_indexed_rows_carrying_parent_and_iteration():
    """D8. The reason for materializing at all is R-VIS-8: "what did the loop see on
    iteration 4" is only answerable if iteration 4 is rows with their own outputs and logs."""
    plan = Plan([_loop(), _move(5.0)])
    copies = plan.materialize_iteration(1, 1)
    assert [c.origin for c in copies] == ["expand", "expand"]
    assert [c.parent_aid for c in copies] == [1, 1]
    assert [c.iteration for c in copies] == [1, 1]
    assert [a.index for a in plan.actions] == [0, 1, 2, 3]
    # Materialized rows sit between the loop and whatever followed it.
    assert plan.by_aid(2).index == 0 + 3, "the trailing action moved down by the body size"


def test_iterations_stack_in_order_and_the_region_stays_contiguous():
    plan = Plan([_loop(), _move(5.0)])
    plan.materialize_iteration(1, 1)
    plan.materialize_iteration(1, 2)
    start, end = plan.region_of(1)
    assert (start, end) == (1, 5)
    assert [a.iteration for a in plan.members_of(1)] == [1, 1, 2, 2]


def test_max_iterations_caps_materialization():
    plan = Plan([_loop(iterations=2)])
    plan.materialize_iteration(1, 1)
    plan.materialize_iteration(1, 2)
    with pytest.raises(MaterializationCapped):
        plan.materialize_iteration(1, 3)


def test_the_global_ceiling_caps_materialization_across_the_plan():
    """D14's second bound. Two loops that individually pass the `Loop` validator must not be
    able to add up past the ceiling — the validator only ever sees one loop."""
    body = [_move(-1.0)] * 10
    plan = Plan([_loop(iterations=30, body=body), _loop(iterations=30, body=body)])
    loop_a, loop_b = plan.aids()
    with pytest.raises(MaterializationCapped):
        for i in range(1, 31):
            plan.materialize_iteration(loop_a, i)
            plan.materialize_iteration(loop_b, i)
    assert plan.materialized_count() <= A.MAX_MATERIALIZED_ACTIONS


def test_an_authored_nested_loop_is_refused_before_it_reaches_the_plan():
    with pytest.raises(Exception) as exc:
        Plan([_loop(body=[_loop()])])
    assert "nested" in str(exc.value).lower()


def test_injecting_a_loop_into_a_materialized_loop_body_is_refused():
    """The second route to a nested loop, which no validator sees: `Loop.body` was fine when
    it was authored, and the loop arrives by injection afterwards (D14)."""
    plan = Plan([_loop()])
    plan.materialize_iteration(1, 1)
    members = plan.members_of(1)
    with pytest.raises(NestedLoopRefused):
        plan.insert_after(members[0].aid, _loop())


def test_an_action_injected_between_two_rows_of_an_iteration_joins_that_iteration():
    """It is part of iteration 1 — the UI nests it there and the runner's "next unrun row in
    this region" picks it up rather than stepping over it, which would be a silent skip."""
    plan = Plan([_loop()])
    plan.materialize_iteration(1, 1)
    first = plan.members_of(1)[0]
    injected = plan.insert_after(first.aid, _move(0.5))[0]
    assert injected.parent_aid == 1 and injected.iteration == 1
    assert injected.origin == "inject", "origin still says who put it there"


def test_injecting_after_a_loop_lands_after_its_whole_materialized_body():
    """"After the servo loop" is the operator's meaning, and keeping the region contiguous is
    what lets the runner tell which rows belong to which iteration."""
    plan = Plan([_loop(), _move(5.0)])
    plan.materialize_iteration(1, 1)
    plan.materialize_iteration(1, 2)
    injected = plan.insert_after(1, {"kind": "control.checkpoint"})[0]
    assert injected.parent_aid is None
    assert injected.index == plan.region_of(1)[1], "immediately past the region, not inside it"
    assert plan.by_aid(2).index == injected.index + 1


def test_next_unrun_in_region_walks_the_iteration_and_stops():
    plan = Plan([_loop()])
    copies = plan.materialize_iteration(1, 1)
    assert plan.next_unrun_in_region(1).aid == copies[0].aid
    plan.set_state(copies[0].aid, "complete")
    assert plan.next_unrun_in_region(1).aid == copies[1].aid
    plan.set_state(copies[1].aid, "complete")
    assert plan.next_unrun_in_region(1) is None


def test_materializing_a_non_loop_is_a_plan_error():
    plan = Plan([_move()])
    with pytest.raises(PlanError):
        plan.materialize_iteration(1, 1)


# --- projections -------------------------------------------------------------

def test_a_row_carries_identity_the_derived_index_state_and_the_result():
    plan = Plan([_move(1.0)])
    plan.record_result(_result(plan, 1))
    row = plan.rows()[0]
    assert (row.aid, row.index, row.state) == (1, 0, "complete")
    assert row.result.outputs.magnitude_mm == 4.0
    assert row.label, "a blank label is auto-filled from the kind"


def test_the_plan_replaced_payload_omits_results():
    """`action_finished` already carries the whole typed result; `plan_replaced` is emitted
    once per loop iteration, and re-sending every completed action's outputs on each one makes
    a plan mutation the largest message on the socket."""
    plan = Plan([_move(1.0)])
    plan.record_result(_result(plan, 1))
    payload = plan.row_payload()[0]
    assert "result" not in payload
    assert payload["aid"] == 1 and payload["state"] == "complete"


def test_a_loops_params_summarize_the_body_rather_than_repeating_it():
    plan = Plan([_loop()])
    params = plan.rows()[0].params
    assert "body" not in params
    assert params["body_kinds"] == ["lh.move_relative", "vision.solve_offset"]
    assert params["body_size"] == 2


def test_params_keep_the_operational_fields_the_row_has_no_column_for():
    action = A.validate_action(_move(1.0, on_failure="continue", max_attempts=3, note="hi"))
    params = params_of(action)
    assert params["on_failure"] == "continue" and params["max_attempts"] == 3
    assert params["note"] == "hi"
    assert "aid" not in params and "index" not in params


def test_the_snapshot_carries_the_plan_and_the_run_level_facts():
    plan = Plan([_move(1.0), _move(2.0)], name="handover")
    plan.cursor = 1
    plan.record_result(_result(plan, 1))
    snap = plan.snapshot(seq=42, run_id="run-x", state="paused",
                         simulated={"ot": True}, readiness="ready")
    assert (snap.seq, snap.run_id, snap.state, snap.cursor) == (42, "run-x", "paused", 1)
    assert snap.revision == plan.revision and snap.name == "handover"
    assert [r.aid for r in snap.actions] == [1, 2]
    assert snap.actions[0].result is not None and snap.actions[1].result is None
    assert snap.simulated == {"ot": True}


def test_default_labels_describe_the_step():
    assert "HOME" in default_label(
        A.validate_action({"kind": "arm.waypoint", "device": "left", "waypoint": "HOME"}))
    assert "gripper" in default_label(
        A.validate_action({"kind": "arm.gripper", "device": "left", "state": "close"}))
    assert default_label(A.validate_action({"kind": "control.checkpoint"}))


# --- pre-flight --------------------------------------------------------------

def _teach(path, library: dict) -> None:
    path.write_text(json.dumps(library))


def _pose() -> dict:
    return {"pose": {"x": 1.0, "y": 2.0, "z": 3.0, "roll": 180.0, "pitch": 0.0, "yaw": 0.0},
            "joints": [0.0] * 6, "saved_at": "2026-01-01T00:00:00+00:00"}


def test_an_untaught_waypoint_is_reported_as_a_device_name_pair():
    """R-WP-5 / R-ENG-16. Bare names let an operator mis-read which arm is untaught, and HOME
    exists on both."""
    plan = Plan([{"kind": "arm.waypoint", "device": "left", "waypoint": "HOME"}])
    report = plan.preflight(known_devices=DEVICES)
    assert not report.ok
    assert report.missing_waypoints() == [("left", "HOME")]
    assert report.readiness == "failed"


def test_a_waypoint_the_acting_device_does_not_own_is_refused_naming_both_arms():
    """R-WP-2. `TUBE` belongs to `right`; the failure this prevents is one arm moving to the
    other's taught point, which is not hypothetical — several are near-mirror images."""
    plan = Plan([{"kind": "arm.waypoint", "device": "left", "waypoint": "TUBE"}])
    report = plan.preflight(known_devices=DEVICES)
    assert report.unowned_waypoints() == [("left", "TUBE")]
    problem = next(p for p in report.blocking if p.code == "waypoint_not_owned")
    assert "'right'" in problem.message and "'left'" in problem.message


def test_a_taught_owned_waypoint_passes(isolated_teach_poses):
    _teach(isolated_teach_poses, {"left": {"HOME": _pose()}})
    plan = Plan([{"kind": "arm.waypoint", "device": "left", "waypoint": "HOME"}])
    assert plan.preflight(known_devices=DEVICES).missing_waypoints() == []


def test_a_device_outside_the_fleet_is_reported_rather_than_discovered():
    plan = Plan([{"kind": "lh.move_relative", "device": "ghost", "dz": 1.0}])
    report = plan.preflight(known_devices=DEVICES)
    assert report.unknown_devices() == ["ghost"]


def test_a_kind_with_no_handler_says_the_step_cannot_run():
    """R-ENG-17. Reported, never skipped: a silently skipped step reads downstream as
    "it ran"."""
    plan = Plan([{"kind": "camera.search_code", "device": "handover_cam", "marker_id": 225}])
    report = plan.preflight(known_devices=DEVICES)
    codes = [p.code for p in report.problems]
    assert "no_handler" in codes or A.handler_for("camera.search_code") is not None


def test_the_control_kinds_need_no_handler():
    """Control flow is the engine's own; pre-flighting it as unhandled would fail readiness
    for a handler that is not supposed to exist."""
    plan = Plan([{"kind": "control.checkpoint"}, _loop(body=[{"kind": "control.checkpoint"}])])
    report = plan.preflight(known_devices=DEVICES)
    assert [p.code for p in report.problems if p.code == "no_handler"] == []
    assert set(ENGINE_KINDS) == {"control.loop", "control.checkpoint"}


def test_preflight_descends_into_loop_bodies():
    """A waypoint referenced only from a servo loop's body would otherwise be discovered
    untaught on iteration 1 — with an arm holding an open tube."""
    plan = Plan([_loop(body=[{"kind": "arm.waypoint", "device": "right", "waypoint": "TUBE"}])])
    report = plan.preflight(known_devices=DEVICES)
    assert report.missing_waypoints() == [("right", "TUBE")]
    untaught = next(p for p in report.blocking if p.code == "waypoint_not_taught")
    assert untaught.aid == 1, "attributed to the loop, since body rows have no aid of their own"


def test_waypoint_pairs_are_deduplicated_and_cover_traverses():
    plan = Plan([
        {"kind": "arm.waypoint", "device": "right", "waypoint": "TUBE"},
        {"kind": "arm.waypoint", "device": "right", "waypoint": "TUBE"},
        {"kind": "arm.traverse", "device": "right",
         "waypoints": ["TRANSITION_MID_TABLE", "LIQUID_HANDLER_DECK"]},
    ])
    assert waypoint_pairs(plan.actions) == [
        ("right", "TUBE"),
        ("right", "TRANSITION_MID_TABLE"),
        ("right", "LIQUID_HANDLER_DECK"),
    ]


def test_an_empty_plan_is_advisory_rather_than_blocking():
    report = preflight([], known_devices=DEVICES)
    assert report.ok and report.readiness == "degraded"
    assert [p.code for p in report.advisory] == ["empty_plan"]


def test_every_problem_becomes_a_machine_readable_warning():
    """R-LOG-6 / R-UI-13: the readiness panel matches on a code, because matching on message
    text is how that quietly stops working."""
    plan = Plan([{"kind": "arm.waypoint", "device": "left", "waypoint": "HOME"}])
    warnings = plan.preflight(known_devices=DEVICES).as_warnings()
    untaught = [w for w in warnings if w.code == "waypoint_not_taught"]
    assert len(untaught) == 1 and untaught[0].device == "left"


def test_the_refusal_reason_names_every_blocking_problem():
    plan = Plan([
        {"kind": "arm.waypoint", "device": "left", "waypoint": "HOME"},
        {"kind": "lh.move_relative", "device": "ghost", "dz": 1.0},
    ])
    reason = plan.preflight(known_devices=DEVICES).reason()
    assert "HOME" in reason and "ghost" in reason


def test_inject_refused_carries_its_reason():
    error = InjectRefused("because", after_aid=3)
    assert error.reason == "because" and error.after_aid == 3
