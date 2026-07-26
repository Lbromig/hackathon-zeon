"""A soft limit must never trap the arm.

Regression test for a real incident: a measured J5 limit of [-78.4, 95.0] was applied
while the arm was parked at J5 = 96.8. Every target — including a jog on an unrelated
joint — kept J5 out of range, so every move was refused and the only way out was
editing config. A limit with no escape is worse than no limit.

The rule: a target that strictly reduces an existing violation is allowed; one that
holds it or deepens it is not.
"""
from __future__ import annotations

import pytest

from drivers.capabilities.arm import ArmDriver, ArmLimits, _moves_toward_range

LO, HI = -78.4, 95.0


class _Arm(ArmDriver):
    """Minimal concrete ArmDriver — only the limit logic is under test."""

    def __init__(self, joints):
        super().__init__("test", {"limits": {"joints": [[LO, HI]] * 6}})
        self._joints = list(joints)

    # the abstract surface, unused here
    def connect(self): ...
    def disconnect(self): ...
    def status(self): return {}
    @property
    def info(self): return None
    def enable(self, on=True): ...
    def home(self): ...
    def stop(self, emergency=False): ...
    def clear_errors(self): ...
    def get_pose(self): ...
    def move_to(self, pose, speed=None, wait=True): ...
    def move_relative(self, **kw): ...
    def get_joints(self): return list(self._joints)
    def move_joints(self, angles, speed=None, wait=True): ...
    def move_joints_relative(self, deltas, speed=None, wait=True): ...
    def grip(self, width=None, force=None): ...
    def release(self): ...
    def gripper_width(self): return None


def _target(base, j5):
    t = list(base)
    t[4] = j5
    return t


PARKED_OUT = [0.0, 0.0, 0.0, 0.0, 96.8, 0.0]
PARKED_IN = [0.0, 0.0, 0.0, 0.0, 40.0, 0.0]


def test_in_range_target_is_allowed_normally():
    arm = _Arm(PARKED_IN)
    assert arm.check_joint_target(_target(PARKED_IN, 50.0), PARKED_IN) is None


def test_out_of_range_target_is_refused_from_a_valid_pose():
    arm = _Arm(PARKED_IN)
    assert arm.check_joint_target(_target(PARKED_IN, 120.0), PARKED_IN) is not None


def test_parked_outside_can_move_back_toward_the_range():
    """The escape hatch. Without this the arm is stranded."""
    arm = _Arm(PARKED_OUT)
    assert arm.check_joint_target(_target(PARKED_OUT, 96.0), PARKED_OUT) is None
    assert arm.check_joint_target(_target(PARKED_OUT, 88.0), PARKED_OUT) is None


def test_parked_outside_cannot_go_further_out():
    arm = _Arm(PARKED_OUT)
    reason = arm.check_joint_target(_target(PARKED_OUT, 99.0), PARKED_OUT)
    assert reason and "parked outside" in reason


def test_parked_outside_cannot_stand_still_while_other_joints_move():
    """The J5 clearance depends on J3 too, so moving other joints while J5 is out
    is exactly what the limit exists to prevent."""
    arm = _Arm(PARKED_OUT)
    sideways = list(PARKED_OUT)
    sideways[0] += 5.0
    assert arm.check_joint_target(sideways, PARKED_OUT) is not None


def test_the_refusal_explains_how_to_recover():
    arm = _Arm(PARKED_OUT)
    reason = arm.check_joint_target(_target(PARKED_OUT, 99.0), PARKED_OUT)
    assert "only moves back toward the range are allowed" in reason


def test_without_current_angles_it_falls_back_to_a_plain_range_check():
    """Callers that cannot read the arm still get the conservative behaviour."""
    arm = _Arm(PARKED_OUT)
    assert arm.check_joint_target(_target(PARKED_OUT, 96.0)) is not None


@pytest.mark.parametrize("now,target,expected", [
    (96.8, 96.0, True),     # above hi, coming back
    (96.8, 96.8, False),    # no change is not recovery
    (96.8, 99.0, False),    # deeper
    (-90.0, -85.0, True),   # below lo, coming back
    (-90.0, -95.0, False),  # deeper
    (40.0, 120.0, False),   # was in range: never a recovery
])
def test_moves_toward_range(now, target, expected):
    assert _moves_toward_range(now, target, LO, HI) is expected
