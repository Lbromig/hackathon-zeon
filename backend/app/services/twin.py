"""Holds the live digital-twin WorldModel once calibration has produced it."""
from __future__ import annotations

from ..worldmodel import WorldModel

_world: WorldModel | None = None


def set_world(wm: WorldModel) -> None:
    global _world
    _world = wm


def get_world() -> WorldModel | None:
    return _world
