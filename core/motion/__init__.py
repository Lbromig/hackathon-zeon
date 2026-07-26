"""Motion helpers that are not the driver's job.

Deliberately thin: `cap_ops` is the only member. The pick-and-place planner and the
`ArmDriverMover` adapter that used to live here were metres/radians world-frame code
serving the deleted digital twin, with no caller in the target scope.
"""
from . import cap_ops
from . import path_teach

__all__ = ["cap_ops", "path_teach"]
