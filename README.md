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

## Running the apps

Two long-running processes, both started from the repo root. Python work goes through
**[uv](https://docs.astral.sh/uv/)**; the frontend through npm.

| App | Command | Listens on |
|---|---|---|
| **backend** — FastAPI REST + websockets | `uv run uvicorn backend.app.main:app --reload` | `127.0.0.1:8000` |
| **frontend** — Vue 3 / Vite dev server | `cd frontend && npm run dev` | `0.0.0.0:5173` |

First time only:

```bash
cp .env.example .env         # optional — arm IPs, camera serials, fleet overrides
uv sync                      # create .venv + install deps (incl. vendored xArm SDK)
cd frontend && npm install   # frontend deps
```

Then, in two shells:

```bash
uv run uvicorn backend.app.main:app --reload   # shell 1 — from the repo root
cd frontend && npm run dev                     # shell 2
```

Open **http://localhost:5173**. That backend comes up **simulated** — see the next section
to drive the real bench. There is no single "start everything" command and no container
(see below) — the two processes are independent, and either can be restarted without the
other. Start the backend first: Vite proxies `/api` and `/ws` to it (`BACKEND_URL`, default
`http://127.0.0.1:8000`), so until it answers the UI shows every device as disconnected.

Run the backend **from the repo root**, not from `backend/` — `core/`, `drivers/` and
`backend/` share one import root.

The same commands are wrapped in the [justfile](justfile) ([just](https://github.com/casey/just)):

| Recipe | Does |
|---|---|
| `just sync` | `uv sync` |
| `just backend` (or `just dev`) | the backend, **simulated** (`HZ_SIM=all`) |
| `just real` | the backend on **real hardware** (`HZ_SIM=none`) |
| `just real-cameras` | real cameras, simulated motion (`HZ_SIM=arms,lh`) |
| `just sim-only left,ot` | simulate just those, drive the rest |
| `just frontend` | `npm install` + the frontend dev server |
| `just test` | the test suite, simulated |
| `just cameras` | identify the four cameras + snapshot each |
| `just logs [n]` | pretty-print the tail of `data/logs/zeon.jsonl` |
| `just reap` | kill orphaned camera child processes (see below) |
| `just init-arm ip=192.168.3.13` | initialize one arm |

The sim recipes set `HZ_SIM` explicitly rather than relying on the default, so they still
mean what they say on a machine whose `.env` sets `HZ_SIM` to something else.

If a camera comes up "busy" after a crash, `just reap` clears it. Opening a UVC device on
macOS can block in an uninterruptible kernel wait that `kill -9` cannot reap, so a capture
child can outlive the backend and keep the device claimed.

Building the frontend for a non-dev serve is `npm run build` (type-checks, emits
`frontend/dist/`) then `npm run preview`; nothing in the stack requires it.

### Running against real hardware

**Simulation is the default.** With `HZ_SIM` unset every device is replaced by a mock, so a
fresh clone runs the whole workflow with no bench attached. To drive the real cell, set
`HZ_SIM=none`:

```bash
HZ_SIM=none uv run uvicorn backend.app.main:app --reload
```

Check the mode from the backend's own boot log rather than by eye — it says one or the other
every start:

```
INFO     SIMULATED devices (HZ_SIM=all): gripper_cam, gripper_left_cam, handover_cam, left, ot, overview_cam, right
WARNING  REAL hardware: no device is simulated (HZ_SIM=none)
```

The real-hardware line is a **warning** on purpose: the expensive mistake is thinking a run
was simulated when it was about to move an arm.

`HZ_SIM` also takes a comma list, so motion and vision can be mixed independently — real
cameras with simulated arms is the safe combination for vision work at an occupied bench:

| `HZ_SIM=` | Simulated | Real |
|---|---|---|
| unset, or `all` | everything | — |
| `none` | — | everything |
| `arms,lh` | both arms, Opentrons | all four cameras |
| `cameras` | all four cameras | both arms, Opentrons |
| `left,overview_cam` | just those two | the rest |

Tokens are device *classes* (`arms`, `lh`, `cameras`), fleet ids (`left`, `right`, `ot`,
`overview_cam`, `handover_cam`, `gripper_cam`, `gripper_left_cam`) or bare fleet types
(`xarm`). An unrecognised token is **ignored with a warning, not fatal** — so read the log:
a typo leaves that device real.

Simulation is applied last, after the env overrides, a whole `HZ_FLEET_FILE` and
`HZ_CAMERA_HOST`, so a device can never be simulated *and* still hold a real address. One
deliberate exception: if `HZ_CAMERA_HOST` is set and `HZ_SIM` is unset, the camera slots
stay real, because borrowing another bench's cameras is already a statement that frames come
from somewhere real. An explicit `HZ_SIM` is still obeyed verbatim.

Two switches apply only to simulated runs (`core/config.py`, `SimConfig`):
`HZ_SIM_SESSION` picks which recorded session under `temp/training/` is replayed (empty =
newest), and `HZ_SIM_SERVO` (default on) gives servo cameras the converging synthetic view.

**Before the first real run:**

- **Arms** — set `XARM_LEFT_IP` / `XARM_RIGHT_IP` in `.env`, and give each arm one
  initialization pass per power cycle (enable servos, clear latched faults, home) before the
  API can move it:

  ```bash
  uv run python scripts/init_xarm.py --ip 192.168.3.13
  ```

- **Cameras** — attach all four; which unit is which viewpoint is pinned in code
  ([core/cameras.py](core/cameras.py)), not in `.env`. Verify with
  `.venv/bin/python scripts/identify_cameras.py --snapshot`.
- **Opentrons** — set `OT_SERIAL_PORT`.

A real run degrades rather than refusing: drivers that fail to init are skipped, so a
missing arm or camera leaves that one device disconnected and the rest live.

### What happens at backend start

The lifespan in [backend/app/main.py](backend/app/main.py) loads the fleet from
`core/config.py` + `.env`, writes one boot frame per camera slot to
`temp/captures/<slot>/`, then starts the FK→twin and camera→twin fusion threads. Drivers
that fail to init are skipped and the SDK/opencv imports are optional, so the backend
**boots with no hardware attached** — a laptop gets the full API with every device
disconnected. On macOS the `realsense` slots are the one exception: they need root, which
the backend deliberately does not run as (see *RealSense cameras on macOS* below).

Start-time switches worth knowing:

| Variable | Effect |
|---|---|
| `HZ_FLEET_FILE=fleet.mock.json` | replaces the whole fleet with synthetic devices |
| `HZ_CAMERA_HOST=http://<bench>:8000` | borrow every camera feed from another backend, all-or-nothing (that backend needs `--host 0.0.0.0` to be reachable; MJPEG carries no depth) |
| `HZ_STARTUP_SNAPSHOT=0` | skip the boot frame capture |
| `HZ_CAPTURE_DIR` | relocate `temp/captures/` |
| `BACKEND_URL` | frontend only — proxy target when the backend is on another host or port |

For camera/vision work with no bench, run against the synthetic fleet: it renders real
AprilTag `tag36h11` markers that the detector genuinely detects, so the Cameras tab is
fully live.

```bash
HZ_FLEET_FILE=fleet.mock.json uv run uvicorn backend.app.main:app --reload
```

### Ports and who can reach them

The two processes differ, and the difference is easy to miss:

- **backend** — uvicorn's default bind, so **loopback only**. Nothing on the network
  reaches `:8000` unless you add `--host 0.0.0.0`.
- **frontend** — `npm run dev` is `vite --host`, so it listens on **every interface**,
  reachable as `http://<this-machine>:5173` from the bench WiFi.

That combination is deliberate but sharp-edged: the Vite proxy runs server-side, so
anyone who loads `:5173` also gets `/api` and `/ws` — the *whole* unauthenticated API,
teach and motion endpoints included, arms included. CORS does not gate this (the browser
sees one origin), and there is no auth anywhere in the app. Use `npm run dev:local` for
a loopback-only dev server when you don't need a second machine. See
**[docs/CAMERA_ACCESS.md](docs/CAMERA_ACCESS.md)**.

### Why on the host, and not in a container

The cameras are USB (UVC/RealSense) and the Opentrons is on a serial port, and macOS
cannot pass either into a Linux VM. A containerised backend reports every camera as
`disconnected`, so there is no compose file to run.

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

### AprilTag detection tuning

`core/perception/fiducials.py` does not use OpenCV's default detector parameters. Two
things dominate how many 20 mm `tag36h11` markers get found, and they were measured on real
frames from this bench rather than reasoned about:

**Capture resolution is the bigger lever.** At 640×480 the handover camera averaged 0.5 tags
per frame and saw markers in 45% of frames; at 1280×720 it averages ~4 and hits 100%. A
20 mm tag across the cell is only a few pixels wide, and the detector discards it on size
before it ever tries to decode. Keep `CAM_WIDTH=1280` / `CAM_HEIGHT=720` unless bandwidth
forces otherwise.

**Detector parameters** (`tuned_parameters()`) add on top of that — +76% detections at
640×480, +9% at 1280×720, and 0→3 on a badly under-exposed frame. The adaptive-threshold
window range is most of it: the default 3..23 assumes even lighting, and a bench has a lit
deck beside a shadowed corner.

Two findings worth not re-discovering:

- **`CORNER_REFINE_APRILTAG` returns zero detections here**, despite the tags being
  AprilTags — measured 0 where every other mode returned 51 over the same 60 frames. It can
  reject, not just refine. `CORNER_REFINE_SUBPIX` gives identical recall plus sub-pixel
  corners, which is what makes the solvePnP poses stable enough to fuse into the twin.
- **CLAHE has to be conditional.** It rescues dark frames (0→3) but costs more than it gains
  on well-exposed ones (51→25), because it amplifies sensor and JPEG noise into false quads.
  It is applied only when the frame's 99th-percentile grey is under `DARK_P99`.

If a camera still finds nothing, check the scene before the code: the overview camera is a
D4xx **infrared** node, and the projector's dot pattern is superimposed on every surface,
which breaks the quad edges a tag detector needs. That one needs the emitter off (SDK, so
root) or the RGB node instead.

### Running the four-camera rig

Four viewpoints — one eye-in-hand camera per arm (`gripper_cam` is the **right** arm's,
`gripper_left_cam` the left's) plus fixed `overview_cam` and `handover_cam`. `camera_hub`
runs one worker per camera and a missing unit is skipped at boot, so any subset works with
no code change.

**Which unit is which viewpoint is hardcoded in [core/cameras.py](core/cameras.py)**, not
configured in `.env`. Each slot is pinned to an AVFoundation **uniqueID** — the only
root-free identity available on macOS — plus the unit's USB serial, so the driver can warn
when a camera has been moved to a different port. Re-identify the rig, with a frame from
each camera, after any change:

```bash
.venv/bin/python scripts/identify_cameras.py --snapshot
```

**An index is not an identity, which is why none of this lives in `.env`.** macOS renumbers
video devices on any replug — and, measured 2026-07-26 on this bench, between two
enumerations seconds apart with nothing touched: the built-in MacBook camera moved from
index 2 to index 0. A `CAM_<SLOT>=<index>` was therefore always one enumeration away from
aiming a viewpoint at a different camera, or at the operator, while the slot still reported
healthy. A leftover `CAM_<SLOT>` value is now ignored with a warning.

Two further traps, both measured here, both of which return a *plausible frame from the
wrong camera* rather than an error:

- **Device names are not unique.** The two D405 arm cameras report the byte-identical name
  `Intel(R) RealSense(TM) Depth Camera 405  Depth`, so binding by name hits whichever
  enumerates first.
- **`ffmpeg -f avfoundation -i <uniqueID>` does not match the uniqueID.** It silently opens
  the *default* device and exits 0 — a bogus uniqueID "succeeds" too. Only AVFoundation's
  own `AVCaptureDevice(uniqueID:)` honours it, which is what
  [scripts/avfsnap.swift](scripts/avfsnap.swift) calls and what the `avf` driver
  ([drivers/camera/avf.py](drivers/camera/avf.py)) streams from. An exact device *name*
  does bind correctly, and an unknown name is refused.

Because a wrong-camera bug looks like a working camera, `identify_cameras.py` fingerprints
every frame and fails if two units return the same view.

If you instead use the RGB-D `realsense` driver (root, for depth), pin it by **the serial the
SDK reports**. ioreg / `system_profiler` print a *different* serial for the same unit (one
D435 here is `125123020017` on USB but `138422075248` to the SDK), and `cfg.enable_device()`
matches the SDK value — so a USB-descriptor serial binds nothing and gives no error.

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

**Every backend start writes one frame per camera slot** to `temp/captures/<slot>/`
(`backend/app/services/startup_snapshot.py`). A slot is a name pointing at a device index,
and macOS reassigns those indices whenever the rig changes — so a slot can come up aimed at
a different camera, or at nothing, with no config change and no error. The boot frame turns
that from invisible drift into something you can look at. It runs on a daemon thread so a
camera that blocks cannot hang startup, is skipped under pytest (a test run must never open
bench hardware), and is disabled with `HZ_STARTUP_SNAPSHOT=0`.

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
