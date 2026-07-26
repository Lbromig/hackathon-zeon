"""Top-down "world map" of the twin — the visualization that shows the shared frame works.

`world_scene(wm)` flattens the twin into camera + entity positions in the world XY plane;
`scene_svg(scene)` renders it as a standalone SVG (no external deps, pure string) so it can
be served live (`/api/worldmodel/scene`), embedded in a page, or saved for a slide.

Why this proves W2: both fixed cameras are placed by solving the *same* 210/211 board, so on
this map they appear at their true relative positions and every tracked object sits in one
common frame. If calibration were wrong, the cameras (or the objects they see) would land in
implausible spots.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from ..worldmodel import EntityKind, WorldModel

# Entity kinds that would just clutter a top-down map (dense grids / coincident slots).
_SKIP = {EntityKind.TIP_SITE, EntityKind.WELL, EntityKind.TIP, EntityKind.DECK_SLOT,
         EntityKind.CAMERA, EntityKind.WORLD}

_COLOURS = {
    "tube": "#22c55e", "cap": "#f97316", "tube_rack": "#14b8a6",
    "ot_base": "#94a3b8", "deck": "#64748b", "deck_slot": "#475569",
    "arm_base": "#a855f7", "tcp": "#c084fc", "tool": "#e879f9",
    "pipette_channel": "#ec4899", "dropzone": "#b45309", "gantry": "#38bdf8",
    "tip_box": "#eab308", "surface": "#334155", "calib_ruler": "#64748b",
}


def world_scene(wm: WorldModel) -> dict[str, Any]:
    """{cameras:[{id,x,y,z,heading}], entities:[{id,kind,x,y,z}]} in world coords (metres)."""
    cams, ents = [], []
    with wm.lock:
        for e in wm.by_kind(EntityKind.CAMERA):
            T = wm.world_pose(e.id)
            fwd = T[:3, :3] @ np.array([0.0, 0.0, 1.0])   # optical axis in world
            cams.append({"id": e.id,
                         "x": float(T[0, 3]), "y": float(T[1, 3]), "z": float(T[2, 3]),
                         "heading": float(math.atan2(fwd[1], fwd[0]))})
        for e in list(wm.entities.values()):
            if e.kind in _SKIP:
                continue
            p = wm.world_pose(e.id)[:3, 3]
            kind = e.kind.value if hasattr(e.kind, "value") else str(e.kind)
            ents.append({"id": e.id, "kind": kind,
                         "x": float(p[0]), "y": float(p[1]), "z": float(p[2])})
    return {"cameras": cams, "entities": ents}


def scene_svg(scene: dict[str, Any], w: int = 780, h: int = 540, pad: int = 48) -> str:
    """Render a scene dict to a standalone top-down SVG string (+X right, +Y up)."""
    cams = scene.get("cameras", [])
    ents = scene.get("entities", [])
    pts = [(c["x"], c["y"]) for c in cams] + [(e["x"], e["y"]) for e in ents] + [(0.0, 0.0)]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    spanx = max(maxx - minx, 0.2)
    spany = max(maxy - miny, 0.2)
    s = min((w - 2 * pad) / spanx, (h - 2 * pad) / spany)   # metres -> px, uniform
    cx_off = (w - s * (minx + maxx)) / 2
    cy_off = (h + s * (miny + maxy)) / 2                    # +Y up -> screen up

    def X(x: float) -> float: return s * x + cx_off
    def Y(y: float) -> float: return cy_off - s * y

    out: list[str] = []
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'font-family="ui-monospace,monospace">')
    out.append(f'<rect width="{w}" height="{h}" fill="#0b1220" rx="10"/>')

    # grid every 0.1 m
    grid = 0.1
    gx = math.floor(minx / grid) * grid
    while gx <= maxx + grid:
        out.append(f'<line x1="{X(gx):.1f}" y1="0" x2="{X(gx):.1f}" y2="{h}" '
                   f'stroke="#1e293b" stroke-width="1"/>')
        gx += grid
    gy = math.floor(miny / grid) * grid
    while gy <= maxy + grid:
        out.append(f'<line x1="0" y1="{Y(gy):.1f}" x2="{w}" y2="{Y(gy):.1f}" '
                   f'stroke="#1e293b" stroke-width="1"/>')
        gy += grid

    # world axes at origin
    ox, oy = X(0), Y(0)
    out.append(f'<line x1="{ox}" y1="{oy}" x2="{ox + 0.1 * s}" y2="{oy}" stroke="#ef4444" stroke-width="2.5"/>')
    out.append(f'<line x1="{ox}" y1="{oy}" x2="{ox}" y2="{oy - 0.1 * s}" stroke="#22c55e" stroke-width="2.5"/>')
    out.append(f'<circle cx="{ox}" cy="{oy}" r="4" fill="#e2e8f0"/>')
    out.append(f'<text x="{ox + 6}" y="{oy + 16}" fill="#94a3b8" font-size="12">world 0,0 (board 210/211)</text>')

    # entities
    for e in ents:
        px, py = X(e["x"]), Y(e["y"])
        col = _COLOURS.get(e["kind"], "#64748b")
        out.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="6" fill="{col}" fill-opacity="0.85" stroke="#0b1220"/>')
        out.append(f'<text x="{px + 9:.1f}" y="{py + 4:.1f}" fill="#cbd5e1" font-size="11">{e["id"]}</text>')

    # cameras: a wedge pointing along heading
    for c in cams:
        px, py = X(c["x"]), Y(c["y"])
        a = -c["heading"]                                   # screen Y is flipped
        for da, r in ((0.0, 26), (0.42, 20), (-0.42, 20)):
            pass
        tip = (px + 26 * math.cos(a), py + 26 * math.sin(a))
        l = (px + 20 * math.cos(a + 0.42), py + 20 * math.sin(a + 0.42))
        rr = (px + 20 * math.cos(a - 0.42), py + 20 * math.sin(a - 0.42))
        out.append(f'<polygon points="{px:.1f},{py:.1f} {l[0]:.1f},{l[1]:.1f} {tip[0]:.1f},{tip[1]:.1f} {rr[0]:.1f},{rr[1]:.1f}" '
                   f'fill="#38bdf8" fill-opacity="0.25" stroke="#38bdf8" stroke-width="1.5"/>')
        out.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="7" fill="#38bdf8" stroke="#0b1220" stroke-width="1.5"/>')
        out.append(f'<text x="{px + 10:.1f}" y="{py - 8:.1f}" fill="#7dd3fc" font-size="12" font-weight="bold">{c["id"]}</text>')

    # scale bar (0.1 m)
    bx, by = pad, h - 18
    out.append(f'<line x1="{bx}" y1="{by}" x2="{bx + 0.1 * s}" y2="{by}" stroke="#e2e8f0" stroke-width="2"/>')
    out.append(f'<text x="{bx}" y="{by - 6}" fill="#94a3b8" font-size="11">100 mm</text>')
    out.append(f'<text x="{w - pad}" y="24" fill="#64748b" font-size="11" text-anchor="end">top-down · world XY</text>')
    out.append("</svg>")
    return "".join(out)
