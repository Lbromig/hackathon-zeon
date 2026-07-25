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

## Teach / jog API (`/api/arms`)

Backs the frontend's Teach tab — hand-driving an arm during bring-up.

| | |
|---|---|
| `GET /api/arms` | arms with gripper kind, axis count, soft limits |
| `GET /api/arms/{id}/state` | pose, joints, gripper, fault codes, busy |
| `POST /api/arms/{id}/jog` | `{space: cartesian\|joint, axis, delta, speed?}` |
| `POST /api/arms/{id}/move_to` | `{pose}` or `{joints}` (exactly one) |
| `POST /api/arms/{id}/gripper` | `{action: open\|close\|set, width?}` |
| `POST /api/arms/{id}/home` · `/enable` · `/clear_errors` · `/stop` | |
| `GET POST /api/arms/{id}/poses`, `POST .../{name}/goto`, `DELETE .../{name}` | taught points |

Handlers are sync on purpose: the SDK blocks, and FastAPI runs sync handlers in a
threadpool, so a `wait=True` move never stalls the event loop or `/ws/state`.

Safety is enforced here rather than in the client, because the client is not the
only possible caller:

* one in-flight command per arm (non-blocking lock → `409`); `/stop` bypasses it
  by design, since an e-stop that queues behind the move it interrupts is useless
* deltas, speeds and joint targets are validated against the arm's soft limits,
  failing closed on non-finite values
* motion is refused while a fault is latched, and while the fault state can't be read
* absolute cartesian moves beyond `max_move_to_jump` (default 250 mm) are refused

Taught poses persist to `data/teach_poses.json` (`HZ_TEACH_POSES_FILE`), namespaced
per device. Go-to replays the saved **joint** angles — the arm physically reached
them, so there's no IK branch to guess at.

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
