"""Joint soft limits: config overrides, and the fleet's measured values.

These exist because the failure mode is *silent*. An unknown key in a fleet entry
is not validated, so a `joint_limit_overrides` block sitting in config next to a
driver that doesn't read it looks completely correct and enforces nothing — which
is what happened when the measured J5 value was first pasted in.
"""
import pytest

from core.config import DEFAULT_FLEET
from drivers.xarm.driver import XArmDriver

# The Lite 6 model table, as the SDK reports it. Normally backfilled on connect;
# injected here so the override logic is testable with no hardware.
MODEL_TABLE = [(-360, 360), (-150, 150), (-3.5, 300), (-360, 360), (-97.0, 180.0), (-360, 360)]


def _driver(device_id: str) -> XArmDriver:
    cfg = next(e for e in DEFAULT_FLEET if e["id"] == device_id)
    d = XArmDriver(device_id, cfg)
    d._joint_limits = list(MODEL_TABLE)
    return d


def test_both_arms_get_the_flange_camera_j5_override():
    """The measured J5 clearance must actually narrow the limit, not just sit in config.

    Both arms, not one: each carries a RealSense on the flange, and the bound was
    re-measured with the camera mounted after it folded the wrist into the camera
    (collision error 31). That tightened the earlier camera-less 113.9 to 95.0.
    """
    for arm in ("left", "right"):
        assert _driver(arm).limits.joints[4] == (-78.4, 95.0), arm


def test_override_leaves_other_joints_alone():
    joints = _driver("left").limits.joints
    for i in (0, 1, 2, 3, 5):
        assert joints[i] == MODEL_TABLE[i], f"J{i + 1} was modified"


@pytest.mark.parametrize("angle", [95.01, 120.0, -78.41, -90.0])
def test_targets_outside_the_override_are_rejected(angle):
    reason = _driver("left").check_joint_target([0, 0, 0, 0, angle, 0])
    assert reason is not None and "soft limit" in reason


@pytest.mark.parametrize("angle", [95.0, 90.0, 0.0, -78.4])
def test_targets_inside_the_override_are_accepted(angle):
    assert _driver("left").check_joint_target([0, 0, 0, 0, angle, 0]) is None


def test_the_override_still_tightens_against_the_model_table():
    """The configured bound must be strictly inside the mechanical range."""
    lo, hi = _driver("right").limits.joints[4]
    model_lo, model_hi = MODEL_TABLE[4]
    assert model_lo <= lo and hi <= model_hi
    assert (lo, hi) != (model_lo, model_hi), "override is not narrowing anything"


def test_overrides_can_only_tighten_never_widen():
    """A too-wide override must clamp to the mechanical range, not extend past it."""
    d = XArmDriver("left", {"joint_limit_overrides": {"5": [-999.0, 999.0]}})
    d._joint_limits = list(MODEL_TABLE)
    assert d.limits.joints[4] == MODEL_TABLE[4]


def test_override_key_is_one_based():
    """Keys are J1..J6 as labelled on the arm, so "5" is index 4 — not index 5."""
    d = XArmDriver("left", {"joint_limit_overrides": {"5": [-10.0, 10.0]}})
    d._joint_limits = list(MODEL_TABLE)
    joints = d.limits.joints
    assert joints[4] == (-10.0, 10.0)
    assert joints[5] == MODEL_TABLE[5]


def test_out_of_range_key_is_rejected_loudly():
    from drivers.base import DriverError

    d = XArmDriver("left", {"joint_limit_overrides": {"9": [-10.0, 10.0]}})
    d._joint_limits = list(MODEL_TABLE)
    with pytest.raises(DriverError, match="out of range"):
        _ = d.limits


def test_inverted_bounds_are_rejected_loudly():
    from drivers.base import DriverError

    d = XArmDriver("left", {"joint_limit_overrides": {"5": [50.0, -50.0]}})
    d._joint_limits = list(MODEL_TABLE)
    with pytest.raises(DriverError, match="lo >= hi"):
        _ = d.limits
