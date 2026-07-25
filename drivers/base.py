"""Common instrument-driver abstraction.

Every physical device in the lab (arms, liquid handler, cameras) is wrapped in a
driver that implements this lifecycle. The backend only ever talks to these
interfaces — never to a vendor SDK directly — so instruments are swappable and
mockable.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class InstrumentKind(str, Enum):
    ARM = "arm"
    LIQUID_HANDLER = "liquid_handler"
    CAMERA = "camera"


@dataclass
class DeviceInfo:
    id: str
    name: str
    kind: InstrumentKind
    model: str = ""
    vendor: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


class DriverError(RuntimeError):
    """Raised for any driver-level failure (connection, motion, hardware fault)."""


class InstrumentDriver(ABC):
    """Lifecycle contract shared by every instrument driver."""

    kind: InstrumentKind

    def __init__(self, device_id: str, config: dict[str, Any] | None = None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self._state = ConnectionState.DISCONNECTED

    # --- identity & health -------------------------------------------------
    @property
    @abstractmethod
    def info(self) -> DeviceInfo: ...

    @abstractmethod
    def status(self) -> dict[str, Any]:
        """Cheap, structured health/state snapshot for the UI and verifier."""

    @property
    def state(self) -> ConnectionState:
        return self._state

    # --- lifecycle ---------------------------------------------------------
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    # convenient context-manager use: `with driver: ...`
    def __enter__(self) -> "InstrumentDriver":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.disconnect()
