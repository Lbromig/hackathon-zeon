"""Calibration / initialization pipeline.

Runs once at startup, before any workflow. Each step depends on the previous, so order
matters. Emits an event per step (same shape as the workflow) so the UI can show progress,
and persists artifacts under `calib/`. Fills in the WorldModel poses as it goes.

Steps:
  1 instrument_init      connect + home every driver
  2 camera_intrinsics    per-camera intrinsics (or load cached)
  3 hand_eye             on-arm camera -> TCP
  4 world_frame          ArUco board + ruler -> world origin + metric scale
  5 arm_to_arm           both arm bases into the shared world frame
  6 locate_instruments   orbit on-arm camera -> scan package -> ot_base / rack poses
  7 register_geometry    instantiate slots / tip box / rack / wells from definitions
  8 detect_consumables   find tubes / caps / tips; set initial states
  9 freeze               persist artifacts; twin is live
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, Protocol

from ..worldmodel import WorldModel, add_tip_box, add_tube_rack, add_tube_with_cap, build_skeleton
from .markers import MARKER_MAP
from .scan import OrbitPlanner, PlaceholderScanAdapter, ScanAdapter

CALIB_DIR = Path("calib")


class DeviceManagerLike(Protocol):
    """Just the device-manager surface this pipeline needs.

    ``core`` must not import the backend, so instead of depending on
    ``backend.app.services.device_manager.DeviceManager`` we accept anything that
    can connect the fleet and hand back a driver by id.
    """

    def connect_all(self) -> dict[str, str]: ...

    def get(self, device_id: str) -> Any: ...


class CalibrationPipeline:
    def __init__(self, dm: DeviceManagerLike, scan: ScanAdapter | None = None,
                 calib_dir: Path = CALIB_DIR) -> None:
        self.dm = dm
        self.scan = scan or PlaceholderScanAdapter()
        self.dir = calib_dir
        self.wm = build_skeleton()

    def run(self) -> Iterator[dict]:
        for step in (
            self._instrument_init, self._camera_intrinsics, self._hand_eye,
            self._world_frame, self._arm_to_arm, self._locate_instruments,
            self._register_geometry, self._detect_consumables, self._freeze,
        ):
            name = step.__name__.lstrip("_")
            yield {"phase": "started", "step": name}
            detail = step()
            yield {"phase": "done", "step": name, "detail": detail or ""}
        yield {"phase": "complete", "entities": len(self.wm.entities)}

    # --- steps (stubs with clear TODOs) -----------------------------------
    def _instrument_init(self) -> str:
        return json.dumps(self.dm.connect_all())

    def _camera_intrinsics(self) -> str:
        # TODO: charuco/checkerboard capture -> per-camera intrinsics; cache to calib/intrinsics
        return "load or calibrate intrinsics (TODO)"

    def _hand_eye(self) -> str:
        # TODO: on-arm camera <-> TCP hand-eye (e.g. cv2.calibrateHandEye); cache
        return "hand-eye gripper_cam -> right_tcp (TODO)"

    def _world_frame(self) -> str:
        # TODO: detect ArUco board + 3D-printed ruler -> world origin + metric scale
        return "world frame from board + ruler (TODO)"

    def _arm_to_arm(self) -> str:
        # TODO: resolve left_base / right_base into world (markers 10/11)
        return "both arm bases -> world (TODO)"

    def _locate_instruments(self) -> str:
        # Orbit the on-arm camera and hand frames to the scan package.
        planner = OrbitPlanner()
        frames = self.dir / "instruments" / "ot"
        frames.mkdir(parents=True, exist_ok=True)
        # TODO: for pose in planner.poses_around(ot_estimate): move right arm, capture frame
        artifacts = self.scan.run_scan("ot", frames)
        T = self.scan.load_pose(artifacts)
        self.wm.set_world_pose("ot_base", T)
        return f"scanned ot -> {artifacts}"

    def _register_geometry(self) -> str:
        # Instantiate labware grids from definitions under their slots.
        add_tip_box(self.wm, "slot_1")
        add_tube_rack(self.wm, "slot_2")
        return "registered tip box (slot_1) + tube rack (slot_2)"

    def _detect_consumables(self) -> str:
        # TODO: detect which wells/tips are occupied via vision; seed a demo tube for now.
        add_tube_with_cap(self.wm, "rack_1_A1", "tube_1")
        return "seeded tube_1 (+cap) in rack_1_A1"

    def _freeze(self) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "entities.json").write_text(json.dumps(self.wm.snapshot(), indent=2))
        (self.dir / "marker_map.json").write_text(
            json.dumps({k: v.entity_id for k, v in MARKER_MAP.items()}, indent=2))
        return f"persisted twin ({len(self.wm.entities)} entities) -> {self.dir}"
