# hackathon-zeon — Track C: Cooperative Uncap → Aspirate

Two **xArm Lite 6** arms cooperatively uncap a sealed tube; one arm then holds the open
tube while an **Opentrons** (the original OT-One) aspirates from it. **Cameras + AI agents
verify every step** and retry when something fails.

## Architecture (three layers)

```
frontend/   Vue 3 + Vite + Tailwind UI — three tabs: Fleet (live status, workflow
            runner), Teach (jog axes/joints, gripper, taught poses), Cameras
            (MJPEG feeds + AprilTag overlay)
core/       Framework-agnostic domain logic (no FastAPI import). Reusable by ZEON.
  worldmodel/    digital twin — scene graph of entities + poses (world frame)
  calibration/   init + vision calibration (ArUco + 3D-printed ruler + scan adapter)
  motion/        safe pick/place planner + anti-flip (upright) guard
  verification/  the "did it work?" agents (cap/grasp/pose/aspiration)
  config.py      settings + fleet definition (env-driven, see .env.example)
backend/    Python / FastAPI — API + websocket, device manager, orchestration.
            Depends on core/ and drivers/ interfaces only.
drivers/    Instrument abstraction — capability interfaces + one driver per instrument,
            built via a registry. No vendor SDK leaks above this layer.
third_party/xArm-Python-SDK/   vendored vendor SDK
docs/       ARCHITECTURE · WORKFLOW · DIGITAL_TWIN · CAPABILITY_pick_place · ZEON_INTEGRATION
```

Data flow: `Vue → FastAPI (REST/WS) → Orchestrator → capability → Driver → Device`,
with cameras feeding the verification agents that gate each step.

See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** and **[docs/WORKFLOW.md](docs/WORKFLOW.md)**
for diagrams, and **[PROJECT_PLAN.md](PROJECT_PLAN.md)** for the 24h plan and ownership.

## Quick start (Docker)

```bash
cp .env.example .env        # optional — set your arm IPs / camera sources
docker compose up           # http://localhost:5173
```

Brings up the backend (`:8000`) and the UI (`:5173`) together. Both bind-mount the
repo and run in reload mode, so editing Python or Vue takes effect live; only
dependency changes need `docker compose up --build`. Taught poses land in `./data`
on the host.

The arms are reached over TCP by IP, which works from the container's default
bridge network. **USB devices do not** — the Opentrons serial port and USB cameras
need `devices:` entries in `docker-compose.yml` (Linux hosts only; on macOS run the
backend on the host for that work).

## Quick start (uv, no Docker)

This project uses **[uv](https://docs.astral.sh/uv/)** for all Python work.

```bash
uv sync                     # create .venv + install deps (incl. vendored xArm SDK)

# initialize the arm
uv run python scripts/init_xarm.py --ip 192.168.3.13

# backend (from the repo root)
uv run uvicorn backend.app.main:app --reload

# frontend (separate shell)
cd frontend && npm install && npm run dev
```

Boots without hardware — drivers that can't init are skipped; SDK/opencv imports are optional.

For camera/vision work with no bench, run against the synthetic fleet: it renders real
AprilTag `tag36h11` markers that the detector genuinely detects, so the Cameras tab is
fully live.

```bash
HZ_FLEET_FILE=fleet.mock.json uv run uvicorn backend.app.main:app --reload
```

### RealSense cameras on macOS

Two macOS-specific facts, both of which look like broken hardware when you hit them:

1. **`pip install pyrealsense2` cannot work.** Upstream publishes no macOS wheel on PyPI —
   only manylinux and win_amd64. The bindings must be compiled from librealsense source
   against the interpreter that will import them:

   ```bash
   brew install librealsense                   # C++ SDK + rs-* CLI tools
   scripts/build_pyrealsense2_macos.sh         # builds + installs pyrealsense2 into .venv
   ```

   The wheel it produces is pinned to one python minor version *and* one CPU
   architecture, and it links the Homebrew dylib by rpath — so re-run the script after a
   python upgrade or a `brew upgrade librealsense`.

   Because it is installed outside the lockfile, a plain **`uv sync` uninstalls it again**
   (it prunes anything not in `uv.lock`, and `pyproject.toml` excludes pyrealsense2 on
   darwin by necessity). Use `--inexact`, which syncs the locked deps while leaving
   unmanaged packages in place:

   ```bash
   uv sync --inexact                              # keeps pyrealsense2 installed
   uv pip install third_party/wheels/pyrealsense2-*.whl   # recover after a plain uv sync
   ```

   Locking that wheel instead is a trap: the hash goes into `uv.lock`, so the next
   rebuild breaks `uv run` for the whole team until someone re-locks.

2. **Opening a device needs root.** librealsense reaches the camera through libusb, and
   claiming its UVC interface means seizing it from macOS's own UVC driver, which only
   root may do ([upstream note](https://github.com/realsenseai/librealsense/blob/master/doc/installation_osx.md)).
   Without root you get `RS2_USB_STATUS_ACCESS` and a misleading
   `failed to set power state` — the device still *enumerates*, so it reads as a missing
   camera rather than a privilege problem. This is unrelated to the System Settings
   Camera permission, which gates AVFoundation and does not lift the root requirement.

   ```bash
   sudo .venv/bin/python scripts/validate_camera.py --frames 30 --fiducials
   sudo rs-enumerate-devices -s                # same check, outside python
   ```

`scripts/validate_camera.py` walks the whole chain — import, enumeration, USB claim, stream
start, frame delivery, intrinsics, metric depth — and names the fix at whichever link
breaks first.

### Running the three-camera rig

`core/config.py` defines three `realsense` fleet slots (`gripper_cam`, `overview_cam`,
`handover_cam`); `camera_hub` runs one worker per camera and a missing unit is skipped at
boot, so one, two, or three cameras all work with no code change. Two things need care.

**Pin every camera by serial.** Blank serials bind by enumeration order, which is stable
only with a single camera — with two or more, the rig silently swaps viewpoints between
runs and attributes every observed pose to the wrong camera.

```bash
sudo .venv/bin/python scripts/validate_camera.py --list   # serials + .env block to paste
```

Use **the serial the SDK reports**. ioreg / `system_profiler` print a *different* serial for
the same unit (one D435 here is `125123020017` on USB but `138422075248` to the SDK), and
`cfg.enable_device()` matches the SDK value — so a USB-descriptor serial binds nothing and
gives no error.

**Verify them together, not one at a time.** Cameras that each pass alone can still fail
collectively once the USB budget or the controller's endpoints run out.

```bash
sudo .venv/bin/python scripts/validate_camera.py --all --frames 30
```

`--all` opens every attached camera at once — the condition the backend actually runs in —
reports per-camera delivery and depth validity, and prints the aggregate wire cost against
the practical USB3 ceiling. At 1280×720@30 each camera costs ~111 MB/s on the wire (YUYV
colour + Z16 depth), so three cameras need ~332 MB/s against roughly 400 MB/s usable per
controller: fine spread across separate controllers, marginal on one. Drop to
`--width 848 --height 480` (~49 MB/s each) if a camera opens alone but not alongside the
others. Note that per-camera fps also falls as cameras are added because depth→colour
alignment is host-side CPU work — that is distinct from a bandwidth fault.

Because the backend should not run as root, macOS is a development-only arrangement for
cameras: put the rig on a Linux host, where a udev rule grants plain-user access, or isolate
capture in a small privileged helper that hands frames over IPC.

**Root-free fallback (infrared, no depth).** macOS also exposes each D4xx as an ordinary UVC
webcam needing no privileges, so the `camera` driver type reaches it directly — set
`CAM_<SLOT>_TYPE=camera` and point `CAM_<SLOT>` at the device index. Two caveats that are
easy to get wrong:

- The stream is the camera's **mono infrared**, not the RGB module (macOS exposes the
  *Depth* function; the frames arrive as three identical channels, so a shape check
  misreads them as colour). AprilTags are still detectable, but the projector's dot pattern
  is superimposed on the image.
- There is no depth and no factory `intrinsics()` on this path, so `depth_m` and
  `camera_xyz` stay null. Metric work needs the `realsense` path, hence root.

UVC `source` indices are assigned by macOS and shift when a camera is added or replugged, so
re-check them after any change. Verify whatever mix you end up with:

```bash
.venv/bin/python scripts/validate_camera.py --fleet --frames 20
```

Add `--persist` to write each camera's frame to disk, one subfolder per camera — which is
how you work out which physical viewpoint a fleet id actually is:

```bash
.venv/bin/python scripts/validate_camera.py --fleet --frames 10 --persist
```

```
temp/captures/                      # gitignored; HZ_CAPTURE_DIR to relocate
  overview_cam/
    latest_color.png                # overwritten each save, for quick eyeballing
    20260726T005304_782Z_color.png  # UTC stamp: saves accumulate and sort chronologically
    20260726T005304_782Z_depth.png  # 16-bit PNG in millimetres (RGB-D slots only)
  handover_cam/
    ...
```

Depth is stored as 16-bit millimetres rather than a colourised preview, because the
measurement is the point of an RGB-D camera — read it back with
`core.perception.load_depth_mm` (plain `cv2.imread` truncates it to 8-bit). All cameras in
one run share a timestamp, so frames from the same instant line up across viewpoints.

`--fleet` builds every slot with its *configured* driver — so it validates the rig the
backend will actually run, mixed `realsense` and `camera` slots included, and needs root only
for the RealSense ones. It opens them all at once, discards the first frames for
auto-exposure (frame 1 routinely reads saturated), and reports per slot: resolution,
colour-vs-mono/IR, frames delivered, fps, depth validity, and whether intrinsics exist.
Exit code 0 = all slots live, 7 = some down, 6 = none.
