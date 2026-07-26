"""Persistence for hand-taught travel paths.

Separate from ``teach_poses`` on purpose: a pose is one destination, a path is an ordered
route between two of them, and conflating the two makes both awkward to query. Same
storage discipline though — write to a temp file and ``os.replace``, so a crash mid-save
can never leave a half-written route that the arm would then try to follow.
"""
from __future__ import annotations

import json
import os
from typing import Any

from .config import settings
from .motion.path_teach import TaughtPath

_ENV_VAR = "HZ_TEACH_PATHS_FILE"


def path_file() -> str:
    """Where the library lives — beside the pose library unless overridden."""
    override = os.getenv(_ENV_VAR)
    if override:
        return override
    return os.path.join(os.path.dirname(settings.teach_poses_file), "teach_paths.json")


def load() -> dict[str, dict[str, Any]]:
    """{device_id: {path_name: entry}}. Empty when nothing has been taught."""
    p = path_file()
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[teach_paths] could not read {p}: {e}")
        return {}
    return data if isinstance(data, dict) else {}


def get(device_id: str, name: str) -> TaughtPath:
    entry = load().get(device_id, {}).get(name)
    if entry is None:
        known = sorted(load().get(device_id, {}))
        raise LookupError(
            f"no taught path {name!r} for {device_id!r} (known: {known or 'none'})"
        )
    return TaughtPath(
        name=name,
        device_id=device_id,
        waypoints=[[float(v) for v in wp] for wp in entry.get("waypoints", [])],
        recorded_at=entry.get("recorded_at", ""),
        note=entry.get("note", ""),
        raw_samples=int(entry.get("raw_samples", 0)),
    )


def save(path: TaughtPath) -> None:
    store = load()
    store.setdefault(path.device_id, {})[path.name] = {
        "name": path.name,
        "waypoints": path.waypoints,
        "recorded_at": path.recorded_at,
        "note": path.note,
        "raw_samples": path.raw_samples,
    }
    _write(store)


def delete(device_id: str, name: str) -> bool:
    store = load()
    if store.get(device_id, {}).pop(name, None) is None:
        return False
    _write(store)
    return True


def _write(store: dict[str, dict[str, Any]]) -> None:
    p = path_file()
    parent = os.path.dirname(p)
    if parent:                      # dirname("paths.json") is "" — makedirs would raise
        os.makedirs(parent, exist_ok=True)
    tmp = f"{p}.tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, p)              # atomic: never leave a half-written route
