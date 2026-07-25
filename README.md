# hackathon-zeon — Track C: Cooperative Uncap → Aspirate

Two **xArm Lite 6** arms cooperatively uncap a sealed tube; one arm then holds the open
tube while an **Opentrons** (the original OT-One) aspirates from it. **Cameras + AI agents
verify every step** and retry when something fails.

## Architecture (three layers)

```
frontend/   Vue 3 + Vite UI — live fleet status, camera feeds, workflow runner
backend/    Python / FastAPI — API + websocket, device manager, orchestration,
            verification agents. Depends ONLY on drivers/ interfaces.
drivers/    Instrument abstraction — capability interfaces + one driver per instrument,
            built via a registry. No vendor SDK leaks above this layer.
third_party/xArm-Python-SDK/   vendored vendor SDK
docs/       ARCHITECTURE.md + WORKFLOW.md (mermaid diagrams)
```

Data flow: `Vue → FastAPI (REST/WS) → Orchestrator → capability → Driver → Device`,
with cameras feeding the verification agents that gate each step.

See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** and **[docs/WORKFLOW.md](docs/WORKFLOW.md)**
for diagrams, and **[PROJECT_PLAN.md](PROJECT_PLAN.md)** for the 24h plan and ownership.

## Quick start

```bash
# backend
cd backend && pip install -r requirements.txt
pip install -e ../third_party/xArm-Python-SDK
PYTHONPATH=.. uvicorn app.main:app --reload

# frontend (separate shell)
cd frontend && npm install && npm run dev
```

Boots without hardware — drivers that can't init are skipped; SDK/opencv imports are optional.
