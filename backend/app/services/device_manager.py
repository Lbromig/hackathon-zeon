"""Owns the live driver instances and brokers access to them."""
from __future__ import annotations

from typing import Any

from drivers import DriverError, InstrumentDriver, build_driver

from ..core.config import settings


class DeviceManager:
    def __init__(self) -> None:
        self._drivers: dict[str, InstrumentDriver] = {}

    def load_fleet(self) -> None:
        for cfg in settings.fleet:
            try:
                self._drivers[cfg["id"]] = build_driver(cfg)
            except Exception as e:  # keep booting even if one driver is misconfigured
                print(f"[device_manager] skipped {cfg.get('id')}: {e}")

    def get(self, device_id: str) -> InstrumentDriver:
        if device_id not in self._drivers:
            raise KeyError(device_id)
        return self._drivers[device_id]

    def all(self) -> list[InstrumentDriver]:
        return list(self._drivers.values())

    def connect(self, device_id: str) -> None:
        self.get(device_id).connect()

    def connect_all(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for d in self._drivers.values():
            try:
                d.connect()
                result[d.device_id] = "connected"
            except DriverError as e:
                result[d.device_id] = f"error: {e}"
        return result

    def disconnect_all(self) -> None:
        for d in self._drivers.values():
            try:
                d.disconnect()
            except Exception:
                pass

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
