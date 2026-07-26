"""The teach checklist's backing endpoints.

The point of deriving `required_poses` from the choreography (rather than a
hand-maintained list) is that the two can never disagree. These tests pin that:
add a waypoint to the workflow and it must show up as required.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import drivers.mock  # noqa: F401  -- registers the hardware-free fleet
from backend.app.main import app
from backend.app.workflows import uncap_aspirate as wf


@pytest.fixture
def client():
    return TestClient(app)


def _choreography_pose_count() -> int:
    return sum(1 for acts in wf.CHOREOGRAPHY.values() for a in acts if a.kind == "move")


def test_required_poses_are_derived_from_the_choreography(client):
    body = client.get("/api/workflow/required_poses").json()
    assert len(body) == _choreography_pose_count()

    wanted = {(a.device, a.pose)
              for acts in wf.CHOREOGRAPHY.values() for a in acts if a.kind == "move"}
    assert {(p["device"], p["name"]) for p in body} == wanted


def test_every_required_pose_names_the_step_that_visits_it(client):
    steps = {s.key for s in wf.PLAN}
    for p in client.get("/api/workflow/required_poses").json():
        assert p["step"] in steps
        assert p["order"] >= 1


def test_untaught_bench_reports_everything_as_missing(client):
    """`isolated_teach_poses` leaves the library empty."""
    body = client.get("/api/workflow/required_poses").json()
    assert body and all(p["taught"] is False for p in body)
    assert all(p["saved_at"] == "" for p in body)


def test_taught_bench_reports_everything_as_taught(client, taught_poses):
    body = client.get("/api/workflow/required_poses").json()
    assert body and all(p["taught"] is True for p in body), \
        [p["name"] for p in body if not p["taught"]]


def test_teaching_one_pose_flips_only_that_row(client, isolated_teach_poses):
    target = next(a for acts in wf.CHOREOGRAPHY.values()
                  for a in acts if a.kind == "move")
    isolated_teach_poses.write_text(json.dumps({
        target.device: {target.pose: {"name": target.pose, "joints": [0.0] * 6,
                                      "pose": {"x": 1.0, "y": 2.0, "z": 3.0},
                                      "saved_at": "2026-01-01T00:00:00+00:00"}}
    }))
    body = client.get("/api/workflow/required_poses").json()
    taught = [p for p in body if p["taught"]]
    assert len(taught) == 1
    assert (taught[0]["device"], taught[0]["name"]) == (target.device, target.pose)
    assert taught[0]["saved_at"] == "2026-01-01T00:00:00+00:00"


def test_preflight_endpoint_agrees_with_the_orchestrator(client):
    """A green banner in the UI must mean the workflow will actually start."""
    from backend.app.services.device_manager import device_manager

    body = client.get("/api/workflow/preflight").json()
    assert body["ok"] == (not wf.preflight(device_manager))
    assert body["problems"] == wf.preflight(device_manager)
    # The banner carries the checklist too, so the UI needs only one round trip.
    assert len(body["required"]) == _choreography_pose_count()
