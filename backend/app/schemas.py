"""Pydantic models for the API surface."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class DeviceSummary(BaseModel):
    id: str
    name: str
    kind: str
    model: str = ""
    vendor: str = ""
    state: str
    status: dict[str, Any] = {}


class ActionResult(BaseModel):
    ok: bool
    detail: str = ""


class WorkflowStepEvent(BaseModel):
    step: str
    phase: str            # started | verifying | passed | retrying | failed
    attempt: int = 1
    detail: str = ""
    verification: dict[str, Any] | None = None
