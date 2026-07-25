"""Driver registry — build driver instances from config, no vendor SDK imports
leaking into the backend.

    from drivers import build_driver
    arm = build_driver({"type": "xarm", "id": "left", "ip": "192.168.1.10"})
"""
from __future__ import annotations

from typing import Any, Callable

from .base import InstrumentDriver
from .camera import OpenCVCameraDriver
from .opentrons import OpentronsDriver
from .xarm import XArmDriver

_REGISTRY: dict[str, Callable[[str, dict[str, Any]], InstrumentDriver]] = {
    "xarm": XArmDriver,
    "opentrons": OpentronsDriver,
    "camera": OpenCVCameraDriver,
}


def register(type_name: str, factory: Callable[[str, dict[str, Any]], InstrumentDriver]) -> None:
    _REGISTRY[type_name] = factory


def available_types() -> list[str]:
    return sorted(_REGISTRY)


def build_driver(cfg: dict[str, Any]) -> InstrumentDriver:
    """cfg = {"type": "xarm", "id": "left", ...driver-specific config...}."""
    cfg = dict(cfg)
    type_name = cfg.pop("type")
    device_id = cfg.pop("id")
    if type_name not in _REGISTRY:
        raise KeyError(f"unknown driver type {type_name!r}; have {available_types()}")
    return _REGISTRY[type_name](device_id, cfg)
