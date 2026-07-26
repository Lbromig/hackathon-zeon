"""Shared simulated world state (D3, R-SIM-5).

Simulation in this repo works by **substituting the driver behind a device** (D2/R-SIM-2),
never by an ``if simulate:`` branch above the driver layer. That leaves one thing the driver
substitution cannot express on its own: the mock liquid handler and the synthetic camera have
to agree about the *same physical situation*, or the servo loop converges in one of them while
diverging in the other.

`core.sim.world` is that agreement — one shared tip↔tube offset, decremented by the mock's
relative moves and rendered by the synthetic camera. **Its module docstring carries the sign
convention both sides code against**; read it before changing either.

Note the deliberate omission: the accessor is re-exported here as ``shared_world``, not as
``world``. Binding the name ``world`` on the package would shadow the submodule, so
``from core.sim import world`` would hand back the function rather than the module — a confusing
five minutes at best, and an ``AttributeError`` inside a driver at import time at worst.
"""
from __future__ import annotations

from .world import (AXES, DEFAULT_GAIN, DEFAULT_NOISE_MM, DEFAULT_OFFSET_MM, SimWorld,
                    reset_world)
from .world import world as shared_world

__all__ = ["AXES", "DEFAULT_GAIN", "DEFAULT_NOISE_MM", "DEFAULT_OFFSET_MM", "SimWorld",
           "shared_world", "reset_world"]
