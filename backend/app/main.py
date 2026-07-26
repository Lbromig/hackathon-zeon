"""FastAPI entrypoint.

    uv run uvicorn backend.app.main:app --reload   (run from the repo root)
"""
from __future__ import annotations

import math
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from core.config import settings
from core.obs import configure as configure_logging
from core.obs import get_logger

from .api import cameras, engine, instruments, logs, runs, teach
# Importing the handler package **is** the handler registration (`handlers/__init__.py`):
# `handler_for(kind)` returns None for a kind nobody claimed, and pre-flight turns that into
# "this step cannot run" (R-ENG-17). Without this import every kind is `no_handler` and every
# plan's readiness is `failed` — so the import is load-bearing, not cosmetic, and the `noqa`
# is there because nothing in this module references the name.
from .engine import handlers as _handlers  # noqa: F401
from .services import startup_snapshot
from .services.camera_hub import camera_hub
from .services.device_manager import device_manager
from .services.run_manager import run_manager

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # First, before anything that might want to report a problem. `core.config` already
    # emitted its records through module loggers at import time; those predate the handlers
    # and are lost, which is the price of settings being importable from anywhere — so the
    # resolved configuration is restated here, where it lands in the file.
    configure_logging(path=settings.log_file, level=settings.log_level,
                      console=settings.log_console, max_bytes=settings.log_max_bytes,
                      backup_count=settings.log_backup_count)
    log.info("logging to %s", settings.log_file, extra={"event": "run_start"})
    # D29 makes simulation the default, so which mode this is must be stated loudly and
    # per device — a simulated run that reads as real is the failure that default trades for.
    log.info("simulation: HZ_SIM=%s -> %s", settings.sim.spec,
             ", ".join(sorted(settings.sim.device_ids)) or "nothing (real hardware)",
             extra={"event": "device_state",
                    "simulated": sorted(settings.sim.device_ids),
                    "servo_cameras": list(settings.servo_cameras)})
    device_manager.load_fleet()
    # One frame per camera slot, written to temp/captures/<slot>/. Backgrounded: a UVC open
    # can block uninterruptibly on macOS, and the API must come up regardless.
    startup_snapshot.run()
    # Initialization is a *plan* on the engine's own runner (D5/R-INIT), so it gets indices,
    # per-action logs, readiness and pause for free, and the Workflow tab can show it. Also on
    # a daemon thread and never awaited: an arm controller can sit in a TCP connect and a UVC
    # open can block uninterruptibly, and the API must come up regardless of any of it
    # (R-START-7). Every failure inside is recorded per device, not raised.
    run_manager.boot_init()
    yield
    # Stop the run before the drivers it is commanding are torn down — `disconnect_all` is also
    # what brakes an arm, and doing that under a live worker leaves a move half executed.
    run_manager.shutdown()
    # Stop the frame workers before the drivers they hold go away, or a worker
    # keeps grabbing from a released VideoCapture during shutdown.
    camera_hub.stop_all()
    device_manager.disconnect_all()


app = FastAPI(title="hackathon-zeon backend", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _json_safe(value: Any) -> Any:
    """Replace non-finite floats so an error body can actually be serialized."""
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """422s for rejected input, including the input that pydantic rejected.

    The stock handler echoes the offending value back verbatim, so rejecting a
    NaN (schemas.FiniteModel) made `json.dumps` raise *inside* the error handler
    — turning a clean 422 into a 500 with no reason attached.
    """
    return JSONResponse(status_code=422, content={"detail": _json_safe(jsonable_encoder(exc.errors()))})


app.include_router(instruments.router)
app.include_router(instruments.state_router)
app.include_router(teach.router)
app.include_router(cameras.router)
app.include_router(logs.router)
app.include_router(engine.router)
# Prefix-less, and it must stay that way: an APIRouter prefix applies to websocket routes too,
# so including this on `engine.router` would rename the path to `/api/engine/ws/engine` and
# every client would see nothing but a closed socket.
app.include_router(engine.ws_router)
app.include_router(runs.router)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}
