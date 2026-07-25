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
