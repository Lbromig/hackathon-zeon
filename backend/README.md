# backend — Python / FastAPI

Orchestration and the API/websocket the UI talks to. Depends on the top-level
`core/` (framework-agnostic domain logic) and `drivers/` interfaces only.

```
app/
  main.py                 FastAPI app + lifespan (loads the fleet)
  services/device_manager.py   owns live driver instances
  services/twin.py        holds the live digital-twin WorldModel
  workflows/uncap_aspirate.py  the hero workflow (middle layer: capability -> drivers)
  api/instruments.py      REST: list / connect / status / camera frame
  api/teach.py            REST: teach/jog the arms + taught-pose library
  api/calibration.py      WS: /ws/calibrate + the twin snapshot
  api/workflow.py         WS: /ws/state (live), /ws/workflow (run + retries)
  schemas.py              pydantic models
```

Domain logic (world model, calibration, motion planner, verification agents,
settings/fleet) lives in the repo-root `core/` package so it can be reused
without importing FastAPI.

## Camera API (`/api/cameras`)

| | |
|---|---|
| `GET /api/cameras` | fleet cameras: depth support, pinned serial, configured format, intrinsics |
| `GET /api/cameras/devices` | attached RealSense units — the serials for `CAM_*` in `.env` |
| `GET /api/cameras/{id}/stream` | MJPEG (`multipart/x-mixed-replace`) — point an `<img>` at it |
| `GET /api/cameras/{id}/snapshot` | single JPEG |
| `GET /api/cameras/{id}/detections` | latest detections, normalized `[0,1]` polygons |
| `POST /api/cameras/{id}/connect` | open the device; reports *why* it failed |
| `POST /api/cameras/{id}/stop` | release the device now instead of at the idle timeout |

`/ws/state` carries the same detections per camera, so the UI needs no extra
connection. `services/camera_hub.py` owns the single thread allowed to read each
device — `cv2.VideoCapture` is not safe to read from two threads, and the overlay
must describe the frame the viewer is actually looking at. Detection runs on every
Nth frame (~5 Hz at 15 fps capture) so it can never stall the video. Workers start
on the first stream viewer and stop after a short idle linger, so booting the
backend doesn't hold a webcam open.

Detection is AprilTag `tag36h11` via `core/perception/fiducials.py`.

**RGB-D cameras carry the metric half.** When a driver reports `has_depth` and
`intrinsics()` — which a RealSense does from the factory, no ChArUco pass — the hub
does three extra things per detection:

* builds the detector with the camera matrix, so each tag gets a solvePnP pose
  (`distance_m`),
* samples the aligned depth map under the tag centre (`depth_m`), taking a median
  over a small patch and ignoring zeros, since a RealSense returns 0 for "no return",
* back-projects the centre to camera-frame metres (`camera_xyz`).

Depth is preferred over the tag pose for back-projection: a 20 mm tag subtends few
pixels, so its PnP range is much noisier than a direct depth reading. Colour-only
cameras leave all three fields `null` rather than guessing. Colour and depth come
from one `capture_rgbd()` frameset, so the depth under a tag belongs to the frame
that tag was found in.

Camera-frame metres are not yet world coordinates: commanding the arm additionally
needs `T_world_cam` (`docs/CAMERA_UI_PLAN.md` C3–C4).

Which physical unit backs each viewpoint is **hardcoded in `core/cameras.py`**, pinned per
unit by AVFoundation uniqueID (an OpenCV device index is not an identity on macOS — it
renumbers, so a slot silently comes to mean a different camera). `.env` keeps only the
stream format — `CAM_WIDTH` / `CAM_HEIGHT` / `CAM_FPS`, optionally suffixed `_<FLEET_ID>` —
and the `CAM_<SLOT>_TYPE` driver escape hatch. A `CAM_<SLOT>` index is ignored with a
warning.

For hardware-free work, `fleet.mock.json` provides `mock_tag_camera` devices that
render genuine tag36h11 markers:

```bash
HZ_FLEET_FILE=fleet.mock.json uv run uvicorn backend.app.main:app --reload
```

## Teach / jog API (`/api/arms`)

Backs the frontend's Teach tab — hand-driving an arm during bring-up.

| | |
|---|---|
| `GET /api/arms` | arms with gripper kind, axis count, soft limits |
| `GET /api/arms/{id}/state` | pose, joints, gripper, fault codes, busy |
| `POST /api/arms/{id}/jog` | `{space: cartesian\|joint, axis, delta, speed?}` |
| `POST /api/arms/{id}/move_to` | `{pose}` or `{joints}` (exactly one) |
| `POST /api/arms/{id}/gripper` | `{action: open\|close\|set, width?}` |
| `POST /api/arms/{id}/home` · `/enable` · `/clear_errors` · `/stop` | |
| `GET POST /api/arms/{id}/poses`, `POST .../{name}/goto`, `DELETE .../{name}` | taught points |

Handlers are sync on purpose: the SDK blocks, and FastAPI runs sync handlers in a
threadpool, so a `wait=True` move never stalls the event loop or `/ws/state`.

Safety is enforced here rather than in the client, because the client is not the
only possible caller:

* one in-flight command per arm (non-blocking lock → `409`); `/stop` bypasses it
  by design, since an e-stop that queues behind the move it interrupts is useless
* deltas, speeds and joint targets are validated against the arm's soft limits,
  failing closed on non-finite values
* motion is refused while a fault is latched, and while the fault state can't be read
* absolute cartesian moves beyond `max_move_to_jump` (default 250 mm) are refused

Taught poses persist to `data/teach_poses.json` (`HZ_TEACH_POSES_FILE`), namespaced
per device. Go-to replays the saved **joint** angles — the arm physically reached
them, so there's no IK branch to guess at.

## Run

Dependencies live in the root `pyproject.toml` and are managed with
[uv](https://docs.astral.sh/uv/) — `uv sync` from the repo root installs everything,
including the vendored xArm SDK.

```bash
uv sync                                        # from the repo root
uv run uvicorn backend.app.main:app --reload   # from the repo root
```

Boots even with no hardware attached — drivers that fail to init are skipped,
and the SDK/opencv imports are optional.
