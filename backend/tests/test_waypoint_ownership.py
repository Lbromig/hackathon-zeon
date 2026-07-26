"""Per-arm waypoint ownership (R-WP-1…5).

The failure every test here exists to prevent is one arm moving to a point taught on the
*other* arm. That is a collision on a shared table, so the bar is not "usually resolves
correctly" — it is "cannot express the wrong pairing, and says which arm is wrong when
asked to".
"""
from __future__ import annotations

import json

import pytest

from core import teach_poses, waypoints


def write_library(path, library: dict) -> None:
    path.write_text(json.dumps(library))


def entry(name: str, *, joints=True, pose=True, saved_at="2026-07-26T09:00:00+00:00") -> dict:
    return {
        "name": name,
        "pose": ({"x": 200.0, "y": 0.0, "z": 300.0,
                  "roll": 180.0, "pitch": 0.0, "yaw": 0.0} if pose else None),
        "joints": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0] if joints else None,
        "gripper_width": 420.0,
        "note": "",
        "saved_at": saved_at,
    }


# --- the spec itself ----------------------------------------------------------------

def test_spec_is_the_fifteen_workflow_waypoints():
    # 9 right-arm + 4 left-arm + HOME once per arm = 15 (device, name) pairs to teach.
    assert len(waypoints.SPEC) == 15
    assert len(waypoints.names_for("right")) == 10        # 9 + its own HOME
    assert len(waypoints.names_for("left")) == 5          # 4 + its own HOME
    assert waypoints.owners_of("HOME") == ("left", "right")


def test_right_arm_owns_the_transition_and_liquid_handler_waypoints():
    """The brief prefixes these `LEFT_ARM_*` while the right arm acts (Q3). Right owns
    them. This test is the tripwire against "fixing" that back."""
    for name in ("TRANSITION_ROBOT_TABLE", "TRANSITION_MID_TABLE",
                 "TRANSITION_LIQUID_HANDLER_TABLE",
                 "LIQUID_HANDLER_APPROACH_DECK", "LIQUID_HANDLER_DECK"):
        assert waypoints.owners_of(name) == ("right",), name


def test_no_waypoint_name_carries_a_device_prefix():
    """Q3 dropped the prefix: a device prefix inside a device-scoped key is redundancy."""
    for s in waypoints.SPEC:
        assert not s.name.startswith(("LEFT_ARM_", "RIGHT_ARM_")), s.name


def test_every_waypoint_is_owned_by_exactly_one_device_except_home():
    for s in waypoints.SPEC:
        owners = waypoints.owners_of(s.name)
        assert len(owners) == (2 if s.name == waypoints.HOME else 1), s.name


def test_speed_tiers_follow_the_workflow():
    tier = {(s.device, s.name): s.speed for s in waypoints.SPEC}
    assert tier[("right", "TUBE")] == "slow"
    assert tier[("left", "CAP_GRAB")] == "slow"
    assert tier[("left", "CAP_STORE")] == "slow"
    assert tier[("right", "APPROACH_TUBE_TRANSFER")] == "medium"
    assert tier[("left", "HOME")] == tier[("right", "HOME")] == "slow"
    for name in ("APPROACH_RACK", "APPROACH_TUBE_GRAB"):
        assert tier[("right", name)] == "fast"
    for name in ("APPROACH_CAP_GRAB", "APPROACH_CAP_STORE"):
        assert tier[("left", name)] == "fast"


def test_specs_are_ordered_by_workflow_step():
    for device in waypoints.DEVICES:
        steps = [s.step for s in waypoints.specs_for(device)]
        assert steps == sorted(steps)
    # Steps 14-18 are pinned by the brief to the transitions and the deck approach.
    by_step = {(s.step, s.device): s.name for s in waypoints.SPEC}
    assert by_step[(14, "right")] == "TRANSITION_ROBOT_TABLE"
    assert by_step[(18, "right")] == "LIQUID_HANDLER_DECK"


def test_every_waypoint_has_a_note_saying_what_the_arm_is_doing():
    for s in waypoints.SPEC:
        assert len(s.note) > 20, s.name


# --- ownership refusals -------------------------------------------------------------

def test_a_waypoint_the_device_does_not_own_is_refused_naming_both_arms():
    with pytest.raises(waypoints.WaypointNotOwned) as e:
        waypoints.assert_owned("right", "CAP_GRAB")
    message = str(e.value)
    assert "'left'" in message and "'right'" in message
    assert "CAP_GRAB" in message
    assert e.value.owners == ("left",)


def test_ownership_is_checked_before_the_library_is_even_read(isolated_teach_poses):
    """A cross-arm move is refused whether or not the other arm has taught it — the check
    is on the spec, not on what happens to be on disk."""
    write_library(isolated_teach_poses, {"right": {"CAP_GRAB": entry("CAP_GRAB")}})
    with pytest.raises(waypoints.WaypointNotOwned):
        waypoints.resolve("right", "CAP_GRAB")


def test_home_resolves_per_arm_to_different_poses(isolated_teach_poses):
    left, right = entry("HOME"), entry("HOME")
    right["joints"] = [90.0] * 6
    write_library(isolated_teach_poses, {"left": {"HOME": left}, "right": {"HOME": right}})

    assert waypoints.resolve("left", "HOME").joints == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    assert waypoints.resolve("right", "HOME").joints == [90.0] * 6


def test_no_fallback_to_a_same_named_waypoint_on_another_arm(isolated_teach_poses):
    """The whole point. `left` has taught HOME, `right` has not: resolving HOME for
    `right` must refuse, not silently return the left arm's home."""
    write_library(isolated_teach_poses, {"left": {"HOME": entry("HOME")}})
    with pytest.raises(waypoints.WaypointNotTaught) as e:
        waypoints.resolve("right", "HOME")
    assert "not taught" in str(e.value)
    # It says the left arm has one, and says it is deliberately not used.
    assert "deliberately not used" in str(e.value)


def test_an_ad_hoc_name_taught_on_another_arm_is_refused_naming_that_arm(isolated_teach_poses):
    """A scratch point is device-scoped too. Asking `right` for `left`'s scratch point
    names the arm that has it rather than pretending it does not exist."""
    write_library(isolated_teach_poses, {"left": {"scratch_1": entry("scratch_1")}})
    with pytest.raises(waypoints.WaypointNotOwned) as e:
        waypoints.resolve("right", "scratch_1")
    assert "'left'" in str(e.value) and "'right'" in str(e.value)


def test_resolution_never_depends_on_dict_order(isolated_teach_poses):
    """Insertion order of the devices in the file must not change the answer — the naive
    'first device with this name wins' bug is order-dependent by construction."""
    a = {"left": {"HOME": entry("HOME")}, "right": {"HOME": entry("HOME")}}
    b = {"right": a["right"], "left": a["left"]}
    for library in (a, b):
        write_library(isolated_teach_poses, library)
        assert waypoints.resolve("right", "HOME").device == "right"
        assert waypoints.resolve("left", "HOME").device == "left"


def test_an_untaught_but_legitimate_pairing_says_what_to_teach(isolated_teach_poses):
    write_library(isolated_teach_poses, {})
    with pytest.raises(waypoints.WaypointNotTaught) as e:
        waypoints.resolve("right", "TUBE")
    assert e.value.pair == ("right", "TUBE")
    assert "teach tab" in str(e.value)
    # Still a MissingPose, so existing pre-flight handlers keep working.
    assert isinstance(e.value, teach_poses.MissingPose)


def test_an_unknown_name_lists_what_the_device_does_own(isolated_teach_poses):
    write_library(isolated_teach_poses, {})
    with pytest.raises(waypoints.WaypointNotTaught) as e:
        waypoints.resolve("right", "NOT_A_WAYPOINT")
    assert "NOT_A_WAYPOINT" in str(e.value)


def test_a_stored_waypoint_with_no_usable_target_is_refused(isolated_teach_poses):
    write_library(isolated_teach_poses,
                  {"right": {"TUBE": entry("TUBE", joints=False, pose=False)}})
    with pytest.raises(waypoints.WaypointNotTaught) as e:
        waypoints.resolve("right", "TUBE")
    assert "unusable" in str(e.value)


def test_resolve_carries_the_spec_tier_and_prefers_joints(isolated_teach_poses):
    write_library(isolated_teach_poses, {"right": {"TUBE": entry("TUBE")}})
    r = waypoints.resolve("right", "TUBE")
    assert r.is_spec and r.speed == "slow"
    assert r.joints == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    assert r.xyz_rpy[:3] == [200.0, 0.0, 300.0]
    assert r.saved_at == "2026-07-26T09:00:00+00:00"


def test_an_ad_hoc_point_resolves_with_no_tier(isolated_teach_poses):
    write_library(isolated_teach_poses, {"right": {"scratch": entry("scratch")}})
    r = waypoints.resolve("right", "scratch")
    assert not r.is_spec and r.speed is None


# --- pre-flight reporting (R-WP-5) --------------------------------------------------

def test_missing_is_reported_as_device_name_pairs(isolated_teach_poses):
    write_library(isolated_teach_poses, {"right": {"TUBE": entry("TUBE")}})
    gaps = waypoints.missing("right")
    assert ("right", "TUBE") not in gaps
    assert ("right", "APPROACH_RACK") in gaps
    assert all(isinstance(p, tuple) and len(p) == 2 for p in gaps)


def test_missing_for_plan_dedupes_and_keeps_the_device(isolated_teach_poses):
    write_library(isolated_teach_poses, {"left": {"HOME": entry("HOME")}})
    gaps = waypoints.missing_for_plan(
        [("left", "HOME"), ("right", "HOME"), ("right", "HOME"), ("right", "TUBE")])
    assert gaps == [("right", "HOME"), ("right", "TUBE")]


def test_progress_counts_taught_waypoints_for_the_bench_checklist(isolated_teach_poses):
    taught = {n: entry(n) for n in waypoints.names_for("right")[:7]}
    write_library(isolated_teach_poses, {"right": taught})
    p = waypoints.progress("right")
    assert (p.taught, p.total) == (7, 10)
    assert not p.complete
    assert p.extra == []


def test_progress_lists_scratch_points_separately(isolated_teach_poses):
    write_library(isolated_teach_poses,
                  {"right": {"TUBE": entry("TUBE"), "scratch": entry("scratch")}})
    p = waypoints.progress("right")
    assert p.taught == 1 and p.extra == ["scratch"]


def test_status_reports_step_tier_note_and_saved_at(isolated_teach_poses):
    write_library(isolated_teach_poses, {"left": {"CAP_GRAB": entry("CAP_GRAB")}})
    rows = {r.name: r for r in waypoints.status("left")}
    assert rows["CAP_GRAB"].taught and rows["CAP_GRAB"].has_joints
    assert rows["CAP_GRAB"].saved_at == "2026-07-26T09:00:00+00:00"
    assert rows["CAP_GRAB"].speed == "slow" and rows["CAP_GRAB"].step == 6
    assert rows["CAP_STORE"].taught is False and rows["CAP_STORE"].saved_at is None


# --- library problems ---------------------------------------------------------------

def test_a_spec_waypoint_taught_under_the_wrong_arm_is_a_blocking_problem(isolated_teach_poses):
    write_library(isolated_teach_poses, {"right": {"CAP_GRAB": entry("CAP_GRAB")}})
    wrong = [p for p in waypoints.problems() if p.kind == "wrong_device"]
    assert [(p.device, p.name) for p in wrong] == [("right", "CAP_GRAB")]
    assert wrong[0].blocking
    assert "'left'" in wrong[0].detail
    # ...and the arm that should have it still reads as untaught.
    assert ("left", "CAP_GRAB") in waypoints.missing("left")


def test_a_taught_pose_not_in_the_spec_is_reported_not_ignored(isolated_teach_poses):
    write_library(isolated_teach_poses, {"right": {"rack_A1_above": entry("rack_A1_above")}})
    ad_hoc = [p for p in waypoints.problems() if p.kind == "ad_hoc"]
    assert [p.name for p in ad_hoc] == ["rack_A1_above"]
    assert not ad_hoc[0].blocking      # scratch points are wanted, just not on the list


def test_a_near_miss_name_is_called_out(isolated_teach_poses):
    """'home' is not 'HOME'. Without this the operator teaches it, the checklist still
    says untaught, and nothing explains why."""
    write_library(isolated_teach_poses, {"right": {"home": entry("home")}})
    kinds = {p.kind: p for p in waypoints.problems()}
    assert "name_mismatch" in kinds
    assert "case-sensitive" in kinds["name_mismatch"].detail


def test_an_empty_library_reports_all_fifteen_as_missing(isolated_teach_poses):
    write_library(isolated_teach_poses, {})
    blocking = [p for p in waypoints.problems() if p.blocking]
    assert len(blocking) == 15
    assert all(p.kind == "missing" for p in blocking)


def test_a_fully_taught_bench_has_no_blocking_problems(isolated_teach_poses):
    library: dict = {d: {} for d in waypoints.DEVICES}
    for s in waypoints.SPEC:
        library[s.device][s.name] = entry(s.name)
    write_library(isolated_teach_poses, library)
    assert [p for p in waypoints.problems() if p.blocking] == []
    assert all(waypoints.progress(d).complete for d in waypoints.DEVICES)


def test_a_corrupt_library_raises_rather_than_reading_as_untaught(isolated_teach_poses):
    """A truncated file must not look like "nothing is taught" — that reads as a bench
    someone forgot to teach, when in fact the data is there and unreadable."""
    isolated_teach_poses.write_text("{not json")
    with pytest.raises(teach_poses.MissingPose):
        waypoints.status("right")
