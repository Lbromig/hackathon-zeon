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

from .api import cameras, instruments, teach
from .services import startup_snapshot
from .services.camera_hub import camera_hub
from .services.device_manager import device_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    device_manager.load_fleet()
    # One frame per camera slot, written to temp/captures/<slot>/. Backgrounded: a UVC open
    # can block uninterruptibly on macOS, and the API must come up regardless.
    startup_snapshot.run()
    yield
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


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}
