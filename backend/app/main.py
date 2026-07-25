"""FastAPI entrypoint.

    uvicorn app.main:app --reload   (run from backend/)
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import calibration, instruments, teach, workflow
from .core.config import settings
from .services.device_manager import device_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    device_manager.load_fleet()
    yield
    device_manager.disconnect_all()


app = FastAPI(title="hackathon-zeon backend", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(instruments.router)
app.include_router(teach.router)
app.include_router(calibration.router)
app.include_router(workflow.router)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}
