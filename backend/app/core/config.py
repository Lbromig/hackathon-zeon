"""Backend settings + the instrument fleet definition.

Fleet is config-driven: the device manager builds one driver per entry via the
drivers registry. Edit IPs / camera sources here (or override with a JSON file
pointed to by HZ_FLEET_FILE).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

# Soft limits for hand-driven motion (teach UI). These bound what one command may
# ask for; the controller's own limits still apply underneath. `joints` is left out
# on purpose — fill it in per model from the manual (drivers/xarm/*.pdf) to get UI
# limit rails and pre-flight range checks:
#   "joints": [[-360, 360], [-150, 150], ...]
TEACH_LIMITS: dict[str, Any] = {
    "max_jog_linear": 50.0,     # mm per jog
    "max_jog_angular": 15.0,    # deg per jog
    "max_speed_linear": 200.0,  # mm/s
    "max_speed_angular": 60.0,  # deg/s
    "max_move_to_jump": 250.0,  # mm, max cartesian distance for one absolute move
}

DEFAULT_FLEET: list[dict[str, Any]] = [
    {"type": "xarm", "id": "left", "name": "Left arm", "ip": "192.168.3.13",
     "gripper": "auto", "limits": TEACH_LIMITS},
    {"type": "xarm", "id": "right", "name": "Right arm", "ip": "192.168.3.11",
     "gripper": "auto", "limits": TEACH_LIMITS},
    {"type": "opentrons", "id": "ot", "name": "Opentrons (OT-One)", "transport": "serial",
     "port": "/dev/ttyACM0"},
    {"type": "camera", "id": "on_arm", "name": "On-arm cam", "source": 0},
    {"type": "camera", "id": "external", "name": "External cam", "source": 1},
]


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


@dataclass
class Settings:
    cors_origins: list[str] = field(default_factory=lambda: ["http://localhost:5173"])
    fleet: list[dict[str, Any]] = field(default_factory=lambda: list(DEFAULT_FLEET))
    # Poses taught through the UI, persisted so they survive a backend restart.
    teach_poses_file: str = os.path.join(REPO_ROOT, "data", "teach_poses.json")

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        fleet_file = os.getenv("HZ_FLEET_FILE")
        if fleet_file and os.path.exists(fleet_file):
            with open(fleet_file) as f:
                s.fleet = json.load(f)
        s.teach_poses_file = os.getenv("HZ_TEACH_POSES_FILE", s.teach_poses_file)
        return s


settings = Settings.load()
