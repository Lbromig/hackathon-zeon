"""The plan: an ordered list of actions, its mutations, and the pre-flight that gates a run.

Behaviour, not contract. Everything here builds on the frozen types in `actions.py` and
`events.py` and adds the three things a run needs from a plan:

1. **Identity that survives mutation** (D4/R-ENG-4). `aid` is assigned once, from a
   monotonic counter, and never reused or rewritten; `index` is *derived* and recomputed on
   every mutation. Recorded results are keyed by `aid`, which is what makes an injection in
   the middle of a run renumber the display without losing what step 3 measured. An
   index-keyed store would have silently re-attributed every completed action's outputs one
   row down — visible only as a plausible-looking wrong answer.

2. **Bounded loop materialization** (D8/D14). A `control.loop` body is copied into the flat
   plan **per iteration**, so iteration 4's snapshot is a real indexed row with its own
   outputs, artifacts and logs (R-VIS-8, R-UI-2..5). Three bounds, because materialization
   without them is a plan-growth path to OOM: the loop's own `max_iterations` (itself capped
   at `MAX_LOOP_ITERATIONS`), the global `MAX_MATERIALIZED_ACTIONS` ceiling on expanded
   actions across the whole plan, and **no nested loops** — refused here as well as in
   `Loop`'s validator, because injection is a second way a loop could end up inside a loop.

3. **Pre-flight that refuses rather than discovers** (R-ENG-16/17). Every referenced device
   is in the fleet, every referenced waypoint exists *and is owned by the acting device*
   (R-WP-2), every kind has something that can execute it. Reported as `(device, name)`
   pairs (R-WP-5). A step that cannot run says so; it is never skipped, because a skipped
   step reads downstream as "it ran".

**The cursor lives here but belongs to the runner.** `Plan.cursor` is the *index* of the
action about to execute, and it is index-based on purpose: "inject at the cursor" means
inserting a row *at* that position, so the injected action becomes the next one to run. An
aid-based cursor would have pointed past the injection and skipped it. The runner is the only
writer; the plan exposes the insertion rules that depend on it
(:meth:`Plan.refusal_for_insert`) so they are testable without starting a thread.

Thread-safety: one worker thread mutates while the asyncio side reads a snapshot for the UI
(D1). Every read that spans more than one field takes the lock, for the same reason
`Blackboard` does — a snapshot that catches a half-finished renumber is a plan the client
cannot render.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Collection, Iterable, Iterator, Sequence

from core import waypoints
from core.config import settings

from .actions import (MAX_MATERIALIZED_ACTIONS, ActionBase, ActionResult, ActionState, Loop,
                      Warning_, handler_for, validate_action)
from .events import ActionRow, ReadinessState, RunSnapshot, RunState

#: Kinds the engine executes itself, with no entry in the handler registry. Control flow is
#: not a device operation: `control.loop` *is* the materialization bookkeeping in this module
#: plus the runner's termination test, and `control.checkpoint` is a pause the plan asked
#: for. Pre-flight must not report them as unhandled — that would make every plan with a
#: servo loop fail readiness for a handler that is not supposed to exist.
ENGINE_KINDS: tuple[str, ...] = ("control.loop", "control.checkpoint")

#: Base fields that `ActionRow` carries in its own right; everything else in an action's dump
#: is kind-specific and goes into `params`. `on_failure`, `max_attempts` and `note` stay in
#: `params` deliberately: they are plan data an operator needs to see, and `ActionRow` has no
#: column of its own for them.
_ROW_FIELDS: frozenset[str] = frozenset(
    {"aid", "index", "label", "device", "speed", "parent_aid", "iteration", "origin", "kind"})


# --- errors ------------------------------------------------------------------

class PlanError(ValueError):
    """Base for every refusal in this module, so a caller that only wants to report a reason
    can catch one type. A `ValueError` because every one of them is a bad request against a
    valid plan, not a broken plan."""


class UnknownActionError(PlanError):
    """No action in the plan has that `aid`."""


class InjectRefused(PlanError):
    """An injection the plan will not accept, carrying the reason (D22/R-ENG-13).

    Refusing *with a stated reason* is the required behaviour: quietly inserting somewhere
    else is the failure mode R-ENG-13 names. The reason is operator-facing text, and the
    runner turns it into an `inject_rejected` event so every connected client sees it.
    """

    def __init__(self, reason: str, after_aid: int | None = None) -> None:
        self.reason = reason
        self.after_aid = after_aid
        super().__init__(reason)


class MaterializationCapped(PlanError):
    """The loop asked for another iteration and the bound (D14) refused it.

    Not an error in the plan — an *outcome*. The runner turns it into
    `LoopOutputs.outcome == "exhausted"`, which is exactly what a never-converging loop must
    produce instead of growing the plan.
    """


class NestedLoopRefused(PlanError):
    """A loop would have ended up inside a loop (D14).

    `Loop`'s own validator catches an authored nested loop. This catches the second route:
    injecting a `control.loop` into a materialized loop body, which no validator sees.
    """


# --- labels ------------------------------------------------------------------

def default_label(action: ActionBase) -> str:
    """Human text for a plan row when the author left `label` blank.

    Generated rather than required, because the plan is also LLM-authored (R-UI-7) and an
    unlabelled row rendering as an empty cell is worse than a mechanical description.
    """
    kind = action.kind
    device = action.device
    what = kind.split(".", 1)[-1].replace("_", " ")
    if kind == "arm.waypoint":
        return f"{device}: move to {getattr(action, 'waypoint', '?')}"
    if kind == "arm.traverse":
        return f"{device}: traverse {' -> '.join(getattr(action, 'waypoints', []))}"
    if kind == "arm.gripper":
        return f"{device}: {getattr(action, 'state', '?')} gripper"
    if kind == "vision.identify":
        return f"identify {getattr(action, 'target', '?')} in {device}"
    if kind == "control.loop":
        return f"loop until offset < {getattr(action, 'threshold_mm', '?')} mm"
    if kind == "lifecycle.initialize":
        return f"initialize {device or 'every device'}"
    return f"{device}: {what}" if device else what


def params_of(action: ActionBase) -> dict[str, Any]:
    """The kind-specific half of an action, for `ActionRow.params`.

    A loop's `body` is replaced by a summary. It is not censorship: every body action exists
    in the flat plan as its own row the moment it is materialized, so carrying the template
    as well would duplicate the whole loop into every `plan_replaced` payload — once per
    iteration, on the socket, for the client to ignore.
    """
    dump = action.model_dump(mode="json")
    out = {k: v for k, v in dump.items() if k not in _ROW_FIELDS}
    if action.kind == "control.loop":
        body = out.pop("body", None) or []
        out["body_kinds"] = [b.get("kind") for b in body if isinstance(b, dict)]
        out["body_size"] = len(body)
    return out


# --- pre-flight --------------------------------------------------------------

@dataclass(frozen=True)
class PreflightProblem:
    """One reason a plan cannot run, or one thing the operator should know before it does.

    `code` is machine-readable so the readiness panel matches on a code rather than on
    message text (R-LOG-6 / R-UI-13); `blocking` separates "this run will fail at step 7"
    from "you should know about this".
    """
    code: str
    message: str
    aid: int | None = None
    device: str | None = None
    waypoint: str | None = None
    blocking: bool = True

    def as_warning(self) -> Warning_:
        return Warning_(code=self.code, message=self.message, device=self.device)


@dataclass(frozen=True)
class PreflightReport:
    """What pre-flight found, in the shape the API and the readiness panel both need."""
    problems: tuple[PreflightProblem, ...] = ()

    @property
    def ok(self) -> bool:
        """True when nothing blocking was found. Advisory problems do not block a start."""
        return not self.blocking

    @property
    def blocking(self) -> tuple[PreflightProblem, ...]:
        return tuple(p for p in self.problems if p.blocking)

    @property
    def advisory(self) -> tuple[PreflightProblem, ...]:
        return tuple(p for p in self.problems if not p.blocking)

    @property
    def readiness(self) -> ReadinessState:
        """R-INIT-6 mapping: blocking problems are `failed` (start is refused), advisory ones
        are `degraded` (start needs explicit confirmation naming them, R-ENG-2)."""
        if self.blocking:
            return "failed"
        return "degraded" if self.advisory else "ready"

    def missing_waypoints(self) -> list[tuple[str, str]]:
        """`(device, name)` pairs, never bare names (R-WP-5)."""
        return [(p.device or "", p.waypoint or "")
                for p in self.problems if p.code == "waypoint_not_taught"]

    def unowned_waypoints(self) -> list[tuple[str, str]]:
        return [(p.device or "", p.waypoint or "")
                for p in self.problems if p.code == "waypoint_not_owned"]

    def unknown_devices(self) -> list[str]:
        return [p.device or "" for p in self.problems if p.code == "device_not_in_fleet"]

    def unhandled_kinds(self) -> list[str]:
        return [p.message.split()[0] for p in self.problems if p.code == "no_handler"]

    def as_warnings(self) -> list[Warning_]:
        return [p.as_warning() for p in self.problems]

    def reason(self) -> str:
        """One line naming every blocking problem — the text a refused start reports."""
        if not self.blocking:
            return ""
        return "; ".join(p.message for p in self.blocking)


def _walk(actions: Iterable[ActionBase], parent: ActionBase | None = None
          ) -> Iterator[tuple[ActionBase, ActionBase | None]]:
    """Every action in the plan, loop bodies included, paired with its owning loop.

    Pre-flight has to descend into loop bodies: they have not been materialized yet, so a
    waypoint referenced only from a servo loop's body would otherwise be discovered untaught
    on iteration 1 — with an arm holding an open tube, which is the exact scenario R-ENG-16
    exists to prevent. Body actions carry `aid == 0`, so problems are attributed to the loop.
    """
    for action in actions:
        yield action, parent
        if isinstance(action, Loop):
            yield from _walk(action.body, action)


def _waypoint_refs(actions: Iterable[ActionBase]) -> dict[tuple[str, str], int]:
    """`(device, waypoint)` -> the aid that first referenced it, order preserved.

    The aid is carried so a pre-flight problem can point at a row. A body action has no aid of
    its own before materialization, so it is attributed to its loop.
    """
    refs: dict[tuple[str, str], int] = {}
    for action, parent in _walk(actions):
        names: list[str] = []
        one = getattr(action, "waypoint", None)
        if isinstance(one, str) and one:
            names.append(one)
        many = getattr(action, "waypoints", None)
        if isinstance(many, list):
            names.extend(n for n in many if isinstance(n, str) and n)
        for name in names:
            refs.setdefault((action.device or "", name),
                            action.aid or (parent.aid if parent else 0))
    return refs


def waypoint_pairs(actions: Iterable[ActionBase]) -> list[tuple[str, str]]:
    """Every `(device, waypoint)` the plan references, deduplicated, order preserved."""
    return list(_waypoint_refs(actions))


def _fleet_ids() -> frozenset[str]:
    return frozenset(str(e.get("id")) for e in settings.fleet if e.get("id"))


def preflight(actions: Sequence[ActionBase], *,
              known_devices: Collection[str] | None = None,
              teach_path: str | None = None,
              engine_kinds: Collection[str] = ENGINE_KINDS) -> PreflightReport:
    """Check a plan before the first motion (R-ENG-16).

    Three classes of problem, all blocking, all reported together rather than one at a time —
    an operator fixing a bench wants the whole list, not a fresh failure per attempt:

    * **device_not_in_fleet** — an action names a device that does not exist. It could never
      run, so it must say so now (R-ENG-17).
    * **waypoint_not_owned** / **waypoint_not_taught** — the two halves of R-WP-2 and R-WP-5.
      Ownership is checked *before* taughtness, because "not taught" is the wrong diagnosis
      for a move aimed at the other arm's point: the pose exists, it just is not this arm's.
    * **no_handler** — a kind with nothing registered to execute it. An incomplete engine
      says so rather than failing at step 7 (R-ENG-17).

    An empty plan is reported as advisory rather than blocking: it is not a defect, and
    refusing to "run" it would make an operator hunt for a bench problem that is not there.
    """
    devices = frozenset(known_devices) if known_devices is not None else _fleet_ids()
    problems: list[PreflightProblem] = []

    if not actions:
        problems.append(PreflightProblem(
            code="empty_plan", message="the plan has no actions", blocking=False))

    seen_devices: set[str] = set()
    seen_kinds: set[str] = set()
    for action, parent in _walk(actions):
        aid = action.aid or (parent.aid if parent else 0)
        device = action.device
        if device and device not in devices and device not in seen_devices:
            seen_devices.add(device)
            problems.append(PreflightProblem(
                code="device_not_in_fleet", aid=aid, device=device,
                message=f"device {device!r} is not in the fleet, so this step cannot run. "
                        f"Configured: {', '.join(sorted(devices)) or 'nothing'}."))
        if action.kind not in engine_kinds and action.kind not in seen_kinds:
            seen_kinds.add(action.kind)
            if handler_for(action.kind) is None:
                problems.append(PreflightProblem(
                    code="no_handler", aid=aid, device=device,
                    message=f"{action.kind} has no registered handler, so this step cannot "
                            f"run. It is reported rather than skipped: a silently skipped "
                            f"step reads downstream as 'it ran' (R-ENG-17)."))

    refs = _waypoint_refs(actions)
    owned: list[tuple[str, str]] = []
    for device, name in refs:
        try:
            waypoints.assert_owned(device, name)
        except waypoints.WaypointNotOwned as e:
            problems.append(PreflightProblem(
                code="waypoint_not_owned", aid=refs[(device, name)], device=device,
                waypoint=name, message=str(e)))
            continue
        owned.append((device, name))

    for device, name in waypoints.missing_for_plan(owned, path=teach_path):
        problems.append(PreflightProblem(
            code="waypoint_not_taught", aid=refs[(device, name)], device=device, waypoint=name,
            message=f"waypoint ({device!r}, {name!r}) is not taught. Discovering that at "
                    f"step 7 leaves an arm holding an open tube in mid-air, so the run is "
                    f"refused now (R-ENG-16)."))

    return PreflightReport(problems=tuple(problems))


# --- the plan ----------------------------------------------------------------

class Plan:
    """An ordered list of actions with stable identity and a derived index.

    Constructed from validated `Action` models or from their JSON/dict form (which is what an
    LLM-authored injection and the HTTP API both arrive as). Actions are **copied** on the way
    in, so assigning `aid` never mutates the caller's object — which matters for a loop body,
    whose template is copied once per iteration.
    """

    def __init__(self, actions: Iterable[Any] = (), *, name: str = "") -> None:
        self.name = name
        self._lock = threading.RLock()
        self._actions: list[ActionBase] = []
        self._states: dict[int, ActionState] = {}
        self._attempts: dict[int, list[ActionResult]] = {}
        self._next_aid = 1
        #: Index of the action about to execute. Written by the runner only.
        self.cursor = 0
        for action in actions:
            self._insert(len(self._actions), [self._adopt(action)])
        # A fresh plan is revision 1: the initial load is itself a `plan_replaced` (see that
        # event's docstring), so a client that has seen revision 0 has seen nothing.
        self._revision = 1

    # --- identity and ordering ------------------------------------------------
    @property
    def revision(self) -> int:
        """Increments on every mutation. A client uses it to tell two plans apart without
        diffing derived values."""
        return self._revision

    @property
    def actions(self) -> tuple[ActionBase, ...]:
        with self._lock:
            return tuple(self._actions)

    def __len__(self) -> int:
        with self._lock:
            return len(self._actions)

    def __iter__(self) -> Iterator[ActionBase]:
        return iter(self.actions)

    def aids(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(a.aid for a in self._actions)

    def by_aid(self, aid: int) -> ActionBase:
        with self._lock:
            for action in self._actions:
                if action.aid == aid:
                    return action
        raise UnknownActionError(f"no action with aid {aid} in this plan")

    def index_of(self, aid: int) -> int:
        with self._lock:
            for i, action in enumerate(self._actions):
                if action.aid == aid:
                    return i
        raise UnknownActionError(f"no action with aid {aid} in this plan")

    def at(self, index: int) -> ActionBase | None:
        """The action at a position, or None past the end. None rather than IndexError: the
        runner's main loop asks "is there another one?" every boundary."""
        with self._lock:
            if 0 <= index < len(self._actions):
                return self._actions[index]
            return None

    # --- state and results ----------------------------------------------------
    def state(self, aid: int) -> ActionState:
        with self._lock:
            if aid not in self._states:
                raise UnknownActionError(f"no action with aid {aid} in this plan")
            return self._states[aid]

    def set_state(self, aid: int, state: ActionState) -> None:
        with self._lock:
            if aid not in self._states:
                raise UnknownActionError(f"no action with aid {aid} in this plan")
            self._states[aid] = state

    def record_result(self, result: ActionResult) -> None:
        """Store one attempt's result and set the action's state from it.

        Keyed by `aid`, appended per attempt — R-ENG-15 wants retries recorded per attempt,
        and the last one is what the row shows.
        """
        with self._lock:
            if result.aid not in self._states:
                raise UnknownActionError(f"no action with aid {result.aid} in this plan")
            self._attempts.setdefault(result.aid, []).append(result)
            self._states[result.aid] = result.status

    def result(self, aid: int) -> ActionResult | None:
        """The last recorded attempt, or None."""
        with self._lock:
            attempts = self._attempts.get(aid)
            return attempts[-1] if attempts else None

    def attempts(self, aid: int) -> tuple[ActionResult, ...]:
        with self._lock:
            return tuple(self._attempts.get(aid, ()))

    def completed(self) -> int:
        with self._lock:
            return sum(1 for s in self._states.values() if s == "complete")

    def failed(self) -> int:
        with self._lock:
            return sum(1 for s in self._states.values() if s == "failed")

    # --- mutation -------------------------------------------------------------
    def append(self, action: Any) -> ActionBase:
        """Add one action at the end. Returns the adopted copy, which carries the `aid`."""
        with self._lock:
            adopted = self._adopt(action)
            self._insert(len(self._actions), [adopted])
            self._touch()
            return adopted

    def extend(self, actions: Iterable[Any]) -> list[ActionBase]:
        with self._lock:
            adopted = [self._adopt(a) for a in actions]
            self._insert(len(self._actions), adopted)
            self._touch()
            return adopted

    def insert_after(self, after_aid: int | None, actions: Any,
                     *, origin: str = "inject") -> list[ActionBase]:
        """Insert one or more actions immediately after `after_aid` (D4/D22).

        `after_aid` of `None` or `0` means the very front. Positions are derived from
        identity, never given: an injection says "after this action", so a concurrent
        renumber cannot make it land somewhere else (R-ENG-13).

        **After a loop means after the whole loop**, materialized body included. That is the
        operator's meaning of "after the servo loop", and it keeps a loop's region contiguous
        so the runner can tell which rows belong to which iteration.

        An action landing **between two rows of the same iteration** inherits that loop's
        `parent_aid`/`iteration`: an action injected into iteration 4 is part of iteration 4,
        the UI nests it there, and the runner's "next unrun action in this loop's region"
        picks it up instead of stepping over it. Its `origin` still says `inject`, so "who
        put this here" stays answerable. Landing at the *end* of the region does not inherit —
        that is "after the loop", and it runs once the loop is done.

        Does not check the cursor — :meth:`refusal_for_insert` owns that rule, because it
        needs run state the plan does not have.
        """
        items = actions if isinstance(actions, (list, tuple)) else [actions]
        with self._lock:
            position = self._position_after(after_aid)
            adopted = [self._adopt(a) for a in items]
            for action in adopted:
                action.origin = origin  # type: ignore[assignment]
            prev = self._actions[position - 1] if position > 0 else None
            nxt = self._actions[position] if position < len(self._actions) else None
            # Inside a region means *between* two of its rows. Sitting at the region's end is
            # "after the loop", which is what `_position_after` already resolves a loop aid to.
            inside = (prev is not None and prev.parent_aid is not None
                      and nxt is not None and nxt.parent_aid == prev.parent_aid)
            if inside and prev is not None:
                for action in adopted:
                    if action.kind == "control.loop":
                        raise NestedLoopRefused(
                            f"aid {prev.parent_aid} is a loop and the insertion point is "
                            f"inside its materialized body; a loop inside a loop is an "
                            f"unbounded path to plan growth (D14)")
                    action.parent_aid = prev.parent_aid
                    action.iteration = prev.iteration
            self._insert(position, adopted)
            self._touch()
            return adopted

    def refusal_for_insert(self, after_aid: int | None, *, cursor: int | None = None,
                           running_aid: int | None = None) -> str | None:
        """Why this insertion must be refused, or None if it is allowed (D22/R-ENG-13).

        The whole rule, in one place, because getting it wrong in either direction is bad:
        too strict and the operator cannot fix a failed step (which is the only reason
        injection exists); too loose and an action lands where nothing will ever run it,
        which is a silent skip wearing an accepted request's clothes.

        * **Landing at the cursor is allowed.** Injecting after the last completed action
          puts the new action next — exactly where a failure leaves the run, and the single
          most useful place to inject (D22).
        * **A long move in flight is not a refusal.** While action #5 executes, injecting
          after #5 lands at #6 and is accepted. Refusing merely because a move is blocking is
          the behaviour D22 explicitly overturns.
        * **Before the action executing now is refused.** The runner advances past the
          running action by identity, so a row inserted in front of it would never be
          reached: accepting it would be a silent skip. Refused with that reason.
        * **The past is refused**, never relocated to the present.
        """
        with self._lock:
            if after_aid not in (None, 0):
                try:
                    self.index_of(int(after_aid))
                except UnknownActionError:
                    return (f"no action with aid {after_aid} in this plan; injection targets "
                            f"an identity, and that one does not exist")
            position = self._position_after(after_aid)
            if running_aid is not None:
                running_index = self.index_of(running_aid)
                if position <= running_index:
                    running = self._actions[running_index]
                    return (f"action #{running_index} (aid {running.aid}, {running.kind}) is "
                            f"executing now; inserting at index {position} would put the new "
                            f"action behind it, where the run would never reach it. Inject "
                            f"after aid {running.aid} to make it the next step.")
                return None
            if cursor is not None and position < cursor:
                nxt = self._actions[cursor] if cursor < len(self._actions) else None
                where = f" The next action is #{cursor}" + (
                    f" (aid {nxt.aid})." if nxt else ".")
                return (f"index {position} is in the past — that part of the plan has "
                        f"already run, and an injection is never relocated to somewhere it "
                        f"was not asked for (R-ENG-13).{where}")
            return None

    # --- loop materialization -------------------------------------------------
    def materialized_count(self) -> int:
        """Actions that exist because a loop expanded (D14's global ceiling)."""
        with self._lock:
            return sum(1 for a in self._actions if a.origin == "expand")

    def region_of(self, loop_aid: int) -> tuple[int, int]:
        """`(start, end)` positions of a loop's materialized region, `end` exclusive.

        The region is the contiguous run of actions immediately after the loop whose
        `parent_aid` is the loop. Contiguity is why `insert_after` on a loop skips past it:
        an unrelated row wedged into the middle would split the region and the runner would
        stop looking for iteration 4's second half.
        """
        with self._lock:
            start = self.index_of(loop_aid) + 1
            end = start
            while end < len(self._actions) and self._actions[end].parent_aid == loop_aid:
                end += 1
            return start, end

    def members_of(self, loop_aid: int) -> tuple[ActionBase, ...]:
        with self._lock:
            start, end = self.region_of(loop_aid)
            return tuple(self._actions[start:end])

    def next_unrun_in_region(self, loop_aid: int) -> ActionBase | None:
        """The first action in a loop's region that has not run yet.

        The runner drives a loop iteration off this rather than off a list captured at
        materialization time, so an action injected into the region is executed rather than
        stepped over.
        """
        with self._lock:
            for action in self.members_of(loop_aid):
                if self._states.get(action.aid) == "planned":
                    return action
            return None

    def materialize_iteration(self, loop_aid: int, iteration: int) -> list[ActionBase]:
        """Copy a loop's body into the flat plan as iteration `iteration` (D8).

        Every copy is a real indexed action with its own `aid`, `parent_aid` and `iteration`,
        so its outputs, artifacts and logs are attributable to *that* pass of the loop
        (R-VIS-8) and the UI can indent it.

        Raises :class:`MaterializationCapped` when a bound refuses — `max_iterations` or the
        global `MAX_MATERIALIZED_ACTIONS` ceiling. The runner reports that as the loop's
        `exhausted` outcome, which is how "this loop never converged" stays a bounded,
        readable result instead of an out-of-memory kill (D14).
        """
        with self._lock:
            loop = self.by_aid(loop_aid)
            if not isinstance(loop, Loop):
                raise PlanError(f"aid {loop_aid} is {loop.kind}, not control.loop")
            if iteration < 1:
                raise PlanError("iterations are 1-based")
            if iteration > loop.max_iterations:
                raise MaterializationCapped(
                    f"loop aid {loop_aid} asked for iteration {iteration}, over its "
                    f"max_iterations={loop.max_iterations}")
            for body_action in loop.body:
                if body_action.kind == "control.loop":
                    raise NestedLoopRefused(
                        f"loop aid {loop_aid} has a loop in its body (D14)")
            already = self.materialized_count()
            if already + len(loop.body) > MAX_MATERIALIZED_ACTIONS:
                raise MaterializationCapped(
                    f"materializing iteration {iteration} of loop aid {loop_aid} would take "
                    f"the plan to {already + len(loop.body)} expanded actions, over the "
                    f"{MAX_MATERIALIZED_ACTIONS} ceiling (D14). A loop that never converges "
                    f"must not grow the plan without bound.")
            _start, end = self.region_of(loop_aid)
            copies: list[ActionBase] = []
            for body_action in loop.body:
                copy = body_action.model_copy(deep=True)
                copy.aid = 0
                copy.parent_aid = loop_aid
                copy.iteration = iteration
                copy.origin = "expand"
                copies.append(self._adopt(copy))
            self._insert(end, copies)
            self._touch()
            return copies

    # --- projections ----------------------------------------------------------
    def row(self, action: ActionBase) -> ActionRow:
        with self._lock:
            return ActionRow(
                aid=action.aid, index=action.index, kind=action.kind, device=action.device,
                label=action.label or default_label(action), speed=action.speed,
                state=self._states.get(action.aid, "planned"),
                parent_aid=action.parent_aid, iteration=action.iteration,
                origin=action.origin, params=params_of(action),
                result=self.result(action.aid))

    def rows(self) -> list[ActionRow]:
        """Every action as the UI needs it — identity, derived index, state, result so far."""
        with self._lock:
            return [self.row(a) for a in self._actions]

    def row_payload(self) -> list[dict[str, Any]]:
        """`PlanReplaced.actions`: the rows without their results.

        Results are omitted because `action_finished` already carries the whole typed result
        (R-UI-4), and `plan_replaced` is emitted once per loop iteration — re-sending every
        completed action's outputs and artifacts on each one turns a plan mutation into the
        largest message on the socket.
        """
        return [row.model_dump(mode="json", exclude={"result"}) for row in self.rows()]

    def snapshot(self, *, seq: int = 0, run_id: str = "", state: RunState = "idle",
                 started_at: str = "", readiness: ReadinessState = "initializing",
                 warnings: Iterable[Warning_] = (),
                 simulated: dict[str, bool] | None = None) -> RunSnapshot:
        """The full-state projection a reconnecting client rebuilds everything from (D24).

        Plan, per-action state, every result recorded so far, readiness, and which devices
        are simulated. The run-level facts are arguments rather than plan state because the
        plan does not own them — the runner does, and passing them in keeps one source of
        truth for each.
        """
        with self._lock:
            return RunSnapshot(
                seq=seq, run_id=run_id, name=self.name, state=state,
                revision=self._revision, cursor=self.cursor, started_at=started_at,
                actions=self.rows(), readiness=readiness, warnings=list(warnings),
                simulated=dict(simulated or {}))

    # --- pre-flight -----------------------------------------------------------
    def preflight(self, *, known_devices: Collection[str] | None = None,
                  teach_path: str | None = None) -> PreflightReport:
        return preflight(self.actions, known_devices=known_devices, teach_path=teach_path)

    def waypoint_pairs(self) -> list[tuple[str, str]]:
        return waypoint_pairs(self.actions)

    # --- internals ------------------------------------------------------------
    def _adopt(self, action: Any) -> ActionBase:
        """Validate, copy, and assign a fresh immutable `aid`.

        Copying is what keeps `aid` assignment from reaching back into the caller's object —
        without it, materializing a loop body would stamp an aid onto the template and the
        second iteration would carry the first's identity.
        """
        if isinstance(action, ActionBase):
            adopted = action.model_copy(deep=True)
        else:
            adopted = validate_action(action)
        adopted.aid = self._next_aid
        self._next_aid += 1
        if not adopted.label:
            adopted.label = default_label(adopted)
        return adopted

    def _insert(self, position: int, actions: Sequence[ActionBase]) -> None:
        self._actions[position:position] = list(actions)
        for action in actions:
            self._states.setdefault(action.aid, "planned")
        self._reindex()

    def _reindex(self) -> None:
        """Recompute every `index`. Identity is untouched; results keep their `aid` key.

        Recorded results carry `index` as well (it is in the log and on the wire), so they
        are refreshed here too — a stale index on a completed action's result is a row the UI
        renders in the wrong place while every other field agrees.
        """
        for i, action in enumerate(self._actions):
            action.index = i
            for result in self._attempts.get(action.aid, ()):
                result.index = i

    def _position_after(self, after_aid: int | None) -> int:
        if after_aid in (None, 0):
            return 0
        index = self.index_of(int(after_aid))  # type: ignore[arg-type]
        action = self._actions[index]
        if isinstance(action, Loop):
            return self.region_of(action.aid)[1]
        return index + 1

    def _touch(self) -> None:
        self._revision += 1

    def __repr__(self) -> str:
        return (f"<Plan {self.name!r} n={len(self)} rev={self._revision} "
                f"cursor={self.cursor}>")


__all__ = [
    "ENGINE_KINDS", "InjectRefused", "MaterializationCapped", "NestedLoopRefused", "Plan",
    "PlanError", "PreflightProblem", "PreflightReport", "UnknownActionError",
    "default_label", "params_of", "preflight", "waypoint_pairs",
]
