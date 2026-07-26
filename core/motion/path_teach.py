"""Teaching a travel path by hand-guiding, and turning the recording into waypoints.

The operator switches the arm to free-drive and physically walks it along the route it
should take — say tube-approach round to the OT handover. The backend samples joint
angles while they do it, and this module turns that dense, shaky recording into a small
ordered set of waypoints the arm can replay.

Simplification is not an optimisation, it is the point. Sampling at 10 Hz for half a
minute yields ~300 poses; replaying those as 300 sequential joint moves would take
minutes and stutter at every one. Two passes fix that:

1. **Deadband** — drop samples that barely moved. A hand resting on the arm produces a
   cloud of near-identical readings, and while the operator pauses to think the recorder
   is still running.
2. **Ramer-Douglas-Peucker** — drop samples that lie close to the straight line between
   their neighbours. A long sweep across the bench is geometrically a few segments, not
   two hundred, and RDP keeps exactly the points where the path actually bends.

Everything here is pure: joint vectors in, joint vectors out. No driver, no I/O, so the
shape of a path can be tested without an arm.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Tuned for a travel path, in degrees of joint motion.
DEFAULT_DEADBAND_DEG = 2.0     # ignore samples within this of the last kept one
DEFAULT_RDP_TOL_DEG = 3.0      # max deviation from the straight line we will discard
MIN_WAYPOINTS = 2              # a path is at least a start and an end
MAX_WAYPOINTS = 200            # a replay longer than this is a recording mistake


class PathError(ValueError):
    """A recorded path cannot be used."""


@dataclass
class TaughtPath:
    name: str
    device_id: str
    waypoints: list[list[float]] = field(default_factory=list)   # joint vectors, deg
    recorded_at: str = ""
    note: str = ""
    raw_samples: int = 0          # how many were captured before simplification

    @property
    def length_deg(self) -> float:
        """Total joint-space travel — a rough proxy for how long a replay takes."""
        return sum(_dist(a, b) for a, b in zip(self.waypoints, self.waypoints[1:]))


def _dist(a: list[float], b: list[float]) -> float:
    """Euclidean distance in joint space (degrees)."""
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def _point_line_distance(p: list[float], start: list[float], end: list[float]) -> float:
    """Perpendicular distance from `p` to the segment start->end, in joint space."""
    seg = [e - s for s, e in zip(start, end)]
    seg_len2 = sum(v * v for v in seg)
    if seg_len2 == 0.0:                       # degenerate segment: fall back to a point
        return _dist(p, start)
    # Projection parameter, clamped so we measure to the segment and not its extension.
    t = sum((pi - si) * vi for pi, si, vi in zip(p, start, seg)) / seg_len2
    t = max(0.0, min(1.0, t))
    proj = [s + t * v for s, v in zip(start, seg)]
    return _dist(p, proj)


def _rdp(points: list[list[float]], tol: float) -> list[list[float]]:
    """Ramer-Douglas-Peucker over n-dimensional joint vectors.

    Iterative rather than recursive: a 10 Hz recording can be long enough to blow the
    default recursion limit, and a stack overflow while saving a taught path is a
    miserable way to lose one.
    """
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        worst, worst_i = 0.0, first
        for i in range(first + 1, last):
            d = _point_line_distance(points[i], points[first], points[last])
            if d > worst:
                worst, worst_i = d, i
        if worst > tol:
            keep[worst_i] = True
            stack.append((first, worst_i))
            stack.append((worst_i, last))
    return [p for p, k in zip(points, keep) if k]


def simplify(samples: list[list[float]], *,
             deadband_deg: float = DEFAULT_DEADBAND_DEG,
             rdp_tol_deg: float = DEFAULT_RDP_TOL_DEG) -> list[list[float]]:
    """Dense hand-guided samples -> a small ordered set of waypoints.

    Always keeps the first and last sample: those are where the operator started and
    stopped, and a travel path that does not actually reach its endpoint is useless.
    """
    if not samples:
        return []
    pts = [list(map(float, s)) for s in samples]

    # Pass 1: deadband. Keeps the last sample even if it is inside the band, so the
    # endpoint survives an operator who slowed to a stop before releasing the button.
    kept = [pts[0]]
    for p in pts[1:]:
        if _dist(p, kept[-1]) >= deadband_deg:
            kept.append(p)
    if _dist(kept[-1], pts[-1]) > 1e-9:
        kept.append(pts[-1])

    # Pass 2: RDP over what survived.
    return _rdp(kept, rdp_tol_deg)


def max_blend_radius(waypoints: list[list[float]], fraction: float = 0.4) -> float:
    """Largest blend radius that is safe for this path, in degrees.

    The controller rejects a blend radius larger than the track length, so the whole
    replay is bounded by its *shortest* segment — one tight corner in an otherwise open
    route caps the blending everywhere. Taking a fraction rather than the whole segment
    leaves room for the arc to start and finish inside it.

    Returns 0 when there is nothing to blend, which callers should treat as "no radius".
    """
    if len(waypoints) < 3:
        return 0.0      # a two-point path has no corner to round
    shortest = min(_dist(a, b) for a, b in zip(waypoints, waypoints[1:]))
    return max(0.0, shortest * fraction)


def validate(waypoints: list[list[float]], axis_count: int) -> None:
    """Reject a path that cannot be replayed, with a reason the operator can act on."""
    if len(waypoints) < MIN_WAYPOINTS:
        raise PathError(
            f"path has {len(waypoints)} waypoint(s); need at least {MIN_WAYPOINTS}. "
            "Hold the record button while you move the arm, not just at the ends."
        )
    if len(waypoints) > MAX_WAYPOINTS:
        raise PathError(
            f"path has {len(waypoints)} waypoints, over the {MAX_WAYPOINTS} cap — "
            "raise the simplification tolerance or record a shorter route."
        )
    for i, wp in enumerate(waypoints):
        if len(wp) != axis_count:
            raise PathError(
                f"waypoint {i} has {len(wp)} joint values, expected {axis_count}"
            )
        if not all(isinstance(v, (int, float)) and v == v and abs(v) != float("inf")
                   for v in wp):
            raise PathError(f"waypoint {i} contains a non-finite joint value: {wp}")
