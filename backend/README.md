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

```bash
cd backend
pip install -r requirements.txt
pip install -e ../third_party/xArm-Python-SDK     # real arms (optional to boot)
PYTHONPATH=.. uvicorn app.main:app --reload       # .. so `drivers` is importable
```

Boots even with no hardware attached — drivers that fail to init are skipped,
and the SDK/opencv imports are optional.
