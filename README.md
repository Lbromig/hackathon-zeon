# hackathon-zeon — Track C: Cooperative Uncap → Aspirate

Two **xArm Lite 6** arms cooperatively uncap a sealed tube; one arm then holds the open
tube while an **Opentrons** (the original OT-One) aspirates from it. **Cameras + AI agents
verify every step** and retry when something fails.

## Architecture (three layers)

```
frontend/   Vue 3 + Vite UI — live fleet status, camera feeds, workflow runner
backend/    Python / FastAPI — API + websocket, device manager, orchestration,
            verification agents. Depends ONLY on drivers/ interfaces.
  app/worldmodel/    digital twin — scene graph of entities + poses (world frame)
  app/calibration/   init + vision calibration (ArUco + 3D-printed ruler + scan adapter)
  app/motion/        safe pick/place planner + anti-flip (upright) guard
drivers/    Instrument abstraction — capability interfaces + one driver per instrument,
            built via a registry. No vendor SDK leaks above this layer.
third_party/xArm-Python-SDK/   vendored vendor SDK
docs/       ARCHITECTURE · WORKFLOW · DIGITAL_TWIN · CAPABILITY_pick_place · ZEON_INTEGRATION
```

Data flow: `Vue → FastAPI (REST/WS) → Orchestrator → capability → Driver → Device`,
with cameras feeding the verification agents that gate each step.

See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** and **[docs/WORKFLOW.md](docs/WORKFLOW.md)**
for diagrams, and **[PROJECT_PLAN.md](PROJECT_PLAN.md)** for the 24h plan and ownership.

## Quick start (uv)

This project uses **[uv](https://docs.astral.sh/uv/)** for all Python work.

```bash
uv sync                     # create .venv + install deps (incl. vendored xArm SDK)

# initialize the arm
uv run python scripts/init_xarm.py --ip 192.168.3.13

# backend
cd backend && PYTHONPATH=.. uv run uvicorn app.main:app --reload

# frontend (separate shell)
cd frontend && npm install && npm run dev
```

Boots without hardware — drivers that can't init are skipped; SDK/opencv imports are optional.
