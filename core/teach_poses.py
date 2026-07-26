"""Read the pose library taught through the UI.

Poses are taught by hand (teach panel -> ``POST /api/arms/{id}/poses``) and persisted
to ``settings.teach_poses_file``. Workflows reference them *by name* so bench-specific
geometry never gets hardcoded into the choreography: re-teach a pose after the deck
moves and the workflow follows, with no code change.

Replay prefers the stored joint angles over the cartesian pose. Those angles were
physically reached, so there is no IK branch to guess at — the same reasoning as
``goto_pose`` in the teach API.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from .config import settings


class MissingPose(LookupError):
    """A workflow asked for a taught pose that nobody has taught yet.

    Raised rather than skipped: a missing pose means the arm would either not move
    or move somewhere unintended, and both are worse than refusing to start.
    """


@dataclass
class TaughtPose:
    name: str
    device_id: str
    xyz_rpy: list[float]                 # mm / deg, matching drivers.Pose
    joints: list[float] | None = None    # deg, preferred for replay
    gripper_width: float | None = None   # controller counts, if recorded


def load(path: str | None = None) -> dict[str, dict[str, Any]]:
    """The whole library: {device_id: {pose_name: entry}}. Empty if not taught yet."""
    path = path or settings.teach_poses_file
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise MissingPose(f"could not read the taught-pose library at {path}: {e}") from e
    return data if isinstance(data, dict) else {}


def get(device_id: str, name: str, path: str | None = None) -> TaughtPose:
    """One taught pose, or raise MissingPose naming what to teach."""
    library = load(path)
    entry = library.get(device_id, {}).get(name)
    if entry is None:
        known = sorted(library.get(device_id, {}))
        raise MissingPose(
            f"no taught pose {name!r} for {device_id!r}. "
            f"Teach it in the UI first (known poses: {known or 'none'})."
        )
    pose = entry.get("pose") or {}
    missing = [k for k in ("x", "y", "z") if pose.get(k) is None]
    if missing:
        raise MissingPose(f"taught pose {name!r} for {device_id!r} has no usable "
                          f"position (missing {missing})")
    return TaughtPose(
        name=name,
        device_id=device_id,
        xyz_rpy=[float(pose.get(k, 0.0)) for k in
                 ("x", "y", "z", "roll", "pitch", "yaw")],
        joints=[float(j) for j in entry["joints"]] if entry.get("joints") else None,
        gripper_width=entry.get("gripper_width"),
    )


def write(store: dict[str, dict[str, Any]], path: str | None = None) -> None:
    """Persist the whole library atomically.

    Written via a temp file and os.replace so an interrupted save can never leave
    a half-written library: the pose that was already taught is the one thing
    here that cannot be recovered by re-running anything.
    """
    path = path or settings.teach_poses_file
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, path)


def require(device_id: str, names: list[str], path: str | None = None) -> list[str]:
    """Which of ``names`` are not taught yet. Lets a workflow pre-flight in one go
    instead of failing halfway through a physical sequence."""
    library = load(path)
    taught = library.get(device_id, {})
    return [n for n in names if n not in taught]
