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

## Quick start

Everything runs directly on the host. This project uses
**[uv](https://docs.astral.sh/uv/)** for all Python work.

```bash
cp .env.example .env        # optional — set your arm IPs / camera sources
uv sync                     # create .venv + install deps (incl. vendored xArm SDK)

# initialize the arm
uv run python scripts/init_xarm.py --ip 192.168.3.13

# backend (from the repo root)
uv run uvicorn backend.app.main:app --reload

# frontend (separate shell)
cd frontend && npm install && npm run dev
```

The bench runs on the host and not in a container on purpose: the cameras are USB
(UVC/RealSense) and the Opentrons is on a serial port, and macOS cannot pass either
into a Linux VM. A containerised backend reports every camera as `disconnected`.

Both services bind to loopback by default. To reach the UI from another machine on
the bench WiFi, start Vite with `--host` — but note that its proxy exposes the whole
unauthenticated API along with it, arms included. See
**[docs/CAMERA_ACCESS.md](docs/CAMERA_ACCESS.md)**.

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
---

# Hardware bring-up (standalone POC tools)

Guarded, mostly read-only utilities used to bring each machine up **before** it is wired
into the stack above. They are deliberately independent of `core/`, `backend/` and `drivers/` —
stdlib or vendor-SDK only, so they keep working when the app does not.

- **[hardware/xarm6/README.md](hardware/xarm6/README.md)** — safe first connection to a
  UFACTORY xArm 6. Intentionally read-only: verifies network access and reports
  controller status without enabling motors or sending movement commands.
- **Opentrons OT-One** — `ot_driver.py`, `ot_move_poc.py`, `ot_one_tip_calibrator.py`
  at the repo root. Detailed below.

## Opentrons control driver POC

A single-file control driver for Opentrons hardware, plus a zero-dependency
"prove it moves" script. Built for the hackathon bring-up of an old-school OT.

### What is here

- `ot_driver.py` - the driver. One uniform API over three transports:
  - `serial` - GCode over USB serial, for an OT-One / Smoothieboard. This is the
    path that works when a robot has no onboard robot-server, or its
    robot-server is dead.
  - `http` - the robot-server HTTP API on port 31950, for an OT-2 or a Flex.
  - `sim` - no hardware. Tracks position in memory so the control logic, the
    soft limits and a demo script are all reviewable with nothing plugged in.
- `ot_move_poc.py` - stdlib only, no imports beyond the standard library. Finds a
  robot-server on the local subnets and commands a home plus a jog over HTTP.
- `ot_one_tip_calibrator.py` - guarded interactive console for the original
  OT-One. It commands the shared Z lift only, in steps no larger than 5 mm, and
  preserves the pickup press cycle from the archived OT-One API.

### Quick start

    python3 ot_driver.py detect                     # what is attached
    python3 ot_driver.py demo --transport sim --go   # full control path, no hardware
    python3 ot_driver.py endstops --transport serial --port /dev/cu.usbmodem11201
    python3 ot_driver.py estop --transport serial --port /dev/cu.usbmodem11201
    python3 ot_one_tip_calibrator.py --port /dev/cu.usbmodem11201

The general driver moves nothing without `--go`; `estop` and `endstops` never
command motion. The calibrator connects read-only and waits at a prompt. Its
`home`, `down`, `up`, `lift`, and `pickup` commands are the explicit motion gate.

### Hardware state, honestly

Verified on the bench unit on 2026-07-25:

- The board enumerates as `Smoothieboard` (vendor `Uberclock`) on
  `/dev/cu.usbmodem11201`, firmware `v1.0.3`.
- `M114.2` reports six firmware axes, `X Y Z A B C`. The archived OT-One API
  uses `X`/`Y` for the gantry, one shared `Z` lift, and `A`/`B` for the two
  pipette plungers. It does not use `C`.
- `M119` reports `min_x min_y min_z min_a min_b`. There is no `min_c`.
- Homing `Z`, then `A`, then `X` each acknowledged cleanly during the initial
  bring-up. The focused calibrator now homes only the shared `Z` lift.
- After a full USB and main-power cycle, the board reconnected and `Z` homed to
  zero. Guarded downward moves of 5 mm, 5 mm, and 3 mm arrived cleanly at
  `Z=13.0`. On this board, increasing Smoothie `Z` moves the head down.

Not working, and not to be claimed as working:

- **No endstop on ANY axis registers with the board.** This is established, not a
  candidate: `M119` was polled for 18 s while the `Z` limit switch was pressed by
  hand and no bit ever changed, and the firmware config has no axis limit entries
  (`config-get sd gamma_min` returns `not in config`). So `G28.2` never terminates
  on a limit; it drives a fixed search distance and zeroes the counter. Homing `Z`
  twice in a row took 5.44 s then 6.56 s, where a real second home would finish
  in a fraction of a second because it starts already on the switch.
- **`Y` does not home**, and this is why. `G28.2 Y` drove looking for a switch
  that never reports and ground against a hard stop. Y is not specially broken;
  `Z` does the same thing, just without an obstruction in its path. See
  `HARDWARE-FINDINGS.md`.
- **`Z=0` after homing is therefore not a physical datum**, and absolute
  positioning cannot be trusted on this machine. Use relative `G91` jogging
  (`scripts/jog_z.py`), which needs no datum.
- Because of that, `Y` is excluded from `DEFAULT_HOME_AXES`, and the driver
  refuses to command any axis it has not homed, since an absolute move on an
  unhomed axis can drive it into a hard stop.
- The general demo still does not command `Z`. Tip calibration uses the separate
  Z-only console, whose per-move limit is 5 mm and whose guarded envelope is
  0..95 mm.
- No full-envelope move and no liquid handling has been demonstrated.

### Learn the envelope, do not guess it

`SOFT_LIMITS` in `ot_driver.py` are documented platform defaults, not
measurements from this machine, and commanding a coordinate past the real
envelope is what overreached during bring-up. The diagnostic command is:

    python3 ot_driver.py config --transport serial --port /dev/cu.usbmodem11201

That attempts to read Smoothieware's `config-get` values and commands no motion.
On this firmware (`v1.0.3`), the queried `gamma_*` travel and homing keys report
`not in config`; the board does not expose a usable envelope through this path.
The focused calibrator therefore uses the archived OT-One 100 mm Z dimension
with a 5 mm reserve and refuses any target outside 0..95 mm.

If a config does expose `homing_direction`, a wrong value can explain a home that
drives away from its switch. This firmware exposes no such value, but the Y fault
no longer needs that diagnosis: the endstop poll above showed no switch is read on
any axis, which accounts for the stall on its own.

On firmware that exposes a complete envelope, the general driver can use it:

    python3 ot_driver.py demo --transport serial --port /dev/cu.usbmodem11201 \
        --learn-limits --go

On this v1.0.3 board the envelope cannot be read, so `--learn-limits` refuses to
move rather than silently falling back to guesses. Use the Z-only calibrator for
the current tip-pickup work.

### Safety notes

The emergency stop sends Ctrl-X first, then `M112`, then `M18`. Ctrl-X goes
first because it is handled at the serial layer and so interrupts a move that is
already executing; `M112` alone can sit in the queue behind that very move.

Closing the serial port does **not** stop an in-flight move. An earlier version
of this driver relied on that and let a stalling axis grind. Motion commands now
carry a bounded timeout and fire the emergency stop before raising.

If a noise persists, cut power at the switch. Do not rely on software.
