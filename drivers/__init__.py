"""Instrument drivers + abstraction layer.

Layer boundary: the backend depends ONLY on these interfaces and the registry,
never on a vendor SDK directly. Swap real hardware for mocks by registering a
different factory under the same type name.
"""
from .base import (
    ConnectionState,
    DeviceInfo,
    DriverError,
    InstrumentDriver,
    InstrumentKind,
)
from .capabilities.arm import ArmDriver, Pose
from .capabilities.camera import CameraDriver
from .capabilities.liquid_handler import DeckLocation, LiquidHandlerDriver
from .registry import available_types, build_driver, register

__all__ = [
    "ArmDriver", "Pose", "CameraDriver", "LiquidHandlerDriver", "DeckLocation",
    "InstrumentDriver", "DeviceInfo", "InstrumentKind",
    "ConnectionState", "DriverError", "build_driver", "register", "available_types",
]
