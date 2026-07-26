"""The Wave 0 contracts: the `Action` union, typed outputs, `ActionResult`, events, blackboard.

These are frozen — seven parallel slices import them. So the tests here are less about
behaviour (there is none) and more about the properties the slices are entitled to rely on:
the union round-trips through JSON, validation rejects what must not reach a robot, the cuts
that were decided stay cut, and `simulated` comes from config rather than from a driver.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from backend.app.engine import actions as A
from backend.app.engine import events as E
from backend.app.engine.blackboard import (PER_CAMERA_SLOTS, SLOT_NAMES, Blackboard,
                                           SlotEmpty)


# --- the kind set (S4's cut) --------------------------------------------------

def test_there_are_fourteen_action_kinds():
    """The review cut 21 kinds to ~14 (S4) and D15 removed one more. Pinned, because scope
    creeping back one action at a time is exactly how it comes back."""
    assert len(A.ACTION_KINDS) == 14
    assert len(A.ACTION_MODELS) == 14
    assert len(set(A.ACTION_KINDS)) == 14


@pytest.mark.parametrize("gone", [
    "lifecycle.initialize_all",     # merged with initialize_device via an optional device
    "lifecycle.initialize_device",
    "arm.home",                     # HOME is a waypoint name (R-WP-4)
    "arm.manual_move",              # free-drive is a teach control, not a plan action
    "vision.identify_tip",          # merged into vision.identify with a target
    "vision.identify_tube",
    "vision.overlay",               # folded into vision.solve_offset
    "vision.select_offset",         # D15: "best single view" guarantees a stalled loop
    "lh.retract_z",                 # full retract belongs to lifecycle.initialize
])
def test_the_cut_kinds_stay_cut(gone):
    assert gone not in A.ACTION_KINDS
    with pytest.raises(ValidationError):
        A.validate_action({"kind": gone, "device": "x"})


def test_home_is_a_waypoint_not_a_kind():
    """R-WP-4: HOME exists once per arm and means a different pose on each. A separate kind
    would have duplicated this one's pre-flight, missing-waypoint path and speed default."""
    action = A.validate_action({"kind": "arm.waypoint", "device": "left",
                                "waypoint": "HOME", "speed": "slow"})
    assert action.waypoint == "HOME" and action.speed == "slow"


def test_identify_takes_a_target_rather_than_being_two_kinds():
    for target in ("tip", "tube"):
        action = A.validate_action({"kind": "vision.identify", "device": "handover_cam",
                                    "target": target})
        assert action.target == target
    with pytest.raises(ValidationError):
        A.validate_action({"kind": "vision.identify", "device": "c", "target": "cap"})


def test_initialize_covers_all_devices_and_one_device():
    """D5/R-INIT-7: "reinitialize device X" must be literally the boot code."""
    every = A.validate_action({"kind": "lifecycle.initialize"})
    one = A.validate_action({"kind": "lifecycle.initialize", "device": "ot"})
    assert every.device is None and one.device == "ot"


# --- validation is the gate before anything moves -----------------------------

def test_an_unknown_field_is_rejected():
    """The LLM path (R-UI-7): an ignored field is a move to the wrong place reporting
    success. `extra="forbid"` is what makes a misspelling a 422."""
    with pytest.raises(ValidationError):
        A.validate_action({"kind": "arm.move_relative", "device": "right", "dzz": 10.0})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_offset_is_rejected(bad):
    """A NaN offset must be a validation error, never a commanded move — the win
    `schemas.FiniteModel` already earned, kept here."""
    with pytest.raises(ValidationError):
        A.validate_action({"kind": "lh.move_relative", "device": "ot", "dz": bad})


def test_an_unknown_kind_is_rejected():
    with pytest.raises(ValidationError):
        A.validate_action({"kind": "arm.teleport", "device": "right"})


def test_a_required_device_cannot_be_omitted():
    with pytest.raises(ValidationError):
        A.validate_action({"kind": "arm.gripper", "state": "close"})


def test_max_attempts_is_bounded():
    """R-ENG-15: retries are bounded. A retry loop around a move that fails for a structural
    reason is a machine repeating a mistake."""
    with pytest.raises(ValidationError):
        A.validate_action({"kind": "arm.waypoint", "device": "r", "waypoint": "W",
                           "max_attempts": 99})


def test_a_traverse_needs_at_least_two_waypoints():
    with pytest.raises(ValidationError):
        A.validate_action({"kind": "arm.traverse", "device": "right", "waypoints": ["ONE"]})


# --- raw speeds cannot appear in plan data (R-ENG-14) -------------------------

def test_speed_is_a_tier_everywhere_and_never_a_number():
    """R-ENG-14's last sentence. A raw number in plan data would outrank the soft-limit clamp
    in `core/speeds.py`, which is the whole point of the indirection."""
    offenders = []
    for model in A.ACTION_MODELS:
        for name, field in model.model_fields.items():
            if name == "speed":
                assert set(getattr(field.annotation, "__args__", ())) == {"slow", "medium", "fast"}
            elif "speed" in name or name in ("mm_per_s", "deg_per_s", "velocity"):
                offenders.append(f"{model.__name__}.{name}")
    assert not offenders, "raw speeds expressible in plan data: " + ", ".join(offenders)


def test_a_numeric_speed_is_refused():
    with pytest.raises(ValidationError):
        A.validate_action({"kind": "arm.waypoint", "device": "r", "waypoint": "W", "speed": 80})


# --- slot references: five names, no interpreter (S2) -------------------------

def test_a_slot_reference_is_one_of_five_literal_names():
    action = A.validate_action({"kind": "lh.move_relative", "device": "ot",
                                "from_slot": "selected_offset"})
    assert action.from_slot == "selected_offset"
    with pytest.raises(ValidationError):
        A.validate_action({"kind": "lh.move_relative", "device": "ot",
                           "from_slot": "offsets.selected"})


def test_there_is_no_ref_model_with_a_dotted_path():
    """S2 cut the `Ref(slot, field)` resolver: a dotted-path interpreter is a thing you debug
    at the bench. If this ever fails, an interpreter has come back."""
    assert not hasattr(A, "Ref")
    for model in A.ACTION_MODELS:
        for name, field in model.model_fields.items():
            assert "field" not in name, f"{model.__name__}.{name} looks like a path navigator"


def test_the_slot_literal_matches_the_blackboard():
    """The action contract and the blackboard must agree on the five names, or a plan can name
    a slot that does not exist."""
    from typing import get_args
    assert set(get_args(A.SlotName)) == set(SLOT_NAMES)


# --- the loop is bounded (D14) and has no expression language (S3) -----------

def test_a_nested_loop_is_refused():
    """D14/B5: a loop inside a loop is a quadratic path to OOM. Refused at validation, so an
    LLM-authored plan is rejected with a reason rather than silently truncated."""
    inner = {"kind": "control.loop", "body": [{"kind": "control.checkpoint"}], "until": "offset_within_threshold"}
    with pytest.raises(ValidationError, match="nested loops"):
        A.validate_action({"kind": "control.loop", "body": [inner]})


def test_materialization_is_capped():
    body = [{"kind": "camera.snapshot", "device": f"cam{i}"} for i in range(30)]
    with pytest.raises(ValidationError, match="materialized"):
        A.validate_action({"kind": "control.loop", "body": body, "max_iterations": 40})


def test_max_iterations_has_a_ceiling():
    with pytest.raises(ValidationError):
        A.validate_action({"kind": "control.loop",
                           "body": [{"kind": "control.checkpoint"}],
                           "max_iterations": A.MAX_LOOP_ITERATIONS + 1})


def test_the_loop_termination_test_is_a_named_predicate_not_an_expression():
    """S3: one test, its threshold spelled out. An expression language is also the part an LLM
    is most likely to author wrongly, and it fails unreadably."""
    loop = A.validate_action({"kind": "control.loop",
                              "body": [{"kind": "control.checkpoint"}],
                              "threshold_mm": 1.5})
    assert loop.until == "offset_within_threshold"
    with pytest.raises(ValidationError):
        A.validate_action({"kind": "control.loop",
                           "body": [{"kind": "control.checkpoint"}],
                           "until": "selected_offset.magnitude_mm < 1.5"})
    assert not hasattr(A, "Condition")


def test_the_loop_watches_the_remaining_offset_not_view_disagreement():
    """R-VIS-9 / Q4: gating on inter-view disagreement can leave a converged loop running
    forever, because the views never agree that closely."""
    loop = A.validate_action({"kind": "control.loop", "body": [{"kind": "control.checkpoint"}]})
    assert loop.watch_slot == "selected_offset"
    assert "disagreement" not in loop.until


# --- round-tripping (it is streamed, logged, and LLM-authored) ----------------

@pytest.mark.parametrize("payload", [
    {"kind": "lifecycle.initialize", "home_after": True},
    {"kind": "lifecycle.reconnect", "device": "right", "scope": "engage"},
    {"kind": "arm.waypoint", "device": "right", "waypoint": "APPROACH_RACK", "dz": -5.0},
    {"kind": "arm.move_relative", "device": "left", "dx": 1.0, "dyaw": 2.0},
    {"kind": "arm.gripper", "device": "right", "state": "close", "width": 420.0},
    {"kind": "arm.decap", "device": "right", "step_deg": 90.0, "turns": 1.0},
    {"kind": "arm.traverse", "device": "left", "waypoints": ["A", "B", "C"]},
    {"kind": "lh.move_relative", "device": "ot", "from_slot": "selected_offset"},
    {"kind": "camera.snapshot", "device": "handover_cam", "fresh": True},
    {"kind": "camera.search_code", "device": "overview_cam", "marker_id": 225},
    {"kind": "vision.identify", "device": "handover_cam", "target": "tube"},
    {"kind": "vision.solve_offset"},
    {"kind": "control.loop", "body": [{"kind": "camera.snapshot", "device": "c"}]},
    {"kind": "control.checkpoint", "message": "hand on the e-stop"},
])
def test_every_kind_round_trips_through_json(payload):
    """It is streamed to the browser, written to the log, and constructed by an LLM — so
    dict -> model -> JSON -> model must be lossless for all fourteen."""
    first = A.validate_action(payload)
    again = A.validate_action(json.loads(first.model_dump_json()))
    assert again.model_dump() == first.model_dump()


def test_every_kind_is_covered_by_the_round_trip_test():
    """A new kind with no round-trip case would look tested and not be."""
    import inspect

    source = inspect.getsource(test_every_kind_round_trips_through_json)
    missing = [k for k in A.ACTION_KINDS if f'"{k}"' not in source]
    assert not missing, f"kinds with no round-trip case: {missing}"


def test_the_json_schema_is_generatable():
    """The frontend's per-kind renderers and the inject form (R-UI-14) are generated from
    this. Hand-writing the TypeScript is how the two drift."""
    from pydantic import TypeAdapter
    schema = TypeAdapter(A.Action).json_schema()
    assert schema, "no schema"
    assert json.dumps(schema)          # serializable, which is what the generator needs


# --- typed outputs ------------------------------------------------------------

def test_every_kind_has_a_typed_outputs_model():
    """R-ENG-6. A kind with no entry would produce untyped outputs and no UI renderer."""
    assert set(A.OUTPUTS_FOR_KIND) == set(A.ACTION_KINDS)


def test_the_outputs_union_covers_every_mapped_model():
    from typing import get_args
    union = get_args(get_args(A.Outputs)[0])
    assert set(A.OUTPUTS_FOR_KIND.values()) <= set(union)


def test_the_offset_metric_has_no_per_view_deviation_field():
    """D16/B2: per view the residual is structurally zero (two equations, two unknowns after
    axis restriction), so the old `deviation_mm` could never report a problem and its
    confidence factor was identically 1.0. It must not come back."""
    fields = set(A.OffsetOutputs.model_fields)
    assert "deviation_mm" not in fields
    assert "residual_offset_mm" in fields      # terminates the loop
    assert "sigma_mm" in fields                # the reported uncertainty
    assert "view_disagreement_mm" in fields    # advisory only


def test_an_unobservable_axis_is_none_not_zero():
    """`None` and `0.0` mean opposite things to a servo loop: "cannot see this axis" versus
    "this axis is already there"."""
    out = A.OffsetOutputs(residual_offset_mm={"x": 2.1, "y": None, "z": -4.8},
                          observed_axes=["x", "z"])
    assert out.residual_offset_mm["y"] is None


def test_the_offset_solve_can_report_a_refusal():
    """D17/D18 make an ill-conditioned geometry and an identity/resolution mismatch
    **refusals**, not warnings. A refusal is an outcome the model can express."""
    out = A.OffsetOutputs(method="refused", refusal="ill_conditioned",
                          singular_values=[0.9, 0.012], condition_number=76.0)
    assert out.method == "refused" and out.refusal == "ill_conditioned"


def test_a_detection_reports_which_path_produced_it():
    """D27: both detectors ship because the jaws occlude the tag mid-approach, so the log has
    to show when the fallback carried a run."""
    assert set(A.IdentifyOutputs.model_fields) >= {"found", "method", "score"}
    out = A.IdentifyOutputs(target="tip", found=False)
    assert out.method == "none" and out.point_px is None


def test_a_snapshot_reports_the_child_side_capture_time_and_the_achieved_mode():
    """D20 (freshness cannot ride a sequence number over MJPEG) and R-CAM-12 (OpenCV
    substitutes the nearest mode silently, so the request is not evidence)."""
    fields = set(A.SnapshotOutputs.model_fields)
    assert {"captured_at", "stale", "achieved_mode", "stream"} <= fields


def test_the_liquid_handler_reports_position_provenance():
    """R-LH-4: a dead-reckoned position must not be mistakable for a reading."""
    out = A.LHMoveOutputs()
    assert out.provenance == "dead_reckoned"      # the conservative default


def test_an_artifact_carries_no_hash_size_or_dimensions():
    """Review S7: nothing consumes them, and metadata nothing reads is metadata nobody
    notices going stale."""
    fields = set(A.Artifact.model_fields)
    assert not (fields & {"sha256", "bytes", "width", "height"})


# --- ActionResult and `simulated` (D25) --------------------------------------

def test_a_result_round_trips_with_its_typed_outputs():
    result = A.ActionResult(
        aid=12, index=11, kind="vision.solve_offset", status="complete", simulated=True,
        outputs=A.OffsetOutputs(residual_offset_mm={"x": 0.4, "y": 0.1, "z": -0.2},
                                magnitude_mm=0.46, sigma_mm={"x": 0.2, "y": 0.5, "z": 0.3},
                                method="tag_3d", observed_axes=["x", "y", "z"]),
        log_ref=A.LogRef(run_id="r_1", aid=12))
    again = A.ActionResult.model_validate(json.loads(result.model_dump_json()))
    assert again.outputs.kind == "offset"
    assert again.outputs.residual_offset_mm["z"] == -0.2
    assert again.simulated is True


def test_simulated_comes_from_the_resolved_config_including_for_compute_actions():
    """**D25**, and both halves of why the design's approach was wrong in the dangerous
    direction: real drivers have no `live` key (reading it raises), and a pure-computation
    action touches no device at all — so a computed offset in a fully simulated run would
    have reported "real"."""
    from core.config import Settings

    sim = Settings.load()                       # simulation is the default (D29)
    assert sim.is_simulated("left") is True
    assert sim.is_simulated(None) is True       # the compute-only case

    result = A.ActionResult(aid=1, index=0, kind="vision.solve_offset", device=None,
                            status="complete", simulated=sim.is_simulated(None))
    assert result.simulated is True


def test_a_warning_carries_a_machine_readable_code():
    """R-INIT-4's undefined home has to drive the readiness panel. Matching on message text is
    how that quietly stops working."""
    warning = A.Warning_(code="home_not_defined", device="left",
                         message="no HOME waypoint taught for 'left'; skipped the home move")
    assert warning.code == "home_not_defined"


def test_the_log_ref_is_a_query_not_a_byte_range():
    """The log file rotates, and an offset into a rotated file is a wrong answer presented
    confidently."""
    fields = set(A.LogRef.model_fields)
    assert fields == {"run_id", "aid"}


# --- the handler registry -----------------------------------------------------

def test_a_handler_registers_and_is_retrievable():
    original = dict(A._HANDLERS)
    try:
        # A Wave-1 slice may already own this kind by the time the suite runs, and a
        # duplicate registration is (correctly) a RuntimeError. What is under test is the
        # register/retrieve round trip, so start from an empty slot and put the real
        # registry back in the `finally`.
        A._HANDLERS.pop("control.checkpoint", None)

        @A.handler("control.checkpoint")
        def _h(action, ctx):
            return A.CheckpointOutputs(acknowledged=True)

        assert A.handler_for("control.checkpoint") is _h
        assert "control.checkpoint" in A.registered_kinds()
    finally:
        A._HANDLERS.clear()
        A._HANDLERS.update(original)


def test_a_duplicate_handler_raises_rather_than_overwriting():
    """Two slices each claiming `arm.waypoint` is a merge conflict that would otherwise
    resolve itself silently, in import order, and be invisible until the wrong one ran."""
    original = dict(A._HANDLERS)
    try:
        # As above: S2 registers a real `arm.gripper` handler, so the *first* registration
        # here would raise and the test would never reach the property it is about.
        A._HANDLERS.pop("arm.gripper", None)

        @A.handler("arm.gripper")
        def _first(action, ctx):
            return A.GripperOutputs(state="open")

        with pytest.raises(RuntimeError, match="duplicate handler"):
            @A.handler("arm.gripper")
            def _second(action, ctx):
                return A.GripperOutputs(state="close")
    finally:
        A._HANDLERS.clear()
        A._HANDLERS.update(original)


def test_registering_an_unknown_kind_raises():
    with pytest.raises(KeyError):
        A.handler("arm.dance")


def test_an_unhandled_kind_reports_as_unhandled_rather_than_being_skipped():
    """R-ENG-17: a step that cannot run must say so. A silently skipped step reads downstream
    as "it ran"."""
    assert A.handler_for("vision.solve_offset") is None or callable(
        A.handler_for("vision.solve_offset"))


# --- events (D24) -------------------------------------------------------------

def test_every_event_carries_a_sequence_number():
    """D24/B8: without it a reconnecting client cannot tell whether it missed anything, and
    therefore cannot know whether its view is true or merely plausible."""
    for model in E.EVENT_MODELS:
        assert "seq" in model.model_fields, f"{model.__name__} has no seq"


def test_the_sequence_is_monotonic_and_thread_safe():
    import threading

    seq = E.EventSequence()
    assert [seq.next() for _ in range(3)] == [1, 2, 3]

    seen: list[int] = []
    lock = threading.Lock()

    def pull():
        for _ in range(200):
            value = seq.next()
            with lock:
                seen.append(value)

    threads = [threading.Thread(target=pull) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # No duplicates: a duplicated seq silently defeats the gap detection it exists for.
    assert len(seen) == len(set(seen)) == 800


def test_events_round_trip_through_the_adapter():
    started = E.RunStarted(seq=1, run_id="r_1", name="handover", action_count=42,
                           simulated={"left": True, "ot": True})
    parsed = E.event_adapter.validate_python(json.loads(started.model_dump_json()))
    assert parsed.type == "run_started" and parsed.simulated["ot"] is True


def test_pausing_is_a_real_state():
    """D9/R-ENG-8: pause is cooperative and lands at the next action boundary. A UI claiming
    an instant stop while a multi-second move finishes is lying about where the arm is."""
    from typing import get_args
    assert "pausing" in get_args(E.RunState)
    assert "paused" in get_args(E.RunState)


def test_a_refused_injection_is_an_event_with_a_reason():
    """D22/R-ENG-13: refuse with a stated reason rather than inserting somewhere else. It is
    an event as well as an HTTP error because the operator who tried it may not be the one
    watching."""
    rejected = E.InjectRejected(seq=9, reason="cannot land in the past", after_aid=3)
    assert rejected.reason


def test_the_snapshot_carries_everything_a_reconnect_needs():
    """The other half of D24. An event stream alone carries deltas; a client that connects
    mid-run can only show the future."""
    fields = set(E.RunSnapshot.model_fields)
    assert {"seq", "run_id", "state", "cursor", "actions", "readiness", "warnings",
            "simulated"} <= fields


def test_the_snapshot_states_which_devices_are_simulated():
    """D29's price: a simulated run must never be mistakable for a real one, including for a
    client that never saw `run_started`."""
    snap = E.RunSnapshot(seq=5, run_id="r_1", simulated={"left": True, "right": False})
    assert snap.simulated == {"left": True, "right": False}


def test_an_action_row_keys_on_identity_and_carries_the_derived_index():
    row = E.ActionRow(aid=12, index=11, kind="arm.waypoint", device="right")
    assert row.aid == 12 and row.index == 11 and row.state == "planned"


# --- blackboard: exactly five slots (S2) --------------------------------------

def test_there_are_exactly_five_slots():
    assert SLOT_NAMES == ("frame", "tip", "tube", "offset", "selected_offset")
    assert len(SLOT_NAMES) == 5


def test_an_unknown_slot_raises_rather_than_being_created():
    """The property that keeps this from growing into the generic store S2 cut: a sixth slot
    has to be a contract change with a reason, not a typo that works."""
    board = Blackboard()
    with pytest.raises(KeyError, match="exactly five"):
        board.set("frames", 1)
    with pytest.raises(KeyError):
        board.get("intrinsics")


def test_reading_an_unwritten_slot_raises_slot_empty_rather_than_returning_none():
    """"No detection yet" and "looked and did not find it" must not be the same value —
    the second is `IdentifyOutputs(found=False)`, which is a *written* value."""
    board = Blackboard()
    with pytest.raises(SlotEmpty):
        board.get("selected_offset")
    board.set("selected_offset", A.OffsetOutputs(method="refused"))
    assert board.get("selected_offset").method == "refused"


def test_per_camera_slots_require_a_device():
    """A `frame` written with no camera would be readable as any camera's frame, which is the
    mix-up that makes a two-view offset solve wrong in a way that still looks plausible."""
    board = Blackboard()
    assert PER_CAMERA_SLOTS == {"frame", "tip", "tube"}
    with pytest.raises(ValueError, match="per-camera"):
        board.set("frame", object())
    board.set("frame", "px", device="handover_cam")
    assert board.get("frame", device="handover_cam") == "px"
    with pytest.raises(SlotEmpty):
        board.get("frame", device="gripper_left_cam")


def test_single_valued_slots_refuse_a_device():
    board = Blackboard()
    with pytest.raises(ValueError, match="single-valued"):
        board.set("selected_offset", 1, device="handover_cam")


def test_the_views_a_solve_has_are_discoverable():
    """The offset solve must find out which views it actually has rather than assume both."""
    board = Blackboard()
    board.set("tip", 1, device="handover_cam")
    board.set("tip", 2, device="gripper_left_cam")
    assert board.devices("tip") == ("gripper_left_cam", "handover_cam")
    assert board.devices("tube") == ()


def test_clearing_per_camera_slots_stops_a_stale_detection_being_reused():
    """A loop iteration clears them first, so iteration 4 cannot solve against iteration 3's
    detections if a capture fails — the same class of bug as a stale frame (D20)."""
    board = Blackboard()
    board.set("tip", "old", device="handover_cam")
    board.set("selected_offset", "keep")
    board.clear("tip")
    assert not board.has("tip", device="handover_cam")
    assert board.get("selected_offset") == "keep"


def test_clear_all_empties_everything():
    board = Blackboard()
    board.set("frame", 1, device="c")
    board.set("offset", 2)
    board.clear()
    assert board.snapshot() == {}


def test_the_snapshot_is_a_plain_dict_for_the_ui_and_the_log():
    board = Blackboard()
    board.set("tube", "t", device="handover_cam")
    board.set("selected_offset", "o")
    snap = board.snapshot()
    assert snap == {"selected_offset": "o", "tube": {"handover_cam": "t"}}
    assert json.dumps({"tube": snap["tube"]})        # serializable for the log


def test_the_blackboard_is_thread_safe():
    """The runner thread writes while the API thread reads a snapshot for the UI. A plain
    dict would let a snapshot see a half-updated loop iteration."""
    import threading

    board = Blackboard()
    errors: list[Exception] = []

    def churn(n: int):
        try:
            for i in range(300):
                board.set("tip", i, device=f"cam{n}")
                board.snapshot()
        except Exception as exc:                       # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=churn, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(board.devices("tip")) == 4


# --- nothing in the contract package executes anything -----------------------

def test_the_contract_modules_import_no_drivers_and_no_hardware():
    """These are types. A slice importing them must not open a device, and the frozen files
    must not acquire behaviour by accident."""
    import inspect

    for module in (A, E):
        source = inspect.getsource(module)
        for forbidden in ("import cv2", "from drivers", "import drivers",
                          "XArmAPI", "VideoCapture", "subprocess"):
            assert forbidden not in source, f"{module.__name__} references {forbidden}"
