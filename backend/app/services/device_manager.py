"""Owns the live driver instances and brokers access to them."""
from __future__ import annotations

from typing import Any

from drivers import DriverError, InstrumentDriver, build_driver

from core.config import settings
from core.obs import get_logger

log = get_logger(__name__)


class DeviceManager:
    def __init__(self) -> None:
        self._drivers: dict[str, InstrumentDriver] = {}

    def load_fleet(self) -> None:
        """(Re)build the driver set from config.

        Reloading disconnects the outgoing drivers first: replacing the dict alone
        leaks controller sockets and orphans an *energized* arm with nothing holding
        a reference to it. Reachable in normal use under `uvicorn --reload`.
        """
        if self._drivers:
            self.disconnect_all()
            self._drivers.clear()
        for cfg in settings.fleet:
            try:
                self._drivers[cfg["id"]] = build_driver(cfg)
            except Exception as e:  # keep booting even if one driver is misconfigured
                log.warning("skipped %s: %s", cfg.get("id"), e)

    def get(self, device_id: str) -> InstrumentDriver:
        if device_id not in self._drivers:
            raise KeyError(device_id)
        return self._drivers[device_id]

    def all(self) -> list[InstrumentDriver]:
        return list(self._drivers.values())

    def connect(self, device_id: str) -> None:
        self.get(device_id).connect()

    def connect_all(self) -> dict[str, str]:
        """Connect every driver, recording per-device outcomes.

        Catches Exception, not just DriverError: a driver raising anything else
        would otherwise abort the loop, leaving the already-connected arms live with
        no record of them in the result.
        """
        result: dict[str, str] = {}
        for d in self._drivers.values():
            try:
                d.connect()
                result[d.device_id] = "connected"
            except Exception as e:
                result[d.device_id] = f"error: {e}"
        return result

    def disconnect(self, device_id: str) -> None:
        self.get(device_id).disconnect()

    def disconnect_all(self) -> None:
        """Disconnect everything, best effort — one failure must not skip the rest.

        For arms, driver.disconnect() is also what brakes them, so a raise here
        would leave later arms energized.
        """
        for d in self._drivers.values():
            try:
                d.disconnect()
            except Exception as e:
                log.warning("%s failed to disconnect cleanly: %s", d.device_id, e)

    def snapshot(self) -> list[dict[str, Any]]:
        out = []
        for d in self._drivers.values():
            info = d.info
            out.append({
                "id": info.id, "name": info.name, "kind": info.kind,
                "model": info.model, "vendor": info.vendor,
                "state": d.state, "status": _safe_status(d),
            })
        return out


def _safe_status(d: InstrumentDriver) -> dict[str, Any]:
    try:
        return d.status()
    except Exception as e:
        return {"error": str(e)}


device_manager = DeviceManager()
