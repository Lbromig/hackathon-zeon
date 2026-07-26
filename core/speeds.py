"""Named speed tiers: `fast` / `medium` / `slow` -> per-device linear and angular speeds.

R-ENG-14, and its last sentence is the requirement that shapes this module: **raw speed
numbers must not appear in plan data.** So a plan says `speed: "slow"` and this is the only
place that turns a tier into a number. The consequences are worth being explicit about:

* A plan is reviewable. "slow" is a decision an operator can check; `18.0` is a number
  nobody can check without knowing which device and which axis it applies to.
* A plan is portable across devices. The arms and the liquid handler have different safe
  envelopes; one tier means the right thing on both because the tier is resolved *per
  device kind*, not globally.
* A tier can never widen a configured soft limit. Resolution ends in a clamp against the
  device's own `max_speed_*` (`TEACH_LIMITS` / a per-device override), so tightening the
  soft limit tightens every tier automatically. A raw number in plan data would silently
  outrank it — which is precisely the failure this indirection buys out.

The tier numbers themselves are the bench's existing defaults widened into three steps, not
new measurements: the xArm driver's own defaults are 100 mm/s TCP and 20 deg/s joint, which
is what `medium` is. `slow` is the tier R-INIT-3 requires for the home move, and it is
deliberately slow enough to watch an unexpected trajectory and reach the e-stop.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from core.obs import get_logger

log = get_logger(__name__)

SpeedTier = Literal["slow", "medium", "fast"]
TIERS: tuple[SpeedTier, ...] = ("slow", "medium", "fast")
DEFAULT_TIER: SpeedTier = "medium"

# Device kinds the tiers resolve differently for. Keyed by capability, not by fleet type, so
# a second arm vendor or a second liquid handler needs no new entry.
DeviceClass = Literal["arm", "liquid_handler"]


@dataclass(frozen=True)
class Speeds:
    """A resolved pair, in the units the drivers take.

    Both are always present, even where a device has no separate angular rate: reporting a
    number the caller then has to guess the meaning of is how "60 deg/s" becomes 60 mm/s.
    The caller picks which field applies to the move it is making — see the note on
    `angular` in the arm API's `_speed`, where exactly that mistake was avoided once.
    """
    tier: SpeedTier
    linear: float          # mm/s
    angular: float         # deg/s
    clamped: bool = False  # a soft limit reduced the tier's nominal value

    def as_dict(self) -> dict[str, float | str | bool]:
        """For `MoveOutputs.resolved_speed` — the log records what was *commanded*, not
        what was asked for, which is the only version worth having after the fact."""
        return {"tier": self.tier, "linear": self.linear, "angular": self.angular,
                "clamped": self.clamped}


# mm/s and deg/s. `medium` matches the xArm driver's own defaults (tcp_speed 100,
# joint_speed 20), so an unspecified tier behaves exactly as the bench does today.
ARM_TIERS: dict[SpeedTier, tuple[float, float]] = {
    "slow":   (30.0, 8.0),    # R-INIT-3's home move: slow enough to watch and to stop
    "medium": (100.0, 20.0),
    "fast":   (180.0, 45.0),
}

# The OT-One gantry. Angular is nominal — the head has no rotary axis — but is carried so
# the shape is uniform and a caller cannot accidentally read a linear speed as angular.
LH_TIERS: dict[SpeedTier, tuple[float, float]] = {
    "slow":   (10.0, 0.0),    # the servo loop's corrective steps are small and near a tube
    "medium": (30.0, 0.0),
    "fast":   (60.0, 0.0),
}

_BY_CLASS: dict[DeviceClass, dict[SpeedTier, tuple[float, float]]] = {
    "arm": ARM_TIERS,
    "liquid_handler": LH_TIERS,
}


def normalize(tier: str | None) -> SpeedTier:
    """A tier name, or `medium` for anything unrecognised — with a warning.

    Not an exception: a plan authored by an LLM (R-UI-7) that names a tier that does not
    exist should run at a defensible speed rather than abort a run mid-workflow. But it must
    say so, because silently substituting a speed is how a "fast" step becomes a slow one
    that nobody notices until the demo drags.
    """
    if tier in TIERS:
        return tier            # type: ignore[return-value]
    if tier is not None:
        log.warning("unknown speed tier %r; using %r. Valid tiers: %s",
                    tier, DEFAULT_TIER, ", ".join(TIERS))
    return DEFAULT_TIER


def resolve(tier: str | None, *, device_class: DeviceClass = "arm",
            max_linear: float | None = None, max_angular: float | None = None) -> Speeds:
    """Resolve a tier for a device class, clamped by that device's soft limits.

    `max_linear` / `max_angular` come from the device's `ArmLimits` (`TEACH_LIMITS`, or a
    per-device override in the fleet). A tier is a *request*; the limit is the authority.
    Clamping rather than refusing is right here — unlike an out-of-envelope liquid-handler
    move (R-LH-3), a speed above the cap has an unambiguous safe interpretation, and
    `clamped` is reported so the log shows the request was reduced.

    A floor of 1.0 mm/s and 1.0 deg/s applies to anything the clamp does not zero: a
    misconfigured `max_speed_linear: 0` would otherwise resolve every tier to "do not move",
    which reads as a stuck arm rather than as a bad limit.
    """
    resolved = normalize(tier)
    linear, angular = _BY_CLASS[device_class][resolved]
    clamped = False

    if max_linear is not None and linear > max_linear:
        linear, clamped = max(1.0, float(max_linear)), True
    if max_angular is not None and angular > max_angular:
        angular, clamped = max(1.0, float(max_angular)), True

    return Speeds(tier=resolved, linear=round(linear, 3), angular=round(angular, 3),
                  clamped=clamped)


def for_arm(tier: str | None, limits: object | None = None) -> Speeds:
    """Resolve for an arm, reading the caps off its `ArmLimits` if one is given.

    Duck-typed rather than importing `drivers.capabilities.arm`: `core/` must not depend on
    `drivers/`, and the only thing needed here is two attributes.
    """
    return resolve(tier, device_class="arm",
                   max_linear=getattr(limits, "max_speed_linear", None),
                   max_angular=getattr(limits, "max_speed_angular", None))


def for_liquid_handler(tier: str | None, *, max_linear: float | None = None) -> Speeds:
    return resolve(tier, device_class="liquid_handler", max_linear=max_linear)
