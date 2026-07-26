"""Artifacts must reach the log as references, so overlays are findable from the log alone.

The servo loop's whole evidence trail is its annotated frames. "The tip was 1.9 mm off" is a
number you either trust or you do not, and the overlay is what settles it — so reading the log
after a run and finding the measurement but not the picture is the position to avoid.
"""
from __future__ import annotations

import json

import pytest

from backend.app.engine import Plan, Runner
from backend.app.engine.actions import OffsetOutputs, VisionSolveOffset, _HANDLERS
from backend.app.engine.runner import _artifact_refs


class _Devices:
    def get(self, device_id): raise KeyError(device_id)
    def require_arm(self, device_id): raise KeyError(device_id)
    def require_liquid_handler(self, device_id): raise KeyError(device_id)
    def is_simulated(self, device_id): return True


def test_artifact_refs_carry_paths_and_never_bytes():
    class A:
        kind = "image/overlay"
        path = "/runs/r1/aid7_handover_cam_overlay.png"
        camera = "handover_cam"
        label = "offset 1.9 mm"
    refs = _artifact_refs([A()])
    assert refs == [{
        "kind": "image/overlay",
        "path": "/runs/r1/aid7_handover_cam_overlay.png",
        "camera": "handover_cam",
        "label": "offset 1.9 mm",
    }]
    # R-LOG-8: nothing image-sized may appear in a log record.
    assert all(len(v) < 512 for ref in refs for v in ref.values())


def test_artifact_refs_omit_empty_optional_fields():
    class A:
        kind = "image/frame"
        path = "/runs/r1/aid3.png"
        camera = None
        label = ""
    assert _artifact_refs([A()]) == [{"kind": "image/frame", "path": "/runs/r1/aid3.png"}]


def test_artifact_refs_tolerates_nothing():
    assert _artifact_refs(None) == []
    assert _artifact_refs([]) == []


def test_an_actions_overlay_appears_in_its_log_record(tmp_path, monkeypatch, caplog):
    """End to end: a handler that registers an overlay puts its path in the action's log line."""
    overlay = tmp_path / "overlay.png"
    overlay.write_bytes(b"\x89PNG\r\n\x1a\n")

    def _handler(action, ctx):
        ctx.artifact("image/overlay", str(overlay), camera="handover_cam",
                     label="offset 1.9 mm")
        return OffsetOutputs(method="tag_3d", magnitude_mm=1.9,
                             observed_axes=["x", "y", "z"])

    monkeypatch.setitem(_HANDLERS, "vision.solve_offset", _handler)

    plan = Plan([VisionSolveOffset(label="render an overlay")], name="t")
    runner = Runner(plan, devices=_Devices(), artifact_root=str(tmp_path))
    with caplog.at_level("INFO"):
        assert runner.run_to_completion(timeout=10.0, skip_preflight=True) == "complete"

    finished = [r for r in caplog.records if getattr(r, "event", "") == "action_output"]
    assert finished, "no action_output record was logged"
    refs = getattr(finished[-1], "artifacts", None)
    assert refs, "the action produced an overlay but its log record has no artifacts"
    assert refs[0]["path"] == str(overlay)
    assert refs[0]["camera"] == "handover_cam"

    # And it survives serialization, which is what the log file and the viewer actually see.
    assert json.loads(json.dumps(refs))[0]["kind"] == "image/overlay"


def test_the_result_and_the_log_agree_on_the_artifacts(tmp_path, monkeypatch):
    """The row's artifacts and the log's references must not be able to drift apart."""
    img = tmp_path / "f.png"
    img.write_bytes(b"x")

    def _handler(action, ctx):
        ctx.artifact("image/frame", str(img), camera="gripper_left_cam")
        return OffsetOutputs(method="tag_3d", magnitude_mm=0.4,
                             observed_axes=["x", "y", "z"])

    monkeypatch.setitem(_HANDLERS, "vision.solve_offset", _handler)
    plan = Plan([VisionSolveOffset()], name="t")
    runner = Runner(plan, devices=_Devices(), artifact_root=str(tmp_path))
    runner.run_to_completion(timeout=10.0, skip_preflight=True)

    result = runner.plan.result(runner.plan.actions[0].aid)
    assert [a.path for a in result.artifacts] == \
           [r["path"] for r in _artifact_refs(result.artifacts)]
