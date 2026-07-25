"""Backend settings + the instrument fleet definition.

Fleet is config-driven: the device manager builds one driver per entry via the
drivers registry. Per-device values (arm IPs, OT serial port, camera sources) are
read from the environment / a local ``.env`` (see ``.env.example``) so nothing
bench-specific is hardcoded. For a wholesale override, point ``HZ_FLEET_FILE`` at
a JSON file — it wins over both the defaults and the per-device env overrides.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv

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
    # Three-camera rig (all resolve to the shared world frame):
    {"type": "camera", "id": "gripper_cam",  "name": "Gripper (on-arm) cam", "source": 0},
    {"type": "camera", "id": "overview_cam", "name": "Overview cam (both devices)", "source": 1},
    {"type": "camera", "id": "handover_cam", "name": "Handover cam (arm->OT)", "source": 2},
]

# Which env var overrides the IP of each xArm entry (keyed by fleet id).
XARM_IP_ENV: dict[str, str] = {"left": "XARM_1_IP", "right": "XARM_2_IP"}
# Which env var overrides the source of each camera entry (keyed by fleet id).
CAM_SOURCE_ENV: dict[str, str] = {
    "gripper_cam": "CAM_GRIPPER", "overview_cam": "CAM_OVERVIEW", "handover_cam": "CAM_HANDOVER",
}


# repo root is one level up from this file: <repo>/core/config.py -> <repo>
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _camera_source(value: str) -> int | str:
    """Camera source is an OpenCV device index (int) or a path / RTSP URL (str)."""
    value = value.strip()
    return int(value) if value.lstrip("-").isdigit() else value


def _apply_env_overrides(fleet: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Overlay per-device env vars onto a fleet, using the same names as .env.example."""
    out = [dict(entry) for entry in fleet]  # shallow copy so defaults stay intact
    gripper = os.getenv("XARM_GRIPPER")
    payload = os.getenv("XARM_PAYLOAD_KG")
    for entry in out:
        eid, kind = entry.get("id"), entry.get("type")
        if kind == "xarm":
            ip = os.getenv(XARM_IP_ENV.get(eid, ""))
            if ip:
                entry["ip"] = ip
            if gripper:
                entry["gripper"] = gripper
            if payload:
                entry["payload_kg"] = float(payload)
        elif kind == "opentrons":
            port = os.getenv("OT_SERIAL_PORT")
            if port:
                entry["port"] = port
        elif kind == "camera":
            src = os.getenv(CAM_SOURCE_ENV.get(eid, ""))
            if src:
                entry["source"] = _camera_source(src)
    return out


@dataclass
class Settings:
    cors_origins: list[str] = field(default_factory=lambda: ["http://localhost:5173"])
    fleet: list[dict[str, Any]] = field(default_factory=lambda: list(DEFAULT_FLEET))
    # Poses taught through the UI, persisted so they survive a backend restart.
    teach_poses_file: str = os.path.join(REPO_ROOT, "data", "teach_poses.json")

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(os.path.join(REPO_ROOT, ".env"))  # no-op if the file is absent
        s = cls()

        cors = os.getenv("CORS_ORIGINS")
        if cors:
            s.cors_origins = [o.strip() for o in cors.split(",") if o.strip()]

        # A full fleet JSON wins over the per-device env overrides.
        fleet_file = os.getenv("HZ_FLEET_FILE")
        if fleet_file and os.path.exists(fleet_file):
            with open(fleet_file) as f:
                s.fleet = json.load(f)
        else:
            s.fleet = _apply_env_overrides(DEFAULT_FLEET)

        s.teach_poses_file = os.getenv("HZ_TEACH_POSES_FILE", s.teach_poses_file)
        return s


settings = Settings.load()
