"""The hero workflow must actually drive the hardware.

These exist because `_execute` was previously a stub with every driver call commented
out: the plan "ran", the (stubbed) verifiers passed, and the demo reported success
while nothing moved. A workflow that silently does nothing is worse than one that
fails, so the first test here asserts real driver calls happen, and the rest assert
that the failure modes are loud.
"""
from __future__ import annotations

import json

import pytest

import drivers.mock  # noqa: F401  -- registers mock_arm / mock_liquid_handler
from drivers import build_driver

from backend.app.services.device_manager import DeviceManager
from backend.app.workflows import uncap_aspirate as wf


def _dm() -> DeviceManager:
    dm = DeviceManager()
    dm._drivers = {}
    for cfg in (
        {"type": "mock_arm", "id": "left", "name": "Left arm"},
        {"type": "mock_arm", "id": "right", "name": "Right arm"},
        {"type": "mock_liquid_handler", "id": "ot", "name": "Opentrons"},
    ):
        dm._drivers[cfg["id"]] = build_driver(cfg)
        dm._drivers[cfg["id"]].connect()
    return dm


class _Recorder:
    """Wraps a driver and records the calls the workflow makes on it."""

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return attr(*args, **kwargs)
        return record


def test_uncap_actually_moves_both_arms_and_works_the_grippers(taught_poses):
    dm = _dm()
    left, right = _Recorder(dm.get("left")), _Recorder(dm.get("right"))
    dm._drivers["left"], dm._drivers["right"] = left, right

    wf._execute(wf.PLAN[0], dm)   # the "uncap" step

    left_kinds = [c[0] for c in left.calls]
    right_kinds = [c[0] for c in right.calls]

    # The left arm clamps the tube; the right takes the cap off and parks it.
    assert "move_joints" in left_kinds or "move_to" in left_kinds, left_kinds
    assert "grip" in left_kinds, left_kinds
    assert "grip" in right_kinds and "release" in right_kinds, right_kinds
    # Right arm must actually travel: approach, grasp, lift, dropoff, retreat.
    right_moves = [k for k in right_kinds if k in ("move_to", "move_joints")]
    assert len(right_moves) >= 4, right_kinds


def test_aspirate_calls_the_liquid_handler(taught_poses):
    dm = _dm()
    ot = _Recorder(dm.get("ot"))
    dm._drivers["ot"] = ot

    wf._execute(wf.PLAN[3], dm)   # the "aspirate" step

    aspirates = [c for c in ot.calls if c[0] == "aspirate"]
    assert len(aspirates) == 1, ot.calls
    assert aspirates[0][2]["volume_ul"] > 0


def test_missing_taught_pose_raises_rather_than_silently_skipping():
    """The old stub did nothing when unconfigured. That must never come back."""
    dm = _dm()   # `isolated_teach_poses` leaves the library empty
    with pytest.raises(Exception) as excinfo:
        wf._execute(wf.PLAN[0], dm)
    assert "tube_hold_approach" in str(excinfo.value)


def test_preflight_reports_every_untaught_pose_before_anything_moves():
    dm = _dm()
    problems = wf.preflight(dm)
    assert problems, "an empty pose library must not pre-flight clean"
    assert any("left" in p for p in problems)
    assert any("right" in p for p in problems)


def test_preflight_passes_on_a_taught_bench(taught_poses):
    assert wf.preflight(_dm()) == []


def test_run_streams_a_failure_instead_of_raising_when_poses_are_missing():
    events = list(wf.run(_dm()))
    assert events, "run() must emit something"
    assert events[0]["step"] == "preflight"
    assert events[0]["phase"] == "failed"
    # And it must stop there rather than driving the arms anyway.
    assert all(e["step"] == "preflight" for e in events)


def test_run_reports_execution_failure_distinctly_from_verification_failure(
        taught_poses, monkeypatch):
    """A refused move is not a failed verification — retrying it changes nothing."""
    dm = _dm()

    def boom(act, dm_):
        raise wf.WorkflowError("simulated driver refusal")
    monkeypatch.setattr(wf, "_run_act", boom)

    events = list(wf.run(dm))
    failed = [e for e in events if e["phase"] == "failed"]
    assert failed and failed[0]["error"] == "execution"
    # One attempt only: re-running an impossible move just wastes time on hardware.
    assert failed[0]["attempt"] == 1


def test_choreography_only_references_poses_the_fixture_teaches(taught_poses):
    """Guards against a pose being added to the choreography but never taught."""
    library = json.loads(taught_poses.read_text())
    for step_key, acts in wf.CHOREOGRAPHY.items():
        for act in acts:
            if act.kind != "move":
                continue
            assert act.pose in library.get(act.device, {}), (
                f"{step_key}: {act.device} pose {act.pose!r} is in the choreography "
                f"but not in the test fixture — add it to conftest._ARM_POSE_NAMES"
            )
