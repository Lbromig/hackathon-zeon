"""CAD mesh registry for the twin's dynamic entities.

Maps a logical mesh key (stored on `Entity.mesh`) to an STL asset under
`assets/cad/tubes/` plus its measured dimensions. Used by:
  - FoundationPose (CAD-mode 6-DoF pose init + tracking),
  - Kaolin render-compare (analysis-by-synthesis verification),
  - the frontend 3D twin view.

IMPORTANT — units: the STL files are authored in MILLIMETRES. The twin and
FoundationPose work in METRES, so scale by SCALE_MM_TO_M when loading (or bake a
metre-scaled copy once). `dims_m` below are already in metres (measured from the
mesh bounding boxes: two equal dims = diameter, the odd dim = height/length).
All four meshes are watertight and winding-consistent.
"""
from __future__ import annotations

from pathlib import Path

# repo_root/assets/cad/tubes  (this file: core/worldmodel/meshes.py)
CAD_DIR = Path(__file__).resolve().parents[2] / "assets" / "cad" / "tubes"
SCALE_MM_TO_M = 0.001

MESHES: dict[str, dict] = {
    "tube_50ml_base": {"file": "tube_50ml_base.stl", "dims_m": {"diameter": 0.0280, "height": 0.1124}},
    "tube_50ml_cap":  {"file": "tube_50ml_cap.stl",  "dims_m": {"diameter": 0.0340, "height": 0.0160}},
    "tube_15ml_base": {"file": "tube_15ml_base.stl", "dims_m": {"diameter": 0.0153, "height": 0.1193}},
    "tube_15ml_cap":  {"file": "tube_15ml_cap.stl",  "dims_m": {"diameter": 0.0220, "height": 0.0100}},
}


def mesh_path(key: str) -> Path:
    """Absolute path to the STL for a mesh key."""
    return CAD_DIR / MESHES[key]["file"]


def dims_m(key: str) -> dict[str, float]:
    """Copy of the measured dimensions (metres) for a mesh key."""
    return dict(MESHES[key]["dims_m"])
