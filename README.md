# hackathon-zeon — Track C: Cooperative Uncap → Aspirate

Two **xArm Lite 6** arms cooperatively uncap a sealed tube; one arm then holds the open
tube while an **Opentrons** (the original OT-One) aspirates from it. **Cameras + AI agents
verify every step** and retry when something fails.

## Architecture (three layers)

```
frontend/   Vue 3 + Vite + Tailwind UI — two tabs: Fleet (live status, camera feeds,
            workflow runner) and Teach (jog axes/joints, gripper, taught poses)
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

- **`Y` does not home.** `G28.2 Y` never acknowledges and the axis stalls
  audibly against a stop. Cause not yet established. The candidates are a
  blocked axis, an inverted homing direction, or a dead or disconnected `Y`
  endstop switch. Run the `endstops` command and press the switch by hand to
  tell those apart.
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

If a config does expose `homing_direction`, a wrong value can explain a home
that drives away from its switch. This particular firmware did not expose the
value, so the Y fault remains mechanical, wiring, or firmware-config diagnosis.

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
