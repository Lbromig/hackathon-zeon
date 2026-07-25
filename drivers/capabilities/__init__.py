"""Capability interfaces — what a class of instrument can *do*.

Instrument packages (xarm/, opentrons/, camera/) implement these.
"""
from .arm import ArmDriver, ArmLimits, GripperInfo, GripperKind, Pose
from .camera import CameraDriver
from .liquid_handler import DeckLocation, LiquidHandlerDriver

__all__ = [
    "ArmDriver", "ArmLimits", "GripperInfo", "GripperKind", "Pose",
    "CameraDriver", "LiquidHandlerDriver", "DeckLocation",
]
