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

# Measured J5 (wrist bend) clearance limits, per arm, in degrees. The controller's
# self-collision detection models the arm's own links ONLY — it knows nothing about
# the RealSense camera bolted to the flange, and will happily fold the wrist until
# the camera hits the forearm (observed: collision error 31). These bounds were
# measured with scripts/find_joint_limit.py and are enforced on joint moves AND on
# cartesian moves (the driver solves IK and checks the result).
#
# Measured 2026-07-25 on the .13 arm at J3 ~= -108 deg, 5 deg margin applied, and used
# for BOTH arms: each carries a flange camera, so both have the same class of obstruction.
# The .11 arm has not been measured independently — if its camera mount differs, run
# scripts/find_joint_limit.py against it and split this into per-arm constants. Clearance
# depends on the J3/J5 pair, so re-measure before working at a markedly more folded J3.
FLANGE_CAM_J5_LIMITS = {"5": [-78.4, 95.0]}

DEFAULT_FLEET: list[dict[str, Any]] = [
    # Which control box is on which side is a bench fact, not a naming convention:
    # as of 2026-07-25 the .11 arm sits on the left, the .13 arm on the right.
    {"type": "xarm", "id": "left", "name": "Left arm", "ip": "192.168.3.11",
     "gripper": "auto", "limits": TEACH_LIMITS,
     "joint_limit_overrides": FLANGE_CAM_J5_LIMITS},
    {"type": "xarm", "id": "right", "name": "Right arm", "ip": "192.168.3.13",
     "gripper": "auto", "limits": TEACH_LIMITS,
     "joint_limit_overrides": FLANGE_CAM_J5_LIMITS},
    {"type": "opentrons", "id": "ot", "name": "Opentrons (OT-One)", "transport": "serial",
     "port": "/dev/ttyACM0"},
    # Three-camera rig: all Intel RealSense (RGB-D), all resolve to the shared world frame.
    # `serial` pins each physical unit; leave empty to bind by enumeration order.
    # Four viewpoints: one eye-in-hand camera per arm, plus two fixed ones.
    # `gripper_cam` is the RIGHT arm's — the id predates the second unit and is wired into
    # the world model (it rides `right_tcp`), kinematics, calibration and the UI, so it
    # keeps its name rather than being renamed to gripper_right_cam. The left arm's camera
    # is the newer `gripper_left_cam`. Rename both together if the asymmetry ever bites.
    {"type": "realsense", "id": "gripper_cam",  "name": "Gripper cam (right arm)", "serial": ""},
    {"type": "realsense", "id": "gripper_left_cam", "name": "Gripper cam (left arm)", "serial": ""},
    {"type": "realsense", "id": "overview_cam", "name": "Overview cam (both devices)", "serial": ""},
    {"type": "realsense", "id": "handover_cam", "name": "Handover cam (arm->OT)", "serial": ""},
]

# Which env var overrides the IP of each xArm entry (keyed by fleet id). Keyed by SIDE,
# not by an arm number: the arms get swapped between the two mounts, and a numbered var
# forces every reader to remember which number is which side. When they swap, change the
# IP values in .env — never this mapping.
XARM_IP_ENV: dict[str, str] = {"left": "XARM_LEFT_IP", "right": "XARM_RIGHT_IP"}
# Which env var overrides each RealSense camera's serial (keyed by fleet id). Plain
# "camera" (UVC/mock) entries instead read a numeric/RTSP `source` from the same var.
CAM_ENV: dict[str, str] = {
    "gripper_cam": "CAM_GRIPPER", "gripper_left_cam": "CAM_GRIPPER_LEFT",
    "overview_cam": "CAM_OVERVIEW", "handover_cam": "CAM_HANDOVER",
}
# Stream format, applied to every camera. Per-camera overrides append the fleet id,
# e.g. CAM_WIDTH_GRIPPER_CAM=848 — the on-arm camera often wants a smaller frame
# than the overview one. RealSense rejects combinations it has no profile for.
CAM_FORMAT_ENV: dict[str, str] = {"width": "CAM_WIDTH", "height": "CAM_HEIGHT", "fps": "CAM_FPS"}
# Fleet entry types that denote a camera slot. `remote` is absent on purpose: it is not
# something a single slot opts into, it is what HZ_CAMERA_HOST turns every slot into.
CAMERA_TYPES: tuple[str, ...] = (
    "realsense", "camera", "still", "mock_tag_camera", "mock_camera",
)


# repo root is one level up from this file: <repo>/core/config.py -> <repo>
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _camera_source(value: str) -> int | str:
    """Camera source is an OpenCV device index (int) or a path / RTSP URL (str)."""
    value = value.strip()
    return int(value) if value.lstrip("-").isdigit() else value


def _excluded_indices(raw: str) -> frozenset:
    """Parse CAM_EXCLUDE_INDICES ("3" or "3,4") into a set of ints.

    Junk is dropped with a warning rather than raising: an unparseable entry here must not
    stop the backend booting, but it must not silently widen access either — a typo that
    quietly dropped an exclusion would re-enable the camera it was meant to block.
    """
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            print(f"[config] ignoring CAM_EXCLUDE_INDICES entry {part!r}: not an integer")
    return frozenset(out)


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
        elif kind in ("camera", "realsense"):
            kind = _apply_camera_type(entry, eid or "")
            val = os.getenv(CAM_ENV.get(eid, ""))
            if val:
                if kind == "realsense":
                    entry["serial"] = val.strip()        # RealSense serial (string)
                else:
                    entry["source"] = _camera_source(val)  # UVC index / path / RTSP
            _apply_camera_format(entry, eid or "")
    return out


def _apply_camera_type(entry: dict[str, Any], eid: str) -> str:
    """Let CAM_<NAME>_TYPE pick the driver behind a camera slot.

    The viewpoints (gripper / overview / handover) are fixed, but how we reach a
    given unit is not. On macOS librealsense needs root, while the same D4xx also
    enumerates as a plain UVC device that any user can open — so `camera` + a
    device index keeps a viewpoint live where `realsense` cannot open at all.
    Values: realsense (RGB-D) | camera (UVC/OpenCV) | still (a saved frame replayed from
    disk, for a viewpoint whose hardware is temporarily unplugged) | mock_tag_camera.
    """
    var = CAM_ENV.get(eid)
    requested = (os.getenv(f"{var}_TYPE") if var else None) or ""
    requested = requested.strip().lower()
    if not requested or requested == entry.get("type"):
        return str(entry.get("type"))
    if requested not in CAMERA_TYPES:
        print(f"[config] ignoring {var}_TYPE={requested!r}: unknown camera driver type")
        return str(entry.get("type"))
    entry["type"] = requested
    if requested != "realsense":
        entry.pop("serial", None)      # a UVC node is addressed by index, not serial
    return requested


def _normalize_host(value: str) -> str:
    """`bench:8100`, `http://bench:8100` and a trailing slash all mean the same thing.

    Done here as well as in the driver (drivers/camera/remote.py) so that
    ``settings.camera_host`` is directly usable as a URL prefix — the API proxies
    /api/cameras/devices to it — while a hand-written fleet file that sets ``base_url``
    on a slot still gets normalized by the driver itself.
    """
    value = value.strip().rstrip("/")
    if value and "://" not in value:
        value = f"http://{value}"
    return value


def _apply_camera_host(fleet: list[dict[str, Any]], host: str) -> list[dict[str, Any]]:
    """Point every camera slot at another backend's camera API (``HZ_CAMERA_HOST``).

    All-or-nothing by design: a machine either has the cameras or it borrows them.
    Mixing the two per slot would mean two sources of truth for what a viewpoint sees,
    and a world model fed from both would be impossible to reason about.

    Applied last, so it wins over the per-device overrides *and* over a whole
    ``HZ_FLEET_FILE`` — the point of a global switch is that a clone can borrow the
    bench's eyes without editing the fleet it was given.

    Non-camera entries are untouched: the arms and the Opentrons are addressed over
    their own networks and are not affected by where the pictures come from. Note that
    this makes them the clone's *own* connections — borrowing frames from a bench does
    not mean sharing its hardware locks.
    """
    out = []
    for entry in fleet:
        if entry.get("type") not in CAMERA_TYPES:
            out.append(entry)
            continue
        eid = str(entry.get("id") or "")
        remote = {k: v for k, v in entry.items()
                  # serial/source address local hardware; carrying them over would leave
                  # a stale USB index on a slot that no longer opens any device, which
                  # reads as a configuration that is still in force.
                  if k not in ("type", "serial", "source")}
        remote["type"] = "remote"
        remote["base_url"] = host
        # Same slot ids on both ends: the viewpoints (gripper / overview / handover) are
        # the shared vocabulary. A remote that names them differently needs a fleet file.
        remote["remote_id"] = eid
        out.append(remote)
    return out


def _apply_camera_format(entry: dict[str, Any], eid: str) -> None:
    """Stream format from env: global CAM_WIDTH, or CAM_WIDTH_<FLEET_ID> per camera."""
    for key, base in CAM_FORMAT_ENV.items():
        value = os.getenv(f"{base}_{eid.upper()}") or os.getenv(base)
        if not value:
            continue
        try:
            entry[key] = int(value)
        except ValueError:
            print(f"[config] ignoring {base} for {eid}: {value!r} is not an integer")


@dataclass
class Settings:
    cors_origins: list[str] = field(default_factory=lambda: ["http://localhost:5173"])
    fleet: list[dict[str, Any]] = field(default_factory=lambda: list(DEFAULT_FLEET))
    # Poses taught through the UI, persisted so they survive a backend restart.
    teach_poses_file: str = os.path.join(REPO_ROOT, "data", "teach_poses.json")
    # Saved camera frames, one subfolder per camera id. Gitignored (bench-local, and
    # image volume grows without bound) — see core/perception/capture.py for the layout.
    capture_dir: str = os.path.join(REPO_ROOT, "temp", "captures")
    # UVC indices this machine must never open (CAM_EXCLUDE_INDICES, comma-separated).
    # A built-in laptop camera is always present, so a mis-set index streams the operator
    # instead of the bench — and the slot still looks healthy, which is worse than a slot
    # that plainly fails. Device *names* cannot be used for this: ffmpeg and OpenCV
    # enumerate AVFoundation in different orders, so a name never identifies a cv2 index.
    camera_exclude_indices: frozenset = frozenset()
    # HZ_CAMERA_HOST: another backend to take every camera feed from, e.g.
    # "http://192.168.1.174:8100". Empty means this machine owns its cameras.
    # For a laptop / a clone of the repo that has no hardware attached but still needs
    # the bench's viewpoints for detection, the world model and the Cameras tab.
    camera_host: str = ""

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

        # Last, so it overrides both the per-device vars and a whole fleet file.
        s.camera_host = _normalize_host(os.getenv("HZ_CAMERA_HOST", ""))
        if s.camera_host:
            s.fleet = _apply_camera_host(s.fleet, s.camera_host)
            print(f"[config] cameras proxied from {s.camera_host} (HZ_CAMERA_HOST)")

        s.teach_poses_file = os.getenv("HZ_TEACH_POSES_FILE", s.teach_poses_file)
        s.capture_dir = os.getenv("HZ_CAPTURE_DIR", s.capture_dir)
        s.camera_exclude_indices = _excluded_indices(os.getenv("CAM_EXCLUDE_INDICES", ""))
        return s


settings = Settings.load()
