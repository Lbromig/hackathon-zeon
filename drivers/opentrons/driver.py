"""Opentrons driver (the original OT-One unit).

The OT-One predates the modern Opentrons HTTP API, so this driver is written
against a pluggable transport. Fill in ``_send`` for the actual control path
available on the unit (serial G-code bridge, legacy Python API, or a small
shim service). Everything above ``_send`` is transport-agnostic.
"""
from __future__ import annotations

from typing import Any

from ..base import ConnectionState, DeviceInfo, DriverError, InstrumentKind
from ..capabilities.liquid_handler import DeckLocation, LiquidHandlerDriver


class OpentronsDriver(LiquidHandlerDriver):
    """Config: {"transport": "serial", "port": "/dev/ttyACM0", "default_tip": {...}}."""

    def __init__(self, device_id: str, config: dict[str, Any] | None = None) -> None:
        super().__init__(device_id, config)
        self._conn: Any = None

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            id=self.device_id,
            name=self.config.get("name", "Opentrons (OT-One)"),
            kind=InstrumentKind.LIQUID_HANDLER,
            model="OT-One",
            vendor="Opentrons",
        )

    def connect(self) -> None:
        self._state = ConnectionState.CONNECTING
        try:
            # TODO: open the real transport (serial / legacy API / shim).
            self._conn = object()
            self._state = ConnectionState.CONNECTED
        except Exception as e:  # pragma: no cover
            self._state = ConnectionState.ERROR
            raise DriverError(f"Opentrons connect failed: {e}") from e

    def disconnect(self) -> None:
        self._conn = None
        self._state = ConnectionState.DISCONNECTED

    def status(self) -> dict[str, Any]:
        return {"state": self._state, "connected": self._conn is not None}

    # --- transport ---------------------------------------------------------
    def _send(self, command: str, **params: Any) -> Any:
        if self._conn is None:
            raise DriverError("Opentrons not connected")
        # TODO: encode + write to the transport, return parsed response.
        return None

    # --- liquid handling ---------------------------------------------------
    def home(self) -> None:
        self._send("home")

    def move_to(self, location: DeckLocation) -> None:
        self._send("move_to", **location.__dict__)

    def pick_up_tip(self, location: DeckLocation) -> None:
        self._send("pick_up_tip", **location.__dict__)

    def drop_tip(self, location: DeckLocation | None = None) -> None:
        self._send("drop_tip", **(location.__dict__ if location else {}))

    def aspirate(self, volume_ul: float, location: DeckLocation) -> None:
        self._send("aspirate", volume=volume_ul, **location.__dict__)

    def dispense(self, volume_ul: float, location: DeckLocation) -> None:
        self._send("dispense", volume=volume_ul, **location.__dict__)
