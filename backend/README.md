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
