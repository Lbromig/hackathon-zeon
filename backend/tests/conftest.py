"""Shared test fixtures.

The important one is pose isolation. ``core.teach_poses`` reads
``settings.teach_poses_file``, which by default points at the developer's real
``data/teach_poses.json`` — so without this, tests would pick up whatever happened to
be taught on the bench that day, and pass or fail accordingly. Every test gets a
temporary library instead, pre-populated with the poses the hero workflow's
choreography asks for.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

# Before `core.config` is imported, because `settings` is built at import time. Two reasons:
# the suite must not append to the developer's real log while it runs, and it must not leave
# a `data/logs/` behind in a fresh clone. `setdefault`, so an explicit HZ_LOG_FILE still wins.
os.environ.setdefault("HZ_LOG_FILE",
                      os.path.join(tempfile.gettempdir(), "hz-test-logs", "zeon.jsonl"))
# Quiet console handler: the lifespan configures logging, and a StreamHandler on the root
# duplicates every record into pytest's captured output for the whole session.
os.environ.setdefault("HZ_LOG_CONSOLE", "0")

from core.config import settings  # noqa: E402  -- must follow the env setup above

# The choreography in workflows/uncap_aspirate.py references these by name. Values are
# arbitrary but well-formed: the mock arms accept anything, and the real driver is
# never involved in tests. Joints are 6-axis to match the mock arm's axis_count.
_ARM_POSE_NAMES = [
    "tube_hold_approach", "tube_hold",
    "cap_grasp_approach", "cap_grasp", "cap_lift",
    "cap_dropoff", "cap_dropoff_retreat",
    "tube_grasp_approach", "tube_grasp", "transport_safe",
    "present_approach", "present_ot",
]


def _pose_entry(name: str, i: int) -> dict:
    return {
        "name": name,
        "pose": {"x": 200.0 + i, "y": 0.0, "z": 300.0 + i,
                 "roll": 180.0, "pitch": 0.0, "yaw": 0.0},
        "joints": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "gripper_width": None,
        "note": "test fixture",
        "saved_at": "2026-01-01T00:00:00+00:00",
    }


@pytest.fixture(autouse=True)
def isolated_teach_poses(tmp_path, monkeypatch):
    """Every test gets its own empty pose library.

    Empty by default so tests that exercise teaching start from a known-clean slate,
    and so anything depending on a taught pose has to say so via ``taught_poses``.

    The teardown assertion is not paranoia — it is a regression guard. ``monkeypatch`` is
    one function-scoped instance shared with the test, so a test calling
    ``monkeypatch.undo()`` to drop its *own* patch also silently reverts this one. That
    happened: the next direct ``_write_poses`` wrote to the developer's real library and
    five hand-taught poses were lost. Failing loudly here turns that from data loss into
    a red test.
    """
    path = tmp_path / "teach_poses.json"
    path.write_text("{}")
    monkeypatch.setattr(settings, "teach_poses_file", str(path))
    yield path
    in_force = str(getattr(settings, "teach_poses_file", ""))
    assert in_force == str(path), (
        "pose-file isolation was reverted during this test — writes would have hit "
        f"{in_force!r} instead of the tmp library. A monkeypatch.undo() in the test "
        "reverts this autouse fixture too; use a fresh MonkeyPatch context instead."
    )


@pytest.fixture(scope="session", autouse=True)
def _real_pose_library_is_never_touched():
    """Backstop: the bench's real taught-pose library must survive the whole session.

    Belt and braces with ``isolated_teach_poses``, which only guards tests that keep the
    fixture in force. This one compares the real file's bytes before and after the entire
    run, so any escape route — a direct write, a subprocess, a fixture ordering bug —
    still gets caught. The library is unreproducible without the bench, which is why it
    is worth a session-scoped check.
    """
    real = os.path.abspath(settings.teach_poses_file)
    before = None
    if os.path.exists(real):
        with open(real, "rb") as f:
            before = f.read()
    yield
    after = None
    if os.path.exists(real):
        with open(real, "rb") as f:
            after = f.read()
    assert after == before, (
        f"the test suite modified the real taught-pose library at {real} — those poses "
        "cost hours of hand-guiding a real arm and cannot be regenerated off-bench. "
        "Find the test that escaped the isolated_teach_poses fixture."
    )


@pytest.fixture
def taught_poses(isolated_teach_poses):
    """A bench where every pose the hero workflow needs has been taught."""
    library = {
        arm: {name: _pose_entry(name, i) for i, name in enumerate(_ARM_POSE_NAMES)}
        for arm in ("left", "right")
    }
    isolated_teach_poses.write_text(json.dumps(library))
    return isolated_teach_poses
