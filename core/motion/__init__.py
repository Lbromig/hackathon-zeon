"""Motion helpers that are not the driver's job.

Deliberately thin: `cap_ops` is the only member. The pick-and-place planner and the
`ArmDriverMover` adapter were metres/radians world-frame code serving the deleted digital
twin; `path_teach` was dense 10 Hz joint recording plus RDP thinning, which is a different
thing from R-ARM-6's "traverse an ordered set of named waypoints" (Q6).
"""
from . import cap_ops

__all__ = ["cap_ops"]
