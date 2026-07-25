"""Pydantic models for the API surface."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


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


# --- teach / jog (see api/teach.py) ------------------------------------------

class PoseModel(BaseModel):
    """Cartesian TCP pose — mm and degrees, matching drivers.Pose."""
    x: float
    y: float
    z: float
    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0


class GripperModel(BaseModel):
    kind: str = "none"            # parallel | lite6 | bio | none | unknown
    supports_width: bool = False
    min_width: float = 0.0
    max_width: float = 0.0
    units: str = "counts"         # widths are NOT metres — see drivers GripperInfo
    stroke_m: float = 0.0         # physical opening at max_width, for display
    width: float | None = None    # only read for width-capable grippers


class ArmLimitsModel(BaseModel):
    joints: list[list[float]] | None = None   # [[min, max], ...] per axis, deg
    max_jog_linear: float
    max_jog_angular: float
    max_speed_linear: float
    max_speed_angular: float
    max_move_to_jump: float


class ArmState(BaseModel):
    id: str
    state: str                    # driver ConnectionState
    connected: bool
    busy: bool = False            # a command is in flight on this arm
    pose: PoseModel | None = None
    joints: list[float] | None = None
    gripper: GripperModel = GripperModel()
    error_code: int | None = None
    warn_code: int | None = None
    detail: str = ""              # why pose/joints are missing, if they are


class ArmSummary(BaseModel):
    id: str
    name: str
    model: str = ""
    state: str
    connected: bool
    axis_count: int = 6
    gripper: GripperModel = GripperModel()
    limits: ArmLimitsModel


class ArmActionResult(BaseModel):
    ok: bool
    detail: str = ""
    state: ArmState | None = None


class JogRequest(BaseModel):
    space: Literal["cartesian", "joint"] = "cartesian"
    axis: str                     # x|y|z|roll|pitch|yaw, or j1..jN
    delta: float                  # mm for x/y/z, deg otherwise
    speed: float | None = None


class MoveToRequest(BaseModel):
    """Absolute move — exactly one of pose / joints."""
    pose: PoseModel | None = None
    joints: list[float] | None = None
    speed: float | None = None


class GripperRequest(BaseModel):
    action: Literal["open", "close", "set"]
    width: float | None = None    # required for "set", width-capable grippers only


class EnableRequest(BaseModel):
    on: bool = True


class StopRequest(BaseModel):
    emergency: bool = True


class TaughtPose(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    pose: PoseModel | None = None
    joints: list[float] | None = None
    gripper_width: float | None = None
    note: str = ""
    saved_at: str = ""
