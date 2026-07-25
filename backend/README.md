# backend — Python / FastAPI

Orchestration, verification agents, and the API/websocket the UI talks to.
Depends on `drivers/` interfaces only.

```
app/
  main.py                 FastAPI app + lifespan (loads the fleet)
  core/config.py          settings + fleet definition (device list)
  services/device_manager.py   owns live driver instances
  verification/agents.py  the "did it work?" agents (cap/grasp/pose/aspiration)
  workflows/uncap_aspirate.py  the hero workflow (middle layer: capability -> drivers)
  api/instruments.py      REST: list / connect / status / camera frame
  api/workflow.py         WS: /ws/state (live), /ws/workflow (run + retries)
  schemas.py              pydantic models
```

## Run

Dependencies live in the root `pyproject.toml` and are managed with
[uv](https://docs.astral.sh/uv/) — `uv sync` from the repo root installs everything,
including the vendored xArm SDK.

```bash
uv sync                                            # from the repo root
cd backend
PYTHONPATH=.. uv run uvicorn app.main:app --reload  # .. so `drivers` is importable
```

Boots even with no hardware attached — drivers that fail to init are skipped,
and the SDK/opencv imports are optional.
