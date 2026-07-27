"""The action model — **FROZEN at the end of Wave 0**. Types only; no behaviour lives here.

Seven parallel slices import this file. Nothing in it executes: the `Action` union is plan
data, `Outputs` is what a handler returns, `ActionResult` is what the runner builds around
that return value, and `@handler` is a registry decorator. Plan mutation is S1's `plan.py`,
dispatch is S1's `runner.py`, and the handlers themselves belong to S2/S3/S4/S5.

An `Action` has to be five things at once, and the shape follows from the combination:

* a plan element with a **stable identity** and a **derived display index** (R-ENG-4) —
  hence `aid` immutable and `index` recomputed on every mutation, never persisted as truth;
* JSON-serializable both ways, because it is streamed to the browser, written to the log,
  and **constructed by an LLM** (R-UI-7);
* validated *before* it can move a robot — a bad `dz` must be a 422, not a crashed move;
* dispatchable to a blocking handler (D1);
* able to reference an earlier action's output **without a Python closure**, because an
  injected action is data, not code.

The last point is where this diverges from the draft in `ARCHITECTURE.md §2.2`. That draft
had a `Ref(slot, field)` with "an optional dotted path inside the slot value". The review cut
it (S2): a dotted-path resolver is an interpreter you end up debugging during bring-up, at
the bench, at the worst possible time. Instead there are exactly **five named slots**
(`SlotName`), an action names one directly, and the runner does one dict lookup. That is
still declarative and still LLM-authorable — it is just not a language.

Three more places where the locked decisions overrule the draft:

* **14 kinds, not 21** (S4). Initialize-all and initialize-one are one kind with an optional
  device; `arm.home` is gone because HOME is a waypoint name (R-WP-4); identify-tip and
  identify-tube are one kind with a `target`; overlay rendering folds into the offset action;
  there is **no manual-move action** — free-drive is a teach-tab control wearing a plan
  action's clothes, and a plan that can hand-guide an arm is a plan that can disable
  position control mid-run. `vision.select_offset` is gone with D15: the "best single view"
  strategy guarantees a stalled loop.
* **No condition language** (S3). `control.loop` has one named termination predicate and its
  thresholds. It is also the part an LLM is most likely to author wrongly, and a wrongly
  authored *expression* fails in a way nobody can read.
* **The offset metric is reworked** (D16). `residual_offset_mm` is what terminates the loop,
  per-axis `sigma_mm` is the reported uncertainty, and `view_disagreement_mm` is advisory
  only. There is deliberately **no per-view `deviation` field**: per view the residual is
  structurally zero (two equations, two unknowns after axis restriction), so the old metric
  could never report a problem and its confidence factor was identically 1.0.
"""
from __future__ import annotations

from typing import Annotated, Any, Callable, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

# --- shared vocabulary -------------------------------------------------------

# Resolved to numbers by `core/speeds.py`, per device class, clamped by that device's soft
# limits. R-ENG-14: raw speed numbers must not be expressible in plan data, which is why
# there is no `mm_per_s` field anywhere below.
SpeedTier = Literal["slow", "medium", "fast"]

FailurePolicy = Literal["halt", "continue", "retry"]

# The blackboard's five slots (S2). A closed `Literal`, so a plan naming a sixth slot is a
# validation error at authoring time rather than a `KeyError` at the bench. See
# `blackboard.py` for what each one holds.
SlotName = Literal["frame", "tip", "tube", "offset", "selected_offset"]

ActionState = Literal["planned", "running", "complete", "failed", "skipped", "aborted"]

# D14: bound loop materialization. A servo loop that never converges must not be able to
# grow the plan without limit — nested loop plus an unbounded `max_iterations` is a
# quadratic path to OOM (review B5). The engine refuses to materialize past this; the cap
# lives here because it is part of the contract S1 implements and S10 authors against.
MAX_MATERIALIZED_ACTIONS = 500
# Per-loop iteration ceiling, independent of the total. A plan may ask for fewer.
MAX_LOOP_ITERATIONS = 40


class ActionBase(BaseModel):
    """Fields every action carries.

    `extra="forbid"` is load-bearing for the LLM path: a proposal with a misspelled field
    must be rejected, not silently accepted with the field ignored — an ignored `dz` is a
    move to the wrong place that reports success. `allow_inf_nan=False` keeps the win
    `schemas.FiniteModel` already earned: a NaN offset must be a 422, never a commanded move.
    """
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    aid: int = 0
    """Immutable stable identity, unique within a run. The UI keys rows on this and the log
    attributes records to it. Injection renumbers `index`; it must never change an `aid`,
    because that is what would lose a completed action's recorded outputs (R-ENG-4).
    0 means "not yet assigned" — the plan assigns it."""

    index: int = 0
    """Display index. **Derived**, recomputed on every plan mutation. Never a target: an
    injection says "after <aid>", never "at index N" (D4)."""

    label: str = ""
    """Human text for the plan row. Auto-filled from the kind when blank."""

    device: str | None = None
    """Fleet id. `None` for pure-computation actions — and note D25: `None` does **not** mean
    "real", it means "no device", and in a simulated run the result still reports
    `simulated=True` because the pixels it read were simulated."""

    speed: SpeedTier = "medium"
    on_failure: FailurePolicy = "halt"
    max_attempts: int = Field(default=1, ge=1, le=5)
    """Bounded and recorded per attempt (R-ENG-15). Not unbounded: a retry loop around a
    move that fails for a structural reason is a machine repeating a mistake."""

    parent_aid: int | None = None
    """Set on actions materialized from a loop body, so the UI can nest iteration 4's rows
    under the loop and R-VIS-8 ("what the loop saw on iteration 4") is answerable."""
    iteration: int | None = None
    """1-based, for loop-materialized actions."""

    origin: Literal["plan", "inject", "expand"] = "plan"
    """Where this action came from. `inject` is an operator/LLM addition, `expand` is a loop
    materialization. Shown in the UI because "who put this here" is the first question when
    a run does something unexpected."""

    note: str = ""


# --- lifecycle (2 kinds) -----------------------------------------------------

class Initialize(ActionBase):
    """Initialize every device, or one named device.

    One kind rather than two (S4). D5 makes initialization a plan run on this same engine, so
    "reinitialize device X" is *literally* the boot code with `device` set, rather than a
    parallel imperative path that can drift from it (R-INIT-7).
    """
    kind: Literal["lifecycle.initialize"] = "lifecycle.initialize"
    device: str | None = None
    """`None` = every configured device. One device failing must not stop the others
    (R-INIT-1)."""
    home_after: bool = True
    """Arms move to their taught HOME afterwards, slowly (R-INIT-3). An arm with no HOME
    taught produces a **warning** and is skipped — never a guessed home move (R-INIT-4)."""


class Reconnect(ActionBase):
    """Recovery: re-connect, re-enable, or re-engage (R-ARM-7).

    One kind with a scope, because on an xArm these are three calls in a fixed order and
    doing one without the others leaves the arm in a state nothing else expects. `engage` is
    Q5's answer: `clear_errors()` -> `enable(True)` -> verify with a zero-distance move.
    """
    kind: Literal["lifecycle.reconnect"] = "lifecycle.reconnect"
    device: str
    scope: Literal["connect", "enable", "engage"] = "engage"


# --- arm (5 kinds) -----------------------------------------------------------

class ArmWaypoint(ActionBase):
    """Move to a named waypoint, with optional offsets (R-ARM-2).

    **This is also the home move.** There is no `arm.home` kind: `HOME` is an ordinary
    waypoint name that exists once per arm and means a different pose on each (R-WP-4). A
    separate kind would have needed its own pre-flight, its own missing-waypoint path and its
    own speed default, all duplicating this one.

    `waypoint` is an **opaque label scoped to the device** — no code parses it, so renaming
    one is a data edit. Per R-WP-2 a move naming a waypoint the acting device does not own is
    **refused at pre-flight**, with a message naming both the owner and the actor; it must
    never fall back to a same-named waypoint on another arm or resolve by search order.
    """
    kind: Literal["arm.waypoint"] = "arm.waypoint"
    device: str
    waypoint: str = Field(min_length=1, max_length=64)
    dx: float = 0.0
    dy: float = 0.0
    dz: float = 0.0
    """mm, TCP frame. "with x/y/z offsets, default 0" — R-ARM-2."""


class ArmRelative(ActionBase):
    """Move by an offset (R-ARM-3)."""
    kind: Literal["arm.move_relative"] = "arm.move_relative"
    device: str
    dx: float = 0.0
    dy: float = 0.0
    dz: float = 0.0
    droll: float = 0.0
    dpitch: float = 0.0
    dyaw: float = 0.0
    from_slot: SlotName | None = None
    """When set, dx/dy/dz come from that blackboard slot instead of the literal fields. Named
    directly rather than through a `Ref` with a dotted path (S2): five slots, one lookup, no
    interpreter."""


class ArmGripper(ActionBase):
    """Close or open the gripper (R-ARM-4)."""
    kind: Literal["arm.gripper"] = "arm.gripper"
    device: str
    state: Literal["open", "close"]
    width: float | None = None
    """Gripper units (counts). `None` = fully. Width-capable grippers only; the driver
    refuses an out-of-range width rather than clamping it."""


class ArmDecap(ActionBase):
    """Unscrew a cap: a full turn in `step_deg` bites, rewinding the wrist between them.

    R-ARM-5 / D12. `core/motion/cap_ops.py` already emits the turn/open/unwind/close ratchet
    in 180 deg bites; 90 deg is a generalization of the same plan, and it makes the pre-flight
    *easier* because peak wrist excursion halves. Two properties must survive: net wrist
    travel is zero, and the whole sequence is pre-flighted against the joint soft limits
    **before the first step**, because the intermediate angles exceed the endpoints.

    Rotation is commanded in **joint space on the tool axis**, never as a cartesian yaw.
    """
    kind: Literal["arm.decap"] = "arm.decap"
    device: str
    step_deg: float = Field(default=90.0, gt=0.0, le=180.0)
    turns: float = Field(default=1.0, gt=0.0, le=4.0)
    grip_counts: float | None = None
    speed: SpeedTier = "slow"


class ArmTraverse(ActionBase):
    """Move through an ordered set of named waypoints as one action (R-ARM-6).

    Blended where the controller supports a radius, reporting progress per waypoint. Every
    waypoint is checked against the joint soft limits before the first move: stopping halfway
    along a travel path leaves the arm somewhere nobody chose — over the deck, or between the
    two instruments.
    """
    kind: Literal["arm.traverse"] = "arm.traverse"
    device: str
    waypoints: list[str] = Field(min_length=2)
    blend_deg: float | None = Field(default=5.0, ge=0.0, le=45.0)
    """Corner radius in degrees, clamped server-side to the shortest segment — the controller
    rejects a radius longer than the track. `None` = point-to-point."""


# --- liquid handler (1 kind) -------------------------------------------------

class LHRelative(ActionBase):
    """Move the pipette head by x/y/z in mm (R-LH-1).

    The single most load-bearing liquid-handler capability, because it is what the servo loop
    drives, and what makes the simulated loop converge *because the moves close the offset*
    (D3/R-SIM-5). Relative **semantics** at this interface; the driver implements it as
    read -> clamp -> absolute command where a readback exists, since the servo loop needs
    relative semantics and never relative commands.

    Also R-VIS-11's final step: `dz` positive, retracting up after the loop converges. There
    is no separate retract-Z action kind — full retract belongs to `lifecycle.initialize`
    (R-LH-2), and "move up by N mm" is this.

    An out-of-envelope request is **refused, never silently clamped** (R-LH-3).
    """
    kind: Literal["lh.move_relative"] = "lh.move_relative"
    device: str
    dx: float = 0.0
    dy: float = 0.0
    dz: float = 0.0
    from_slot: SlotName | None = None
    clamp_mm: float = Field(default=15.0, gt=0.0)
    """Per-iteration move clamp (R-VIS-7/O9). A vision-derived step larger than this is a
    detection failure presented as a command, so it is refused rather than executed."""


# --- cameras (2 kinds) -------------------------------------------------------

class CameraSnapshot(ActionBase):
    """Capture and store one frame from a named camera (R-CAM-1).

    `fresh` enforces R-CAM-2, and D20 is why it cannot be a sequence number: no `seq` crosses
    the MJPEG wire. The child answers an explicit request/response snapshot carrying its own
    **capture timestamp**, and drains the capture buffer first, because `capture()` is a bare
    `read()` that can return a queued older frame. The engine fails a servo iteration whose
    frame predates the last liquid-handler move — a cached pre-move frame is exactly what
    makes a servo loop never converge.
    """
    kind: Literal["camera.snapshot"] = "camera.snapshot"
    device: str
    fresh: bool = True
    store: bool = True
    """Write it under the run's artifact directory and emit an `Artifact`."""
    into_slot: SlotName = "frame"


class CameraSearchCode(ActionBase):
    """Watch a camera for a marker id until it appears or a timeout expires (R-CAM-3).

    "Simplest possible marker-search action — one marker id, one timeout" (review S8).
    """
    kind: Literal["camera.search_code"] = "camera.search_code"
    device: str
    marker_id: int = Field(ge=0)
    timeout_s: float = Field(default=15.0, gt=0.0, le=120.0)


# --- vision (2 kinds) --------------------------------------------------------

class VisionIdentify(ActionBase):
    """Locate the tip bottom or the tube top in one camera's frame (R-VIS-1/2).

    One kind with a `target` rather than two near-identical kinds (S4): the two detectors
    have the same contract — a point, a quality score, which path produced it, and an honest
    report of **absence** rather than a low-confidence guess.

    D27 makes this fiducial-first: a flat 40-50 mm tag on the pipette carriage and another on
    the tube/gripper assembly are the **primary** signal, with the classical detectors as a
    fallback. Both ship, because the jaws occlude the tag mid-approach — which is the worst
    possible moment to drop onto an untested path, so `method` is reported and the log shows
    when the fallback carried a run.
    """
    kind: Literal["vision.identify"] = "vision.identify"
    device: str
    target: Literal["tip", "tube"]
    from_slot: SlotName = "frame"
    into_slot: SlotName | None = None
    """Defaults to the slot matching `target` (`tip` or `tube`)."""


class VisionSolveOffset(ActionBase):
    """Solve the tip-bottom -> tube-top offset, and render the overlay (R-VIS-3/5).

    Overlay rendering is folded in rather than being its own kind (S4): it has exactly the
    same inputs, it is never wanted without the solve, and a separate action could be omitted
    from a plan — leaving a converged loop with no visual record of why it moved.

    The primary path is **O1**: direct 3D from two tag poses by vector subtraction in one
    camera's frame. No image jacobian on the primary path (R-VIS-12). With one tag visible it
    degrades to **O3**, axis-decoupled jacobian control, with damped least squares (O5) and
    online gain adaptation (O6). Always on: 4-corner and multi-frame averaging (O4),
    conditioning **refusal** on an ill-conditioned solve (O7/D17), per-axis `sigma_mm` from
    the solve covariance (O8), and the clamp plus no-progress abort (O9).

    There is no `select_offset` companion (D15): one weighted solve, and an explicit
    `low_observability` refusal. A "best single view" fallback guarantees a stalled loop.
    """
    kind: Literal["vision.solve_offset"] = "vision.solve_offset"
    device: str | None = None
    """`None` = solve across every configured servo camera. A camera id restricts it to one
    view. Which cameras those are comes from config, never hardcoded (D19)."""
    tip_slot: SlotName = "tip"
    tube_slot: SlotName = "tube"
    into_slot: SlotName = "selected_offset"
    render_overlay: bool = True


# --- control (2 kinds) -------------------------------------------------------

class Loop(ActionBase):
    """A bounded loop whose every iteration materializes into the flat plan (D8).

    Materializing is the only way R-VIS-8 and R-UI-2..5 can show "what the loop saw on
    iteration 4" — so each iteration becomes real indexed actions with their own outputs,
    artifacts and logs, carrying `parent_aid` and `iteration`.

    It is bounded three ways, because D14 made that a condition of D8: `max_iterations`,
    the global `MAX_MATERIALIZED_ACTIONS` ceiling, and **no nested loops** (validated below).
    A never-converging loop must not be able to grow the plan without limit.

    `until` is a **named predicate**, not an expression (S3). One test, its threshold spelled
    out. R-VIS-9 and Q4 fix which test: terminate on the **remaining offset**, never on
    inter-view disagreement — gating on disagreement can leave a perfectly converged loop
    running forever because the views never agree that closely.
    """
    kind: Literal["control.loop"] = "control.loop"
    device: None = None
    body: list["Action"] = Field(min_length=1)
    until: Literal["offset_within_threshold"] = "offset_within_threshold"
    threshold_mm: float = Field(default=1.5, gt=0.0)
    """The remaining offset at which the loop is converged (D16/Q4)."""
    watch_slot: SlotName = "selected_offset"
    corrected_axes: tuple[str, ...] = ("x", "y", "z")
    """Which axes this loop is responsible for closing, and therefore which must be OBSERVED
    before it may claim convergence (R-VIS-4).

    All three by default, which is the safe reading: a solve that saw x and y and reported a
    partial magnitude must not terminate a loop whose z was never measured. Narrow it only when
    the rig genuinely cannot close an axis and the plan means to say so — a single side-on view
    observes one in-plane axis, because the other runs along the camera's optical axis
    (measured on this bench: 2.07 px/mm against 0.06). Declaring that in the plan is honest;
    inferring it from whatever happened to be visible is not, because then an occluded tag
    silently shrinks the goal and the loop reports success for reaching it."""
    max_iterations: int = Field(default=12, ge=1, le=MAX_LOOP_ITERATIONS)
    no_progress_abort: int = Field(default=3, ge=1)
    """Consecutive iterations without improvement before the loop aborts as `stalled`. The
    third of R-VIS-7's three honest outcomes — converged, stalled, aborted; never spin."""

    @field_validator("body")
    @classmethod
    def _no_nested_loops(cls, body: list[Any]) -> list[Any]:
        """D14. A loop inside a loop multiplies materialized actions, and the review traced
        that to OOM (B5). Refused at validation rather than capped at runtime, so an
        LLM-authored plan is rejected with a reason instead of silently truncated."""
        for action in body:
            kind = getattr(action, "kind", None) or (
                action.get("kind") if isinstance(action, dict) else None)
            if kind == "control.loop":
                raise ValueError("nested loops are not allowed (D14): a loop inside a loop "
                                 "is an unbounded path to plan growth")
        return body

    @model_validator(mode="after")
    def _bounded_materialization(self) -> "Loop":
        planned = len(self.body) * self.max_iterations
        if planned > MAX_MATERIALIZED_ACTIONS:
            raise ValueError(
                f"{len(self.body)} body actions x {self.max_iterations} iterations = "
                f"{planned} materialized actions, over the {MAX_MATERIALIZED_ACTIONS} cap "
                f"(D14). Reduce max_iterations or the body.")
        return self


class Checkpoint(ActionBase):
    """An explicit pause point authored into the plan.

    Distinct from the pause *button*: this is the plan saying "stop here and let the operator
    look" — before the first real motion of a run, say. Pause remains cooperative and at
    action boundaries (D9); this is simply a boundary the plan chose.
    """
    kind: Literal["control.checkpoint"] = "control.checkpoint"
    device: None = None
    message: str = ""


# --- the union ---------------------------------------------------------------

ACTION_MODELS: tuple[type[ActionBase], ...] = (
    Initialize, Reconnect,
    ArmWaypoint, ArmRelative, ArmGripper, ArmDecap, ArmTraverse,
    LHRelative,
    CameraSnapshot, CameraSearchCode,
    VisionIdentify, VisionSolveOffset,
    Loop, Checkpoint,
)

Action = Annotated[
    Union[
        Initialize, Reconnect,
        ArmWaypoint, ArmRelative, ArmGripper, ArmDecap, ArmTraverse,
        LHRelative,
        CameraSnapshot, CameraSearchCode,
        VisionIdentify, VisionSolveOffset,
        Loop, Checkpoint,
    ],
    Field(discriminator="kind"),
]

Loop.model_rebuild()

#: Every kind string, in plan order. The frontend generates its per-kind renderers from this
#: and the JSON schema; do not hand-write that list on the TypeScript side.
ACTION_KINDS: tuple[str, ...] = tuple(
    m.model_fields["kind"].default for m in ACTION_MODELS      # type: ignore[misc]
)

_action_adapter: TypeAdapter[Any] = TypeAdapter(Action)


def validate_action(data: Any) -> ActionBase:
    """Parse one action from JSON/dict form, raising `ValidationError` on anything invalid.

    This is the LLM-injection gate (R-UI-7/14) and the API's request parser. A misspelled
    field, a NaN offset, a nested loop or an unknown kind all fail here — before anything can
    move — which is the whole reason the plan is a discriminated union rather than
    `kind: str` + `params: dict`.
    """
    return _action_adapter.validate_python(data)


# --- typed outputs, one model per kind ---------------------------------------

class OutputsBase(BaseModel):
    """Base for every action's typed outputs.

    ``allow_inf_nan=False`` matches ``ActionBase`` and is load-bearing on the way *out*, not
    just the way in. Outputs are serialized to the websocket, and Python's ``json`` emits bare
    ``Infinity`` / ``NaN`` — which are not JSON, and which ``JSON.parse`` rejects outright. One
    non-finite float would therefore break the event stream for every connected client, not
    just mis-render one row.

    This is not hypothetical: ``condition_number`` is the natural output of an SVD on a
    singular matrix, and ``inf`` is exactly what a degenerate camera geometry produces. The
    contract for handlers is therefore: **a value you cannot compute is ``None``, with a stated
    reason** — never an infinity. ``OffsetOutputs`` makes every such field optional for this
    reason, and carries ``refusal`` to say why.

    Failing here is loud and local (the action fails validation) rather than silent and global
    (invalid JSON on a shared socket), which is the trade worth making.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class InitializeOutputs(OutputsBase):
    """Per-device outcomes. One device failing does not stop the others (R-INIT-1), so this
    is a list of results rather than a single verdict."""
    kind: Literal["initialize"] = "initialize"
    devices: list[dict[str, Any]] = Field(default_factory=list)
    """`[{"device", "connected", "detail", "simulated"}]`."""
    homed: list[str] = Field(default_factory=list)
    home_missing: list[str] = Field(default_factory=list)
    """Arms skipped because no HOME was taught. Paired with a `home_not_defined` warning, so
    it reaches the readiness panel and the log, not a terminal (R-INIT-4/R-LOG-6)."""


class ReconnectOutputs(OutputsBase):
    kind: Literal["reconnect"] = "reconnect"
    scope: Literal["connect", "enable", "engage"]
    connected: bool = False
    enabled: bool = False
    verified: bool = False
    """`engage` verifies with a zero-distance move (Q5). Reported separately, because
    "enabled" and "actually accepts a move" are not the same claim."""
    cleared_errors: list[str] = Field(default_factory=list)


class MoveOutputs(OutputsBase):
    """Any arm move: waypoint, relative, or the last leg of a traverse."""
    kind: Literal["move"] = "move"
    pose_before: list[float] = Field(default_factory=list)   # x y z roll pitch yaw
    pose_after: list[float] = Field(default_factory=list)
    joints_before: list[float] = Field(default_factory=list)
    joints_after: list[float] = Field(default_factory=list)
    waypoint: str | None = None
    offsets_mm: dict[str, float] = Field(default_factory=dict)
    resolved_speed: dict[str, Any] = Field(default_factory=dict)
    """From `core.speeds.Speeds.as_dict()`: tier, linear, angular, clamped. The log records
    what was **commanded**, not what was asked for — the only version worth having after."""
    path: Literal["joint_replay", "joint_replay+cartesian_offset", "cartesian",
                  "relative", ""] = ""
    """Which motion path was taken, because the three are not equally trustworthy.

    `joint_replay` reproduces a configuration the arm physically reached, so no IK branch was
    guessed at. `cartesian` solved IK and could in principle have picked a different
    configuration. `joint_replay+cartesian_offset` replays the taught joints and then moves
    the requested offset — only those millimetres went through IK.

    Worth a field of its own rather than a note: when a move ends up somewhere unexpected,
    which path produced it is the first question, and four of this workflow's moves are long
    enough (298-430 mm) that the answer decides whether to trust the pose at all."""


class GripperOutputs(OutputsBase):
    kind: Literal["gripper"] = "gripper"
    state: Literal["open", "close"]
    width_before: float | None = None
    width_after: float | None = None


class DecapOutputs(OutputsBase):
    kind: Literal["decap"] = "decap"
    bites: int = 0
    step_deg: float = 0.0
    total_rotation_deg: float = 0.0
    net_wrist_travel_deg: float = 0.0
    """Must be ~0. Reported rather than asserted silently, so a ratchet that drifted shows up
    in the record instead of in the cabling."""
    preflight_ok: bool = False
    wrist_rewound_before_decap: bool = False
    """Whether the wrist had to be unwound before the ratchet could start.

    The bench found J6 already wound far enough that a fresh 360° would have reached 484°, so
    the handler unwinds first. That is a real change to what the arm did and belongs in the
    record, not only in a warning — a decap that needed a rewind started from a different
    wrist configuration than one that did not."""
    rewind_deg: float = 0.0
    """How far the wrist was unwound before starting. 0 when no rewind was needed."""
    ended_gripped: bool = True
    """Whether the jaws finished **closed on the cap**.

    The next authored action lifts the cap clear of the tube, and it cannot do that with open
    jaws — the bench saw exactly that: "the gripper opened first, so it didn't hold on to the
    decapped cap". Whether the arm still has the cap therefore decides what its next move may
    safely be, which makes it a fact the record has to carry rather than one a reader infers
    from configuration."""


class TraverseOutputs(OutputsBase):
    kind: Literal["traverse"] = "traverse"
    reached: list[str] = Field(default_factory=list)
    blend_deg: float | None = None
    """The radius actually used after the shortest-segment clamp, not the one requested."""
    resolved_speed: dict[str, Any] = Field(default_factory=dict)


class LHMoveOutputs(OutputsBase):
    kind: Literal["lh_move"] = "lh_move"
    requested_mm: dict[str, float] = Field(default_factory=dict)
    applied_mm: dict[str, float] = Field(default_factory=dict)
    position_before: dict[str, float] = Field(default_factory=dict)
    position_after: dict[str, float] = Field(default_factory=dict)
    provenance: Literal["measured", "dead_reckoned"] = "dead_reckoned"
    """R-LH-4: how the driver knows where it is. A dead-reckoned position must not be
    mistakable for a reading — the servo loop's gain depends on which it is."""
    drift_mm: float | None = None


class SnapshotOutputs(OutputsBase):
    kind: Literal["snapshot"] = "snapshot"
    width: int = 0
    height: int = 0
    captured_at: str = ""
    """The **child-side** capture timestamp, ISO8601 UTC (D20). Reported in the action's
    outputs per R-CAM-2, because "the frame is fresh" is a claim that has to be checkable."""
    stale: bool = False
    """True when the capture time predates the motion that preceded it. A servo iteration
    fails on this rather than solving against a pre-move frame."""
    achieved_mode: str = ""
    """`WxH@fps` as read back off the device. OpenCV silently substitutes the nearest mode
    and RealSense rejects outright (R-CAM-12), so the requested mode is not evidence."""
    stream: Literal["color", "ir", "unknown"] = "unknown"
    """Detected, not assumed. A slot delivering greyscale IR when colour was requested is a
    warning naming the slot (R-CAM-15)."""


class SearchCodeOutputs(OutputsBase):
    kind: Literal["search_code"] = "search_code"
    marker_id: int
    found: bool = False
    center_px: list[float] | None = None
    elapsed_s: float = 0.0
    """Reported whether or not it was found: a search that timed out at 0.2 s means something
    different from one that timed out at 15 s."""


class IdentifyOutputs(OutputsBase):
    """Tip-bottom or tube-top in image coordinates (R-VIS-1/2)."""
    kind: Literal["identify"] = "identify"
    target: Literal["tip", "tube"]
    found: bool = False
    """R-VIS-1/2's honesty requirement: absence is reported as absence. A low-confidence
    guess presented as a detection is the failure these requirements name."""
    point_px: list[float] | None = None
    score: float = 0.0
    method: Literal["tag_anchored", "classical", "none"] = "none"
    """Which path produced it (D27). The jaws occlude the tag mid-approach, so the log has to
    show when the fallback carried a run."""
    marker_id: int | None = None
    T_cam_feature: list[list[float]] | None = None
    """4x4, metres, camera<-feature. Present on the tag-anchored path, which is what makes
    the primary offset solve a vector subtraction rather than a jacobian (R-VIS-12)."""


class OffsetOutputs(OutputsBase):
    """The reworked offset metric (D16). Read the field comments before using any of them.

    Three numbers that are **not** interchangeable, and were conflated in the draft:
    `residual_offset_mm` is how far there is left to go and is what terminates the loop;
    `sigma_mm` is how well that is known; `view_disagreement_mm` is advisory. There is no
    per-view `deviation` field, because per view it is structurally zero.
    """
    kind: Literal["offset"] = "offset"
    residual_offset_mm: dict[str, float | None] = Field(default_factory=dict)
    """`{"x", "y", "z"}` in mm — the **remaining** offset tip-bottom -> tube-top. This is the
    convergence criterion (R-VIS-9, Q4/D16). An axis that is not observable is `None`, never
    0.0: those mean opposite things to a servo loop."""
    magnitude_mm: float | None = None
    """Over the observed axes only."""
    sigma_mm: dict[str, float] = Field(default_factory=dict)
    """Per-axis uncertainty from the solve covariance `(JᵀWJ)⁻¹` (O8/R-VIS-15). This is the
    reported confidence. It **degrades** when an axis is poorly observed or a detection is
    missing, which is R-VIS-4's requirement that the metric cannot be structurally incapable
    of reporting a problem."""
    view_disagreement_mm: float | None = None
    """Advisory only (Q4). Never a termination gate: views that never agree to within the
    threshold would keep a converged loop running forever."""
    observed_axes: list[str] = Field(default_factory=list)
    method: Literal["tag_3d", "axis_decoupled_jacobian", "refused"] = "refused"
    """O1 primary, O3 degraded, or a refusal."""
    refusal: str = ""
    """Why the solve refused — e.g. `low_observability`, `ill_conditioned`,
    `identity_mismatch`, `stale_frame`. A refusal is an outcome, not an error: D17/D18 make
    an ill-conditioned geometry and a camera identity/resolution mismatch **refusals**, not
    warnings, because a 45-degree-yawed camera at condition ~76 turns 3 px of detection error
    into ~20 mm of commanded motion with nothing reported."""
    singular_values: list[float] = Field(default_factory=list)
    """Logged so the conditioning decision is auditable after the fact (D17)."""
    condition_number: float | None = None
    contributions: list[dict[str, Any]] = Field(default_factory=list)
    """Per-camera weight and residual. One weighted solve, no best-single fallback (D15)."""


class LoopOutputs(OutputsBase):
    kind: Literal["loop"] = "loop"
    outcome: Literal["converged", "stalled", "aborted", "exhausted"] = "aborted"
    """R-VIS-7's honest outcomes. `exhausted` is distinct from `stalled`: hitting
    `max_iterations` while still improving is not the same as stopping making progress."""
    iterations: int = 0
    materialized: int = 0
    final_magnitude_mm: float | None = None
    threshold_mm: float = 0.0


class CheckpointOutputs(OutputsBase):
    kind: Literal["checkpoint"] = "checkpoint"
    acknowledged: bool = False
    message: str = ""


Outputs = Annotated[
    Union[
        InitializeOutputs, ReconnectOutputs,
        MoveOutputs, GripperOutputs, DecapOutputs, TraverseOutputs,
        LHMoveOutputs,
        SnapshotOutputs, SearchCodeOutputs,
        IdentifyOutputs, OffsetOutputs,
        LoopOutputs, CheckpointOutputs,
    ],
    Field(discriminator="kind"),
]

#: Which outputs model each action kind returns. The runner uses it to validate a handler's
#: return value, and the frontend to pick a renderer. Exhaustive over `ACTION_KINDS` — a test
#: asserts that, because a kind with no entry would produce untyped outputs and no UI.
OUTPUTS_FOR_KIND: dict[str, type[OutputsBase]] = {
    "lifecycle.initialize": InitializeOutputs,
    "lifecycle.reconnect": ReconnectOutputs,
    "arm.waypoint": MoveOutputs,
    "arm.move_relative": MoveOutputs,
    "arm.gripper": GripperOutputs,
    "arm.decap": DecapOutputs,
    "arm.traverse": TraverseOutputs,
    "lh.move_relative": LHMoveOutputs,
    "camera.snapshot": SnapshotOutputs,
    "camera.search_code": SearchCodeOutputs,
    "vision.identify": IdentifyOutputs,
    "vision.solve_offset": OffsetOutputs,
    "control.loop": LoopOutputs,
    "control.checkpoint": CheckpointOutputs,
}


# --- the result ---------------------------------------------------------------

class Artifact(BaseModel):
    """A file an action produced, referenced by path — never inlined into a record (R-LOG-8).

    Deliberately no hash, size or dimensions (review S7): nothing consumes them, and metadata
    that nothing reads is metadata nobody notices going stale.
    """
    model_config = ConfigDict(extra="forbid")
    kind: Literal["image/frame", "image/overlay", "json"]
    path: str
    """Repo-relative, under `settings.artifact_dir`."""
    url: str = ""
    camera: str | None = None
    label: str = ""


class ErrorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    message: str
    retriable: bool = False
    device: str | None = None


class Warning_(BaseModel):
    """An operational warning, reachable programmatically rather than printed (R-LOG-6).

    `code` is machine-readable on purpose: R-INIT-4's undefined home must drive the readiness
    panel, and matching on message text is how that quietly stops working.
    """
    model_config = ConfigDict(extra="forbid")
    code: str
    message: str
    device: str | None = None


class LogRef(BaseModel):
    """How the frontend fetches this action's log records.

    A **query**, not a byte range: the log file rotates, and an offset into a rotated file is
    a wrong answer presented confidently. Resolves to
    `GET /api/logs?run_id=<run_id>&aid=<aid>`.
    """
    model_config = ConfigDict(extra="forbid")
    run_id: str
    aid: int


class ActionResult(BaseModel):
    """Everything known about one attempt at one action.

    Built by the runner, never by a handler — R-LOG-5 makes the wrapper responsible for
    logging so a handler *cannot* forget, and the same applies to timing, artifacts and
    events. A handler returns `Outputs` and nothing else.
    """
    model_config = ConfigDict(extra="forbid")

    aid: int
    index: int
    kind: str
    device: str | None = None
    status: Literal["complete", "failed", "skipped", "aborted"]
    attempt: int = 1

    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0

    simulated: bool = False
    """**D25.** Derived from the resolved simulation configuration
    (`settings.is_simulated(device)`), never from a driver's vendor string or metadata. Two
    reasons the vendor-string approach was wrong, both in the dangerous direction: real
    drivers have no `live` key (reading it raises), and a pure-computation action touches no
    device at all — so a computed offset in a fully simulated run would have reported
    "real". With simulation now the default (D29) this field is a correctness requirement,
    not decoration: it is what stops a simulated run reading as a real one (R-SIM-6)."""

    inputs: dict[str, Any] = Field(default_factory=dict)
    """The action's own `model_dump()`, **after** slot resolution — so the record shows the
    numbers that were actually commanded, not the slot name they came from."""
    outputs: Outputs | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    warnings: list[Warning_] = Field(default_factory=list)
    error: ErrorInfo | None = None
    log_ref: LogRef | None = None


# --- handler registry --------------------------------------------------------

#: A handler is a **plain blocking function**. It does not build an `ActionResult`, touch the
#: plan, emit events, or catch its own errors — the runner wraps every call, which is what
#: makes R-LOG-5 structural rather than a convention, and what makes a handler directly
#: unit-testable with a mock driver and a stub context.
Handler = Callable[..., OutputsBase]

_HANDLERS: dict[str, Handler] = {}


def handler(kind: str) -> Callable[[Handler], Handler]:
    """Register the handler for one action kind.

    Duplicate registration raises rather than overwriting. Two slices each importing a module
    that claims `arm.waypoint` is a merge conflict that would otherwise resolve itself
    silently, in import order, and be invisible until the wrong one ran.
    """
    if kind not in OUTPUTS_FOR_KIND:
        raise KeyError(f"{kind!r} is not an action kind; expected one of {ACTION_KINDS}")

    def deco(fn: Handler) -> Handler:
        existing = _HANDLERS.get(kind)
        if existing is not None and existing is not fn:
            raise RuntimeError(
                f"duplicate handler for {kind!r}: {existing.__module__}.{existing.__qualname__} "
                f"and {fn.__module__}.{fn.__qualname__}")
        _HANDLERS[kind] = fn
        return fn

    return deco


def handler_for(kind: str) -> Handler | None:
    """The registered handler, or None. `None` is a pre-flight failure the engine reports as
    "this step cannot run" (R-ENG-17) — never a silent skip, which reads downstream as
    "it ran"."""
    return _HANDLERS.get(kind)


def registered_kinds() -> tuple[str, ...]:
    """Kinds with a handler, in `ACTION_KINDS` order. Used by pre-flight and by the
    readiness report, so an incomplete engine says so rather than failing at step 7."""
    return tuple(k for k in ACTION_KINDS if k in _HANDLERS)
