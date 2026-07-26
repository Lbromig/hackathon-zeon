"""Adapter from :class:`DeviceManager` to the engine's :class:`DeviceAccess` protocol.

The handlers need three things the device manager does not offer: "give me this device and
assert it is an arm", the same for a liquid handler, and "is this device simulated?". Rather
than widen the device manager — which the teach API and the instruments API also use, and
which knows nothing about the engine — the engine-facing shape lives here.

The type assertions exist so a handler never has to defend itself against being pointed at
the wrong kind of device: a plan naming a camera where an arm belongs is a *plan* bug, and it
should surface as one clear error at the top of the handler rather than as an
``AttributeError`` from somewhere inside a motion call.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from drivers import InstrumentKind

if TYPE_CHECKING:  # pragma: no cover - typing only
    from drivers.capabilities.arm import ArmDriver
    from drivers.capabilities.liquid_handler import LiquidHandlerDriver

    from .device_manager import DeviceManager


class WrongDeviceKind(TypeError):
    """A plan pointed an action at a device of the wrong kind."""


class DeviceGateway:
    """Implements ``engine.context.DeviceAccess`` over a ``DeviceManager``."""

    def __init__(self, manager: "DeviceManager", *, simulated: dict[str, bool] | None = None
                 ) -> None:
        self._dm = manager
        # Which devices are simulated, resolved once from sim config by whoever builds the
        # run. Passed in rather than sniffed off the driver: a real driver has no marker to
        # sniff, and `info.vendor == "mock"` is a string comparison standing in for a
        # configuration fact (D25).
        self._simulated = dict(simulated or {})

    # --- DeviceAccess -------------------------------------------------------
    def get(self, device_id: str) -> Any:
        """The driver for ``device_id``. Raises ``KeyError`` if it is not in the fleet."""
        return self._dm.get(device_id)

    def require_arm(self, device_id: str) -> "ArmDriver":
        return self._require(device_id, InstrumentKind.ARM, "an arm")

    def require_liquid_handler(self, device_id: str) -> "LiquidHandlerDriver":
        return self._require(device_id, InstrumentKind.LIQUID_HANDLER, "a liquid handler")

    def is_simulated(self, device_id: str) -> bool:
        return bool(self._simulated.get(device_id, False))

    # --- helpers ------------------------------------------------------------
    def _require(self, device_id: str, kind: InstrumentKind, label: str) -> Any:
        driver = self._dm.get(device_id)          # KeyError if absent — let it through
        actual = getattr(getattr(driver, "info", None), "kind", None)
        if actual != kind:
            raise WrongDeviceKind(
                f"{device_id!r} is {actual.value if actual else 'of unknown kind'}, "
                f"not {label} — check the plan"
            )
        return driver

    def all_simulated(self) -> dict[str, bool]:
        """Per-device simulation state, for the run-started event and the UI banner.

        Simulation being the default (D29) is only safe if it is impossible to mistake a
        simulated run for a real one, so this is reported per run rather than inferred.
        """
        return {d.device_id: self.is_simulated(d.device_id) for d in self._dm.all()}


__all__ = ["DeviceGateway", "WrongDeviceKind"]
