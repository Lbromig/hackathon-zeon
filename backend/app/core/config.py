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

DEFAULT_FLEET: list[dict[str, Any]] = [
    {"type": "xarm", "id": "left", "name": "Left arm", "ip": "192.168.3.13"},
    {"type": "xarm", "id": "right", "name": "Right arm", "ip": "192.168.3.14"},  # TODO confirm 2nd arm IP
    {"type": "opentrons", "id": "ot", "name": "Opentrons (OT-One)", "transport": "serial",
     "port": "/dev/ttyACM0"},
    {"type": "camera", "id": "on_arm", "name": "On-arm cam", "source": 0},
    {"type": "camera", "id": "external", "name": "External cam", "source": 1},
]


@dataclass
class Settings:
    cors_origins: list[str] = field(default_factory=lambda: ["http://localhost:5173"])
    fleet: list[dict[str, Any]] = field(default_factory=lambda: list(DEFAULT_FLEET))

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        fleet_file = os.getenv("HZ_FLEET_FILE")
        if fleet_file and os.path.exists(fleet_file):
            with open(fleet_file) as f:
                s.fleet = json.load(f)
        return s


settings = Settings.load()
