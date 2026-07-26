"""Backend settings + the instrument fleet definition.

Fleet is config-driven: the device manager builds one driver per entry via the
drivers registry. Per-device values that differ between benches and networks (arm IPs,
OT serial port) are read from the environment / a local ``.env`` (see ``.env.example``).
For a wholesale override, point ``HZ_FLEET_FILE`` at a JSON file — it wins over both the
defaults and the per-device env overrides.

The **cameras are the exception, and are hardcoded** in ``core/cameras.py``. Which
physical unit provides which viewpoint is a fact about this cell rather than a
per-developer preference, and it cannot be expressed as an env var safely: the only thing
``.env`` could hold is a device index, and on macOS an index is not an identity — it
renumbers, so a slot silently comes to mean a different camera, including the operator's
built-in one. Identities live in reviewed code; ``.env`` keeps the stream format and the
driver-type escape hatch.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Literal

from dotenv import load_dotenv

# Plain stdlib logging on purpose. `core/obs/log.py` configures the handlers, and it must
# be able to read `settings.log_file` — so this module must not import it back.
log = logging.getLogger(__name__)

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
# measured with the (now deleted) scripts/find_joint_limit.py and are enforced on joint
# moves AND on cartesian moves (the driver solves IK and checks the result). The script was
# a bench diagnostic; this constant is its *output*, which is the part worth keeping.
#
# Measured 2026-07-25 on the .13 arm at J3 ~= -108 deg, 5 deg margin applied, and used
# for BOTH arms: each carries a flange camera, so both have the same class of obstruction.
# The .11 arm has not been measured independently — if its camera mount differs, recover
# find_joint_limit.py from git history (deleted in the v2 scope reduction), re-measure and
# split this into per-arm constants. Clearance depends on the J3/J5 pair, so re-measure
# before working at a markedly more folded J3.
FLANGE_CAM_J5_LIMITS = {"5": [-78.4, 95.0]}


def _bench_cameras() -> list[dict[str, Any]]:
    """The four bench cameras, pinned by AVFoundation uniqueID in core/cameras.py.

    Imported lazily-ish (module level, but in a function) so that the fleet definition
    reads as one list while the identities stay in the module that documents them.
    """
    from .cameras import fleet_entries

    return fleet_entries()

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
    # Four camera viewpoints — one eye-in-hand per arm plus two fixed — come from
    # core/cameras.py, which pins each to an AVFoundation uniqueID. They are NOT listed
    # here and NOT configured from .env: a device index is not an identity on macOS (it
    # renumbers, and a slot can silently come up aimed at the operator's own camera), so
    # the mapping belongs in reviewed code. See core/cameras.py for the reasoning.
    #
    # `gripper_cam` is the RIGHT arm's — the id predates the second unit and is wired into
    # the world model (it rides `right_tcp`), kinematics, calibration and the UI, so it
    # keeps its name rather than being renamed to gripper_right_cam. The left arm's camera
    # is the newer `gripper_left_cam`. Rename both together if the asymmetry ever bites.
    *_bench_cameras(),
]

# Which env var overrides the IP of each xArm entry (keyed by fleet id). Keyed by SIDE,
# not by an arm number: the arms get swapped between the two mounts, and a numbered var
# forces every reader to remember which number is which side. When they swap, change the
# IP values in .env — never this mapping.
XARM_IP_ENV: dict[str, str] = {"left": "XARM_LEFT_IP", "right": "XARM_RIGHT_IP"}
# Which env var names each camera slot, for the CAM_<SLOT>_TYPE escape hatch and for the
# format overrides.
#
# It no longer sets a camera's IDENTITY. The bench's four units are pinned by
# AVFoundation uniqueID in core/cameras.py, because a CAM_<SLOT>=<index> in .env is not an
# identity: macOS renumbers video devices, so an index that was right when it was written
# comes to mean a different camera — including the always-present built-in one, which makes
# a misaimed slot look healthy. A CAM_<SLOT> value is ignored for `avf` slots and warned
# about, rather than silently obeyed.
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
# `avf` is the bench default on macOS — UVC addressed by AVFoundation uniqueID, so a
# viewpoint cannot silently become a different camera (core/cameras.py, drivers/camera/avf.py).
CAMERA_TYPES: tuple[str, ...] = (
    "avf", "realsense", "camera", "still", "mock_tag_camera", "mock_camera",
)


# repo root is one level up from this file: <repo>/core/config.py -> <repo>
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _flag(value: str | None, *, default: bool) -> bool:
    """An env var as a boolean, with an explicit default for "unset"."""
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


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
            log.warning("ignoring CAM_EXCLUDE_INDICES entry %r: not an integer", part)
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
        elif kind in ("avf", "camera", "realsense"):
            kind = _apply_camera_type(entry, eid or "")
            val = os.getenv(CAM_ENV.get(eid, ""))
            if val:
                if kind == "avf":
                    # Refused, not obeyed. A leftover CAM_<SLOT>=2 in someone's .env would
                    # otherwise re-point a hardcoded viewpoint at a bare index, which is the
                    # renumbering hazard core/cameras.py exists to remove. Say so loudly:
                    # a silently ignored setting is its own kind of trap.
                    log.warning(
                        "ignoring %s=%r: %s is pinned by uniqueID in core/cameras.py. "
                        "Delete the variable, or set %s_TYPE to override the driver.",
                        CAM_ENV.get(eid), val, eid, CAM_ENV.get(eid),
                    )
                elif kind == "realsense":
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
    Values: avf (UVC pinned by uniqueID — the bench default) | realsense (RGB-D, root on
    macOS) | camera (UVC by OpenCV index) | still (a saved frame replayed from disk, for a
    viewpoint whose hardware is temporarily unplugged) | mock_tag_camera.
    """
    var = CAM_ENV.get(eid)
    requested = (os.getenv(f"{var}_TYPE") if var else None) or ""
    requested = requested.strip().lower()
    if not requested or requested == entry.get("type"):
        return str(entry.get("type"))
    if requested not in CAMERA_TYPES:
        log.warning("ignoring %s_TYPE=%r: unknown camera driver type", var, requested)
        return str(entry.get("type"))
    # Switching to the index-addressed driver without saying WHICH index would leave
    # OpenCV's default of 0 in force — and index 0 is regularly the built-in MacBook
    # camera, which is always present and so makes the misaimed slot look healthy. Refuse.
    if requested == "camera" and not (os.getenv(var or "") or "").strip():
        log.warning("ignoring %s_TYPE=camera: no %s index given, keeping %s "
                    "(an unset index would open device 0, often the built-in camera)",
                    var, var, entry.get("type"))
        return str(entry.get("type"))
    entry["type"] = requested
    if requested != "realsense":
        entry.pop("serial", None)      # a UVC node is addressed by index, not serial
    if requested != "avf":
        # These name a physical port/unit for the avf driver and mean nothing to the
        # others; leaving them on the entry would suggest the slot is still pinned.
        entry.pop("unique_id", None)
        entry.pop("usb_serial", None)
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
            log.warning("ignoring %s for %s: %r is not an integer", base, eid, value)


# --- simulation (D29: simulation is the DEFAULT) -----------------------------

# Real fleet type -> the mock/replay type that stands in for it. Simulation is achieved by
# **substituting the driver behind a device** (D2/R-SIM-2), never by an `if simulate:`
# branch above the driver layer: a simulated path that runs different code from the real
# path validates only itself.
SIM_SUBSTITUTIONS: dict[str, str] = {
    "xarm": "mock_arm",
    "opentrons": "mock_liquid_handler",
    # Cameras: `mock_tag_camera` renders real tag36h11 markers, so the whole
    # capture -> detect -> polygon -> overlay chain is exercised. S3 adds a `replay` type
    # for recorded sessions and a `servo_sim` type for the converging synthetic view; when
    # they exist, point the camera rows at them here — this table is the only place that
    # decides what stands in for what.
    "avf": "mock_tag_camera",
    "realsense": "mock_tag_camera",
    "camera": "mock_tag_camera",
    "still": "mock_tag_camera",
    "remote": "mock_tag_camera",
}

# Device *classes*, so `HZ_SIM=arms` simulates both arms without naming them. R-SIM-3
# requires per-class selection so real arms can be driven with simulated cameras.
SIM_CLASSES: dict[str, tuple[str, ...]] = {
    "arms": ("xarm",),
    "lh": ("opentrons",),
    "cameras": CAMERA_TYPES + ("remote",),
}


@dataclass(frozen=True)
class SimConfig:
    """Which devices are simulated, resolved once at boot.

    `HZ_SIM`: `all` (the **default**, D29/Q8) | `none` | a comma list of fleet ids and/or
    class names from `SIM_CLASSES`. `just backend` comes up simulated so a fresh clone runs
    the whole workflow with no configuration; `just real` sets `HZ_SIM=none`.

    The price of that default is stated in D29 and is a correctness requirement, not
    chrome: **a simulated run must never be mistakable for a real one.** Hence
    `simulated_device_ids` being resolved here, once, so `ActionResult.simulated` can be
    derived from configuration rather than from a driver's vendor string (D25) — device
    metadata has no `live` key on real drivers, and a pure-compute action touches no device
    at all, so both would have reported "real" in a fully simulated run.
    """
    spec: str = "all"                        # the raw HZ_SIM value, for reporting
    device_ids: frozenset[str] = frozenset()  # fleet ids resolved to "simulated"
    session: str = ""                        # temp/training/<session>; "" = newest
    servo: bool = False                      # servo cameras use the converging synthetic view

    @property
    def any(self) -> bool:
        return bool(self.device_ids)

    @property
    def all_devices(self) -> bool:
        return self.spec.strip().lower() == "all"


def _resolve_sim(fleet: list[dict[str, Any]], spec: str) -> frozenset[str]:
    """Fleet ids that `spec` marks simulated. Unknown tokens warn and are ignored.

    Ignored rather than fatal, but *warned* rather than silent: a typo must not stop the
    backend booting, and it must not quietly leave a device real when the operator asked
    for simulation. The warning is the only thing standing between the two.
    """
    spec = (spec or "").strip().lower()
    if spec in ("", "none", "0", "false", "off"):
        return frozenset()
    if spec in ("all", "1", "true", "on"):
        return frozenset(str(e.get("id")) for e in fleet if e.get("id"))

    by_id = {str(e.get("id")): str(e.get("type")) for e in fleet if e.get("id")}
    out: set[str] = set()
    for token in (t.strip() for t in spec.split(",")):
        if not token:
            continue
        if token in by_id:
            out.add(token)
        elif token in SIM_CLASSES:
            kinds = SIM_CLASSES[token]
            out.update(did for did, kind in by_id.items() if kind in kinds)
        elif token in SIM_SUBSTITUTIONS:          # a bare fleet type, e.g. "xarm"
            out.update(did for did, kind in by_id.items() if kind == token)
        else:
            log.warning("ignoring HZ_SIM token %r: not a fleet id, class or type", token)
    return frozenset(out)


def _apply_sim(fleet: list[dict[str, Any]], simulated: frozenset[str]) -> list[dict[str, Any]]:
    """Rewrite `entry["type"]` for every simulated device. Applied last, after the env
    overrides and after HZ_CAMERA_HOST, so simulation always wins over a real address —
    a slot cannot be simulated *and* still hold a USB index or an arm IP."""
    out = []
    for entry in fleet:
        eid, kind = str(entry.get("id") or ""), str(entry.get("type") or "")
        if eid not in simulated:
            out.append(entry)
            continue
        mock = SIM_SUBSTITUTIONS.get(kind)
        if mock is None:
            log.warning("no simulated substitute for %s type %r; leaving it real", eid, kind)
            out.append(entry)
            continue
        sim_entry = dict(entry)
        sim_entry["type"] = mock
        sim_entry["simulated"] = True
        out.append(sim_entry)
    return out


# --- camera identity + resolution schema (R-CAM-6…13) ------------------------

CameraStream = Literal["color", "ir", "unknown"]


@dataclass(frozen=True)
class CameraMode:
    """One offered stream format. All four are D4xx colour profiles; every unit is a D4xx.

    `servo_capable` is not a preference — 640x480 is **below the servo minimum** as a
    measured fact (P-1/R-CAM-10): the tube tag subtends too few pixels and detection finds
    nothing, while the same frame upscaled 2x detects tag 218. The frontend must label it.
    """
    width: int
    height: int
    fps: int = 30
    servo_capable: bool = True
    note: str = ""

    @property
    def key(self) -> str:
        return f"{self.width}x{self.height}@{self.fps}"


CAMERA_MODES: tuple[CameraMode, ...] = (
    CameraMode(640, 480, 30, servo_capable=False,
               note="low bandwidth / many cameras on one hub. BELOW THE SERVO MINIMUM "
                    "(P-1): the tube tag is undetectable here and detectable at 1280x720"),
    CameraMode(848, 480, 30, note="native D4xx wide mode; good bandwidth/detail trade"),
    CameraMode(1280, 720, 30, note="default. The measured minimum at which the tube tag "
                                   "is detectable (P-1)"),
    CameraMode(1920, 1080, 30, note="maximum detail for detection; highest USB load"),
)
DEFAULT_CAMERA_MODE: CameraMode = CAMERA_MODES[2]        # 1280x720 @30
CAMERA_MODES_BY_KEY: dict[str, CameraMode] = {m.key: m for m in CAMERA_MODES}


def mode_for(width: int, height: int, fps: int = 30) -> CameraMode | None:
    """The offered mode matching a w/h/fps, or None if it is not one of the four.

    Used to *report* an achieved mode, which is the load-bearing direction: OpenCV
    silently substitutes the nearest supported format and RealSense rejects outright
    (R-CAM-12), so what was requested is not evidence of what is streaming. S3 reads the
    format back off the device and looks it up here.
    """
    return CAMERA_MODES_BY_KEY.get(f"{width}x{height}@{fps}")


@dataclass(frozen=True)
class CameraIdentity:
    """How a slot proves it is looking at the camera it claims to be looking at.

    Every field is optional because no single route works on every unit, and the empty
    identity is meaningful: it says "this slot is bound by index alone", which R-CAM-6
    forbids for anything the offset solve depends on. **An unresolvable slot is
    `unavailable` with a stated reason — never a silent substitution** (R-CAM-7).

    Routes, in preference order, with the dead ends documented in
    `docs/v2/NOTES_camera_identity.md`:

    * `sdk_serial` — the RealSense SDK's `serial_number`, which is exactly what
      `cfg.enable_device()` matches on. Preferred whenever a slot runs the `realsense`
      driver. Needs root on macOS, which is why it cannot be the general mechanism.
    * `unique_id` + `usb_serial` — AVFoundation's `uniqueID` embeds the USB `locationID`
      (physical port), and `ioreg` gives that port's USB serial (the unit in it). Root-free.
      **Note the USB-descriptor serial differs from the SDK serial** — pairing them invents
      a mapping, so they are separate fields and never compared.
    * `reference_frame` — path to the stored per-slot frame the content fingerprint
      compares against. `startup_snapshot` already writes one every boot.

    Deliberately absent: a device *name*. ffmpeg and OpenCV enumerate AVFoundation in
    different orders (measured: ffmpeg index 3 is the D405, OpenCV index 3 is the built-in
    camera), so a name never identifies a cv2 index.
    """
    sdk_serial: str = ""
    unique_id: str = ""
    usb_serial: str = ""
    reference_frame: str = ""

    @property
    def is_bound(self) -> bool:
        """True when the slot has *some* verifiable identity beyond a bare index."""
        return bool(self.sdk_serial or self.unique_id or self.usb_serial
                    or self.reference_frame)


@dataclass(frozen=True)
class CameraSlot:
    """Per-slot camera configuration: what it should be, and how to tell that it is.

    `mode` is the *requested* mode. The achieved one is read back at runtime and lives on
    the camera's runtime state, not here — conflating the two is how a silent 2x resolution
    change becomes a silent 2x servo gain error (R-CAM-12, D18).
    """
    id: str
    mode: CameraMode = DEFAULT_CAMERA_MODE
    identity: CameraIdentity = field(default_factory=CameraIdentity)
    stream: CameraStream = "color"          # colour is the default for every camera (R-CAM-14)
    servo: bool = False                     # participates in the offset solve (D19)


# Which slots the servo loop may use. **Config, not a hardcode**: D19 forbids hardwiring
# the pair into the solve, and the pair is ultimately chosen by *measured conditioning*
# among these candidates, not by this list's order.
#
# The default is the measured evidence as of 2026-07-26 (GAP_ANALYSIS §3.1): `handover_cam`
# sees the pipette descending onto the tube with tag 225 detected on the assembly, and the
# slot named `gripper_left_cam` is physically the RIGHT arm's eye-in-hand camera and sees
# the same handover. The slot named `gripper_cam` is aimed across the room and detected
# zero tags in 53 frames, which is why it is not here. Override with HZ_SERVO_CAMERAS.
DEFAULT_SERVO_CAMERAS: tuple[str, ...] = ("handover_cam", "gripper_left_cam")


def _camera_slots(fleet: list[dict[str, Any]], servo_ids: tuple[str, ...]) -> dict[str, CameraSlot]:
    """One `CameraSlot` per camera entry in the fleet, keyed by fleet id.

    Built *from the fleet* rather than from a literal list of camera ids, so adding a
    viewpoint is a fleet edit. Nothing in this module names a camera id except
    `CAM_ENV` (which maps ids to env var names) and `DEFAULT_SERVO_CAMERAS` (config data
    with the measurement that justifies it).
    """
    slots: dict[str, CameraSlot] = {}
    for entry in fleet:
        if entry.get("type") not in CAMERA_TYPES and entry.get("type") != "remote":
            continue
        eid = str(entry.get("id") or "")
        if not eid:
            continue
        width = int(entry.get("width") or DEFAULT_CAMERA_MODE.width)
        height = int(entry.get("height") or DEFAULT_CAMERA_MODE.height)
        fps = int(entry.get("fps") or DEFAULT_CAMERA_MODE.fps)
        mode = mode_for(width, height, fps)
        if mode is None:
            # Keep the request rather than silently snapping it to an offered mode: S3
            # reports the achieved mode, and pretending the request was one of the four
            # would hide the very substitution R-CAM-12 exists to surface.
            log.warning("camera %s requests %dx%d@%d, which is not an offered mode %s",
                        eid, width, height, fps, [m.key for m in CAMERA_MODES])
            mode = CameraMode(width, height, fps, servo_capable=False,
                              note="not one of the offered modes")
        slots[eid] = CameraSlot(
            id=eid, mode=mode,
            identity=CameraIdentity(
                sdk_serial=str(entry.get("serial") or ""),
                unique_id=str(entry.get("unique_id") or ""),
                usb_serial=str(entry.get("usb_serial") or ""),
                reference_frame=str(entry.get("reference_frame") or ""),
            ),
            servo=eid in servo_ids,
        )
    return slots


def _servo_camera_ids(raw: str) -> tuple[str, ...]:
    """HZ_SERVO_CAMERAS as a comma list, else the measured default."""
    ids = tuple(t.strip() for t in raw.split(",") if t.strip())
    return ids or DEFAULT_SERVO_CAMERAS


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
    # the bench's viewpoints for detection and the Cameras tab.
    camera_host: str = ""

    # --- observability paths (R-LOG-2: ONE shared log file, one writer) -------
    # A single file, not one per subsystem: attributing a record to its run and its action
    # (R-LOG-3) is only useful if there is one place to look. The camera subprocesses do
    # not append here — they log JSONL to stdout and the supervisor re-emits through the
    # parent logger, because two writers plus rotation is a corruption hazard (D7).
    log_file: str = os.path.join(REPO_ROOT, "data", "logs", "zeon.jsonl")
    log_level: str = "INFO"
    log_max_bytes: int = 64 * 1024 * 1024      # size, not time: an image-heavy run bursts
    log_backup_count: int = 5                  # 320 MiB ceiling, bounded and predictable
    log_console: bool = True                   # human line to stderr through the same records
    # Per-run artifacts (frames, overlays). Images are NEVER inlined into a log record
    # (R-LOG-8); a record carries the path and the artifact lives here.
    artifact_dir: str = os.path.join(REPO_ROOT, "data", "runs")

    # --- simulation (D29: the default) ---------------------------------------
    sim: SimConfig = field(default_factory=SimConfig)

    # --- cameras: identity + resolution, per slot (R-CAM-6…13) ---------------
    cameras: dict[str, CameraSlot] = field(default_factory=dict)
    servo_cameras: tuple[str, ...] = DEFAULT_SERVO_CAMERAS

    # --- queries -------------------------------------------------------------
    def is_simulated(self, device_id: str | None) -> bool:
        """Whether this device is simulated, per the resolved configuration (D25).

        `None` — a pure-computation action, which touches no device — reports the run's own
        reality: in a fully simulated run a computed offset *is* simulated, because the
        pixels it read were. This is the case the design's "inspect the driver's vendor
        string" approach got wrong in the dangerous direction (B10, R-SIM-6).
        """
        if device_id is None:
            return self.sim.any
        return device_id in self.sim.device_ids

    def camera(self, device_id: str) -> CameraSlot:
        """The slot config for a camera id, defaulted rather than raising.

        A camera absent from `cameras` is a camera absent from the fleet; callers that care
        check membership. Defaulting here keeps "what resolution should this be?" from
        needing a try/except at every call site.
        """
        return self.cameras.get(device_id, CameraSlot(id=device_id))

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

        # Before simulation, so a proxied slot still gets substituted if HZ_SIM names it.
        s.camera_host = _normalize_host(os.getenv("HZ_CAMERA_HOST", ""))
        if s.camera_host:
            s.fleet = _apply_camera_host(s.fleet, s.camera_host)
            log.info("cameras proxied from %s (HZ_CAMERA_HOST)", s.camera_host)

        # Simulation is applied LAST, so it wins over the per-device vars, a whole fleet
        # file, and HZ_CAMERA_HOST. A device cannot be simulated and still hold a real
        # address. Default "all" (D29/Q8) — a fresh clone runs the workflow with no config.
        requested = os.getenv("HZ_SIM")
        spec = "all" if requested is None else requested
        device_ids = _resolve_sim(s.fleet, spec)
        if requested is None and s.camera_host:
            # HZ_CAMERA_HOST is itself an explicit statement that camera frames come from
            # somewhere real. Simulating them by default would make the setting a no-op
            # while looking like it was in force — the failure shape D29 warns about. An
            # explicit HZ_SIM is still obeyed verbatim, including "all".
            camera_ids = {cid for cid, slot in _camera_slots(s.fleet, ()).items()}
            device_ids = device_ids - camera_ids
            log.info("HZ_CAMERA_HOST is set, so the camera slots stay real; "
                     "set HZ_SIM explicitly to override")
        s.sim = SimConfig(
            spec=spec,
            device_ids=device_ids,
            session=os.getenv("HZ_SIM_SESSION", "").strip(),
            servo=_flag(os.getenv("HZ_SIM_SERVO"), default=True),
        )
        s.fleet = _apply_sim(s.fleet, device_ids)
        if device_ids:
            log.info("SIMULATED devices (HZ_SIM=%s): %s", spec, ", ".join(sorted(device_ids)))
        else:
            log.warning("REAL hardware: no device is simulated (HZ_SIM=%s)", spec)

        s.teach_poses_file = os.getenv("HZ_TEACH_POSES_FILE", s.teach_poses_file)
        s.capture_dir = os.getenv("HZ_CAPTURE_DIR", s.capture_dir)
        s.camera_exclude_indices = _excluded_indices(os.getenv("CAM_EXCLUDE_INDICES", ""))

        s.log_file = os.getenv("HZ_LOG_FILE", s.log_file)
        s.log_level = (os.getenv("HZ_LOG_LEVEL") or s.log_level).upper()
        s.log_console = _flag(os.getenv("HZ_LOG_CONSOLE"), default=s.log_console)
        s.artifact_dir = os.getenv("HZ_ARTIFACT_DIR", s.artifact_dir)

        s.servo_cameras = _servo_camera_ids(os.getenv("HZ_SERVO_CAMERAS", ""))
        s.cameras = _camera_slots(s.fleet, s.servo_cameras)
        missing = [cid for cid in s.servo_cameras if cid not in s.cameras]
        if missing:
            # A servo camera that is not in the fleet cannot be resolved later either, and
            # the offset solve would simply find one fewer view. Say so at boot.
            log.warning("HZ_SERVO_CAMERAS names %s, which are not camera slots in the fleet",
                        ", ".join(missing))
        return s


settings = Settings.load()
