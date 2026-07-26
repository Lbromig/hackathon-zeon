"""Named speed tiers, and the clamp that makes them safe (R-ENG-14).

The requirement's last sentence is the one with teeth: raw speed numbers must not appear in
plan data. That only buys anything if a tier can never outrank a soft limit, so the clamp is
what most of this file is about.
"""
from __future__ import annotations

import pytest

from core.speeds import (ARM_TIERS, DEFAULT_TIER, LH_TIERS, TIERS, Speeds, for_arm,
                         for_liquid_handler, normalize, resolve)


class Limits:
    """Stand-in for `ArmLimits` — `core/` must not import `drivers/`, so `for_arm` is
    duck-typed on exactly these two attributes."""

    def __init__(self, linear: float, angular: float) -> None:
        self.max_speed_linear = linear
        self.max_speed_angular = angular


# --- the three tiers ----------------------------------------------------------

def test_there_are_exactly_three_tiers_and_they_are_ordered():
    assert TIERS == ("slow", "medium", "fast")
    for table in (ARM_TIERS, LH_TIERS):
        linear = [table[t][0] for t in TIERS]
        assert linear == sorted(linear), f"{table} is not monotonic in linear speed"


def test_medium_matches_the_drivers_existing_defaults():
    """An unspecified tier must behave exactly as the bench does today — the xArm driver's
    own defaults are 100 mm/s TCP and 20 deg/s joint."""
    assert DEFAULT_TIER == "medium"
    assert ARM_TIERS["medium"] == (100.0, 20.0)


def test_slow_is_slow_enough_to_watch_and_stop():
    """R-INIT-3 homes on `slow`, and the point of that is that an operator can see an
    unexpected trajectory and reach the e-stop."""
    assert ARM_TIERS["slow"][0] <= 40.0
    assert ARM_TIERS["slow"][1] <= 10.0


def test_a_tier_resolves_per_device_class():
    """One tier name means the right thing on an arm and on a gantry, which is why plan data
    can name a tier at all."""
    assert resolve("slow", device_class="arm").linear != \
           resolve("slow", device_class="liquid_handler").linear


def test_both_axes_are_always_reported():
    """Returning only the axis the caller happens to want is how "60 deg/s" becomes
    60 mm/s — a mistake this codebase has already had to reason about once."""
    speeds = resolve("fast")
    assert speeds.linear > 0 and speeds.angular > 0
    lh = resolve("fast", device_class="liquid_handler")
    assert lh.angular == 0.0            # no rotary axis, stated rather than absent


# --- clamping (the reason tiers are safe) -------------------------------------

def test_a_tier_cannot_widen_a_soft_limit():
    speeds = for_arm("fast", Limits(linear=50.0, angular=10.0))
    assert speeds.linear == 50.0 and speeds.angular == 10.0
    assert speeds.clamped is True


def test_a_tier_below_the_limit_is_untouched():
    speeds = for_arm("slow", Limits(linear=200.0, angular=60.0))
    assert (speeds.linear, speeds.angular) == ARM_TIERS["slow"]
    assert speeds.clamped is False


def test_tightening_the_soft_limit_tightens_every_tier():
    """The property that makes this indirection worth having: one config change bounds the
    whole plan, and no plan can opt out of it."""
    tight = Limits(linear=25.0, angular=5.0)
    for tier in TIERS:
        speeds = for_arm(tier, tight)
        assert speeds.linear <= 25.0 and speeds.angular <= 5.0


def test_only_the_axis_that_exceeds_is_clamped():
    speeds = for_arm("fast", Limits(linear=500.0, angular=10.0))
    assert speeds.linear == ARM_TIERS["fast"][0]
    assert speeds.angular == 10.0 and speeds.clamped is True


def test_a_zero_soft_limit_does_not_resolve_to_do_not_move():
    """A misconfigured `max_speed_linear: 0` reads as a stuck arm rather than as a bad
    limit, which is the harder bug to find."""
    speeds = for_arm("medium", Limits(linear=0.0, angular=0.0))
    assert speeds.linear >= 1.0 and speeds.angular >= 1.0


def test_missing_limits_leave_the_tier_nominal():
    """A device with no configured caps must still get a usable speed, not None."""
    speeds = for_arm("medium", None)
    assert (speeds.linear, speeds.angular) == ARM_TIERS["medium"]
    assert speeds.clamped is False


def test_the_liquid_handler_clamps_too():
    speeds = for_liquid_handler("fast", max_linear=15.0)
    assert speeds.linear == 15.0 and speeds.clamped is True


# --- unknown tiers ------------------------------------------------------------

def test_an_unknown_tier_falls_back_to_medium_with_a_warning(caplog):
    """An LLM-authored plan (R-UI-7) naming a tier that does not exist should run at a
    defensible speed rather than abort mid-workflow — but silently substituting a speed is
    how a "fast" step becomes a slow one nobody notices."""
    with caplog.at_level("WARNING"):
        assert normalize("blazing") == "medium"
    assert any("blazing" in r.getMessage() for r in caplog.records)


def test_no_tier_at_all_is_medium_and_silent(caplog):
    """`None` means "unspecified", which is not a mistake and must not warn."""
    with caplog.at_level("WARNING"):
        assert normalize(None) == "medium"
    assert not caplog.records


@pytest.mark.parametrize("tier", TIERS)
def test_every_tier_resolves_for_every_class(tier):
    for device_class in ("arm", "liquid_handler"):
        speeds = resolve(tier, device_class=device_class)
        assert isinstance(speeds, Speeds) and speeds.tier == tier


# --- the reported form --------------------------------------------------------

def test_the_resolved_speed_is_reportable():
    """`MoveOutputs.resolved_speed` records what was commanded, not what was asked for —
    the only version worth having after the fact."""
    reported = for_arm("fast", Limits(linear=50.0, angular=10.0)).as_dict()
    assert reported == {"tier": "fast", "linear": 50.0, "angular": 10.0, "clamped": True}
