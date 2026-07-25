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
from .capabilities.arm import ArmDriver, ArmLimits, GripperInfo, GripperKind, Pose
from .capabilities.camera import CameraDriver
from .capabilities.liquid_handler import DeckLocation, LiquidHandlerDriver
from .registry import available_types, build_driver, register

# Imported last (mock/ imports .registry, so this order avoids a cycle): registers
# the `mock_*` driver types so any fleet config — HZ_FLEET_FILE included — can ask
# for them without the caller remembering to import the module first.
from . import mock  # noqa: E402,F401  isort:skip

__all__ = [
    "ArmDriver", "ArmLimits", "GripperInfo", "GripperKind", "Pose",
    "CameraDriver", "LiquidHandlerDriver", "DeckLocation",
    "InstrumentDriver", "DeviceInfo", "InstrumentKind",
    "ConnectionState", "DriverError", "build_driver", "register", "available_types",
]
