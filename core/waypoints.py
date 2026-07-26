"""The canonical workflow waypoints, and per-arm ownership enforcement (R-WP-1…5).

Two things live here, and nothing else:

1. **The spec** — the 15 waypoints the handover workflow names, each pinned to the one
   device that owns it, the workflow step it serves, its intended speed tier and a
   one-line note on what the arm is doing there. This is reviewed code rather than a JSON
   file because it is a *contract*: the plan (S10), the arm handlers (S2) and the teach UI
   must agree on the same 15 names, and a typo in any of them must be a test failure, not
   a silently untaught waypoint discovered on the bench.
2. **Resolution** — turning `(device, name)` into a taught pose, refusing every pairing
   that could send an arm to the *other* arm's taught point (R-WP-2).

The taught values themselves are data: they live in ``settings.teach_poses_file``
(``data/teach_poses.json``), device-scoped, written by the teach API. Renaming or adding a
waypoint is a spec edit here plus a re-teach — never a change to the motion code, which
treats a waypoint name as an opaque label (R-ARM-9, and `ArmWaypoint`'s docstring).

**No FastAPI, no drivers.** The engine imports this; so does the API; so do the tests.

Why ownership is enforced rather than assumed
---------------------------------------------
The failure this module exists to prevent is one arm moving to a point taught on the
other. It is not hypothetical: the two arms share a table, several waypoints are
near-mirror images, `HOME` exists once per arm and means a different pose on each
(R-WP-4), and the naive lookup — "find a pose with this name" — resolves by search order
and picks the wrong arm's point silently. So:

* a spec waypoint the acting device does not own is **refused**, naming both the owner and
  the actor;
* a name that is taught only for *another* device is **refused** with the same clarity,
  never quietly substituted;
* missing waypoints are reported as ``(device, name)`` pairs, never bare names (R-WP-5).

Naming note — do not "fix" the TRANSITION_*/LIQUID_HANDLER_* device
-------------------------------------------------------------------
The original brief names the three ``TRANSITION_*`` and two ``LIQUID_HANDLER_*``
waypoints (workflow steps 14–18) with a ``LEFT_ARM_`` prefix, while the device that
actually performs those moves is the **right** arm — it is the arm carrying the open tube
across the tables to the deck. Open question **Q3** resolved this: the *names* are the
mistake, not the acting device. The device prefix is dropped entirely (a prefix inside a
device-scoped key is redundancy that misleads every future reader), and all five belong to
``right``. If you are here because a waypoint called ``TRANSITION_MID_TABLE`` "obviously"
belongs to the left arm: it does not. See docs/v2/REQUIREMENTS.md §16 Q3 and
docs/v2/ARCHITECTURE_REVIEW.md Q-WP-1.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from core import teach_poses
from core.speeds import SpeedTier

# Device ids from the fleet config (`core/config.py`). Waypoint names are unique only
# *within* a device (R-WP-4), so the device is part of every identity in this module.
LEFT = "left"
RIGHT = "right"
DEVICES: tuple[str, ...] = (LEFT, RIGHT)

#: The one waypoint every arm has, and the only name owned by more than one device.
#: `HOME` on `left` and `HOME` on `right` are different poses; there is no shared home
#: and initialization warns rather than guessing when one is missing (R-INIT, R-ARM-1).
HOME = "HOME"


@dataclass(frozen=True)
class WaypointSpec:
    """One waypoint of the canonical workflow.

    `speed` is the *intended* tier, resolved to numbers by `core.speeds` at dispatch —
    plan data and this spec never carry raw speeds (R-ENG-14).
    """
    name: str
    device: str
    step: int
    speed: SpeedTier
    note: str


# The 15 waypoints, in the order the workflow visits them.
#
# Step numbers: steps 14–18 are pinned by the brief (the three transitions then the two
# liquid-handler points — see REQUIREMENTS §16 Q3, which quotes them). Steps 1–13 are the
# documented order of the uncap sequence that precedes them: right arm takes the tube out
# of the rack, left arm takes the cap off and stores it, right arm then carries the open
# tube across. Steps 19 (the vision servo loop) and 20 (liquid handler retracts Z) name no
# waypoint and so appear nowhere below.
#
# Speed tiers follow the brief: every APPROACH_* is `fast` except APPROACH_TUBE_TRANSFER,
# which is `medium`; the terminal precision moves — TUBE, CAP_GRAB, CAP_STORE — and HOME
# are `slow`; the long table traverses are `fast`. LIQUID_HANDLER_DECK is the one tier the
# brief does not state: it is `slow` here because it is a terminal precision move into the
# deck with the servo loop starting from it, and getting that one wrong is expensive.
SPEC: tuple[WaypointSpec, ...] = (
    # --- right arm: tube out of the rack -------------------------------------------
    WaypointSpec(
        "APPROACH_RACK", RIGHT, 1, "fast",
        "clear standoff above the tube rack, before anything is over a tube",
    ),
    WaypointSpec(
        "APPROACH_TUBE_GRAB", RIGHT, 2, "fast",
        "lined up directly above the target tube, jaws open",
    ),
    WaypointSpec(
        "TUBE", RIGHT, 3, "slow",
        "down on the tube body at grip height — the jaws close here",
    ),
    # --- left arm: cap off and stored ----------------------------------------------
    WaypointSpec(
        "APPROACH_CAP_GRAB", LEFT, 5, "fast",
        "left tool brought in beside the cap while the right arm holds the tube",
    ),
    WaypointSpec(
        "CAP_GRAB", LEFT, 6, "slow",
        "closed onto the cap at grip height — decap (360° in 90° bites) runs from here",
    ),
    WaypointSpec(
        "APPROACH_CAP_STORE", LEFT, 9, "fast",
        "cap carried clear of the tube, standing off above its store position",
    ),
    WaypointSpec(
        "CAP_STORE", LEFT, 10, "slow",
        "cap lowered into the store — the jaws open here and leave it behind",
    ),
    # --- right arm: carry the open tube to the deck ---------------------------------
    WaypointSpec(
        "APPROACH_TUBE_TRANSFER", RIGHT, 13, "medium",
        "open tube lifted clear of the rack, ready to traverse; medium — it is carrying",
    ),
    WaypointSpec(
        "TRANSITION_ROBOT_TABLE", RIGHT, 14, "fast",
        "traverse waypoint over the robot table (right arm acts — see the naming note)",
    ),
    WaypointSpec(
        "TRANSITION_MID_TABLE", RIGHT, 15, "fast",
        "traverse waypoint mid-table, between the two benches",
    ),
    WaypointSpec(
        "TRANSITION_LIQUID_HANDLER_TABLE", RIGHT, 16, "fast",
        "traverse waypoint over the liquid-handler table",
    ),
    WaypointSpec(
        "LIQUID_HANDLER_APPROACH_DECK", RIGHT, 17, "fast",
        "standoff outside the deck envelope, tube still clear of the gantry",
    ),
    WaypointSpec(
        "LIQUID_HANDLER_DECK", RIGHT, 18, "slow",
        "tube presented on the deck under the pipette — the servo loop starts here",
    ),
    # --- both arms ------------------------------------------------------------------
    # One name, two owners, two entirely different poses (R-WP-4). `HOME` is also each
    # arm's **initialization** home (R-INIT, R-ARM-1): initialization warns rather than
    # guessing when it is missing, which is why it is on the checklist at all.
    #
    # Step numbers: the left arm's home is a move *inside* the workflow — step 12, where it
    # gets out of the right arm's traverse path. The right arm's home is not one of the 20
    # steps (step 20 is the liquid handler retracting Z), so it takes step 0: initialization,
    # and the pose to return to when a session ends. 0 also sorts it to the top of the
    # right arm's checklist, which is the right place — teach a safe pose first.
    WaypointSpec(
        HOME, LEFT, 12, "slow",
        "left arm parked clear of the right arm's traverse path; also its init home",
    ),
    WaypointSpec(
        HOME, RIGHT, 0, "slow",
        "right arm's init home and rest pose — teach this one first, it is the way back",
    ),
)

_BY_DEVICE: dict[str, dict[str, WaypointSpec]] = {d: {} for d in DEVICES}
for _s in SPEC:
    if _s.device not in _BY_DEVICE:
        raise AssertionError(f"waypoint {_s.name!r} names unknown device {_s.device!r}")
    if _s.name in _BY_DEVICE[_s.device]:
        raise AssertionError(f"waypoint {_s.name!r} listed twice for {_s.device!r}")
    _BY_DEVICE[_s.device][_s.name] = _s

_OWNERS: dict[str, tuple[str, ...]] = {}
for _s in SPEC:
    _OWNERS[_s.name] = _OWNERS.get(_s.name, ()) + (_s.device,)


# --- errors -------------------------------------------------------------------------

class WaypointError(LookupError):
    """Base for every refusal in this module. A `LookupError`, because every one of them
    is "that (device, waypoint) does not resolve" — and callers that only want to report
    a reason can catch this one type."""


class WaypointNotOwned(WaypointError):
    """The acting device does not own the named waypoint (R-WP-2).

    Raised *before* any motion, and the message names both the owner and the actor,
    because "waypoint not found" leaves the operator to guess which arm is wrong.
    """

    def __init__(self, device: str, name: str, owners: Iterable[str]) -> None:
        self.device, self.name = device, name
        self.owners = tuple(owners)
        if self.owners:
            owned = " and ".join(repr(o) for o in self.owners)
            detail = (
                f"waypoint {name!r} is owned by {owned}, but {device!r} is the acting "
                f"device. Waypoints are per-arm (R-WP-1): refusing rather than moving "
                f"{device!r} to another arm's taught point. Teach a {name!r} for "
                f"{device!r}, or send this move to {self.owners[0]!r}."
            )
        else:
            detail = (
                f"waypoint {name!r} is not a workflow waypoint for any device, and "
                f"{device!r} has no taught pose by that name. Waypoints for {device!r}: "
                f"{', '.join(names_for(device)) or 'none'}."
            )
        super().__init__(detail)


class WaypointNotTaught(WaypointError, teach_poses.MissingPose):
    """The pairing is legitimate but nobody has taught it yet.

    Subclasses `teach_poses.MissingPose` so existing pre-flight code that catches that
    keeps working; carries the `(device, name)` pair for R-WP-5 reporting.
    """

    def __init__(self, device: str, name: str, detail: str) -> None:
        self.device, self.name = device, name
        super().__init__(detail)

    @property
    def pair(self) -> tuple[str, str]:
        return (self.device, self.name)


# --- the spec -----------------------------------------------------------------------

def specs_for(device: str) -> tuple[WaypointSpec, ...]:
    """That device's waypoints, in workflow order. Empty for an unknown device."""
    return tuple(sorted(_BY_DEVICE.get(device, {}).values(), key=lambda s: s.step))


def names_for(device: str) -> tuple[str, ...]:
    """The only waypoint names this device may be commanded to (R-WP-1/3)."""
    return tuple(s.name for s in specs_for(device))


def spec_for(device: str, name: str) -> WaypointSpec | None:
    """The spec entry for this exact pairing, or None if the device does not own it."""
    return _BY_DEVICE.get(device, {}).get(name)


def owners_of(name: str) -> tuple[str, ...]:
    """Devices that own a waypoint name — two for `HOME`, one otherwise, none if the name
    is not in the spec at all."""
    return _OWNERS.get(name, ())


def is_spec_name(name: str) -> bool:
    return name in _OWNERS


def assert_owned(device: str, name: str) -> WaypointSpec | None:
    """Refuse a pairing the spec forbids; return the spec entry, or None for an ad-hoc name.

    A name that is in the spec must be used by its owner and nobody else. A name that is
    *not* in the spec is a scratch/ad-hoc teach point: this function allows it (the teach
    panel needs throwaway points) and the caller learns it is ad-hoc from the `None`.
    """
    owners = owners_of(name)
    if owners and device not in owners:
        raise WaypointNotOwned(device, name, owners)
    return spec_for(device, name)


# --- resolution ---------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedWaypoint:
    """A taught waypoint, ready to move to.

    `joints` is preferred over `xyz_rpy` for replay: those angles were physically
    reached, so there is no IK branch to guess at — the same reasoning as `goto_pose` in
    the teach API and `core.teach_poses`.
    """
    device: str
    name: str
    xyz_rpy: list[float]
    joints: list[float] | None
    gripper_width: float | None
    saved_at: str
    spec: WaypointSpec | None

    @property
    def is_spec(self) -> bool:
        return self.spec is not None

    @property
    def speed(self) -> SpeedTier | None:
        """The intended tier, or None for an ad-hoc point (caller picks its own)."""
        return self.spec.speed if self.spec else None


def resolve(device: str, name: str, *, path: str | None = None) -> ResolvedWaypoint:
    """Resolve `(device, name)` to a taught pose, or raise.

    The one entry point the engine needs, and the whole ownership rule in one place:

    * a spec waypoint owned by another device        -> `WaypointNotOwned`
    * an ad-hoc name taught only on another device   -> `WaypointNotOwned` (a scratch point
      is device-scoped too; this is the cross-arm mistake and is refused as firmly)
    * a pairing this device owns but has not taught  -> `WaypointNotTaught`, which says so
      even when the *other* arm has one — that other pose is never substituted

    There is no search-order fallback and no same-name substitution anywhere in it.
    """
    spec = assert_owned(device, name)
    library = teach_poses.load(path)
    entry = library.get(device, {}).get(name)

    if entry is None:
        elsewhere = sorted(d for d, poses in library.items()
                           if d != device and name in poses)
        if elsewhere and spec is None:
            # Not in the spec at all, and taught only somewhere else: ownership is
            # whatever the store says, and the store says not this device.
            raise WaypointNotOwned(device, name, elsewhere)
        also = ""
        if elsewhere:
            # `HOME` is the case that matters: both arms own the name, one has taught it.
            # Naming the other arm is useful; using its pose would be the collision.
            also = (f" {' and '.join(repr(d) for d in elsewhere)} has a {name!r} taught, "
                    f"and it is deliberately not used — it is a different pose.")
        raise WaypointNotTaught(
            device, name,
            f"waypoint ({device!r}, {name!r}) is not taught yet. Teach it on {device!r} in "
            f"the teach tab; nothing is guessed.{also} Taught on {device!r} so far: "
            f"{', '.join(sorted(library.get(device, {}))) or 'nothing'}.",
        )

    pose = entry.get("pose") or {}
    missing = [k for k in ("x", "y", "z") if pose.get(k) is None]
    joints = [float(j) for j in entry["joints"]] if entry.get("joints") else None
    if missing and not joints:
        raise WaypointNotTaught(
            device, name,
            f"waypoint ({device!r}, {name!r}) is stored but unusable: no joint angles and "
            f"no cartesian position (missing {missing}). Re-teach it.",
        )
    return ResolvedWaypoint(
        device=device,
        name=name,
        xyz_rpy=[float(pose.get(k, 0.0)) for k in
                 ("x", "y", "z", "roll", "pitch", "yaw")],
        joints=joints,
        gripper_width=entry.get("gripper_width"),
        saved_at=str(entry.get("saved_at") or ""),
        spec=spec,
    )


# --- status and pre-flight ----------------------------------------------------------

@dataclass(frozen=True)
class WaypointStatus:
    """One row of the operator's checklist."""
    device: str
    name: str
    step: int
    speed: SpeedTier
    note: str
    taught: bool
    saved_at: str | None
    has_joints: bool

    @property
    def pair(self) -> tuple[str, str]:
        return (self.device, self.name)


def status(device: str, *, path: str | None = None) -> list[WaypointStatus]:
    """That device's spec waypoints with taught/untaught state, in workflow order.

    The replacement for the deleted `required_poses` endpoint, which derived its list from
    the choreography that no longer exists.
    """
    taught = teach_poses.load(path).get(device, {})
    rows = []
    for s in specs_for(device):
        entry = taught.get(s.name)
        rows.append(WaypointStatus(
            device=s.device, name=s.name, step=s.step, speed=s.speed, note=s.note,
            taught=entry is not None,
            saved_at=(str(entry.get("saved_at") or "") or None) if entry else None,
            has_joints=bool(entry and entry.get("joints")),
        ))
    return rows


def missing(device: str, names: Iterable[str] | None = None, *,
            path: str | None = None) -> list[tuple[str, str]]:
    """Untaught waypoints as `(device, name)` pairs — never bare names (R-WP-5).

    `names` defaults to that device's whole spec. Names the device does not own are
    reported as missing too rather than skipped: asking about them is itself the bug.
    """
    taught = teach_poses.load(path).get(device, {})
    wanted = list(names) if names is not None else list(names_for(device))
    return [(device, n) for n in wanted if n not in taught]


def missing_for_plan(pairs: Iterable[tuple[str, str]], *,
                     path: str | None = None) -> list[tuple[str, str]]:
    """Pre-flight a whole plan's `(device, waypoint)` pairs in one pass.

    Deduplicated, order preserved. Returns pairs, so the operator reads *which arm* is
    untaught and not just a bare name (R-WP-5).
    """
    library = teach_poses.load(path)
    out: list[tuple[str, str]] = []
    for device, name in pairs:
        if (device, name) in out:
            continue
        if name not in library.get(device, {}):
            out.append((device, name))
    return out


# --- library problems ---------------------------------------------------------------

def _canonical(name: str) -> str:
    """A loose form used *only* to spot near-miss names, never to resolve one."""
    return name.strip().upper().replace(" ", "_").replace("-", "_")


_CANONICAL_SPEC: dict[str, tuple[str, ...]] = {}
for _s in SPEC:
    _c = _canonical(_s.name)
    _CANONICAL_SPEC[_c] = _CANONICAL_SPEC.get(_c, ()) + (_s.device,)


@dataclass(frozen=True)
class WaypointProblem:
    """Something wrong with the taught library that would otherwise pass unnoticed.

    `blocking` separates "this will break the run" from "you should know about this":
    a scratch point is fine, a spec waypoint taught on the wrong arm is not.
    """
    kind: str          # missing | wrong_device | name_mismatch | ad_hoc | unusable
    device: str
    name: str
    detail: str
    blocking: bool


def problems(*, path: str | None = None) -> list[WaypointProblem]:
    """Everything wrong with the taught library, as the readiness panel should show it.

    Four cases, none of which the naive "is it in the file?" check catches:

    * **missing** — a spec waypoint nobody has taught (blocking).
    * **wrong_device** — a spec waypoint taught under a device that does not own it
      (blocking). The pose is real and looks taught in the library, but no move will ever
      use it, and the arm that *should* have it is untaught.
    * **name_mismatch** — a taught name that is a spec name modulo case/spacing
      (`home`, `Approach Rack`). Not blocking, but it is why "I taught it" and "the
      checklist says untaught" can both be true.
    * **ad_hoc** — a scratch point with no relation to the spec. Reported so the
      checklist means something; never blocking, since scratch points are wanted.
    * **unusable** — stored with neither joints nor a cartesian position (blocking).
    """
    library = teach_poses.load(path)
    out: list[WaypointProblem] = []

    for device in DEVICES:
        for pair in missing(device, path=path):
            s = spec_for(*pair)
            out.append(WaypointProblem(
                kind="missing", device=device, name=pair[1], blocking=True,
                detail=f"({device!r}, {pair[1]!r}) is not taught — workflow step "
                       f"{s.step if s else '?'} has nowhere to go.",
            ))

    for device, poses in sorted(library.items()):
        for name, entry in sorted(poses.items()):
            owners = owners_of(name)
            if owners and device not in owners:
                out.append(WaypointProblem(
                    kind="wrong_device", device=device, name=name, blocking=True,
                    detail=f"{name!r} is taught under {device!r} but is owned by "
                           f"{' and '.join(repr(o) for o in owners)}. No move will use "
                           f"it; re-teach it on the right arm and delete this one.",
                ))
                continue
            if not owners:
                canon = _CANONICAL_SPEC.get(_canonical(name))
                if canon:
                    out.append(WaypointProblem(
                        kind="name_mismatch", device=device, name=name, blocking=False,
                        detail=f"{name!r} looks like the workflow waypoint of the same "
                               f"name but does not match it exactly — names are "
                               f"case-sensitive, so this does not count as taught.",
                    ))
                else:
                    out.append(WaypointProblem(
                        kind="ad_hoc", device=device, name=name, blocking=False,
                        detail=f"{name!r} on {device!r} is not a workflow waypoint — a "
                               f"scratch point. Fine to keep; it is not on the checklist.",
                    ))
                continue
            # `is None`, not falsiness: x = 0.0 is a legitimate taught coordinate.
            if not entry.get("joints") and (entry.get("pose") or {}).get("x") is None:
                out.append(WaypointProblem(
                    kind="unusable", device=device, name=name, blocking=True,
                    detail=f"({device!r}, {name!r}) is stored with neither joint angles "
                           f"nor a cartesian position. Re-teach it.",
                ))
    return out


# --- progress -----------------------------------------------------------------------

@dataclass(frozen=True)
class DeviceProgress:
    """"7 of 9 taught", for the arm the operator is standing in front of."""
    device: str
    taught: int
    total: int
    waypoints: list[WaypointStatus]
    extra: list[str]        # taught names that are not spec waypoints for this device

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.taught == self.total


def progress(device: str, *, path: str | None = None) -> DeviceProgress:
    rows = status(device, path=path)
    spec_names = set(names_for(device))
    taught_names = teach_poses.load(path).get(device, {})
    return DeviceProgress(
        device=device,
        taught=sum(1 for r in rows if r.taught),
        total=len(rows),
        waypoints=rows,
        extra=sorted(n for n in taught_names if n not in spec_names),
    )
