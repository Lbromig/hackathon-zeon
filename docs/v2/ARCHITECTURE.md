# v2 target architecture — scope-reduced rebuild

Date: 2026-07-26 · Companion to [`GAP_ANALYSIS.md`](./GAP_ANALYSIS.md) (trusted as the
statement of what exists today; not restated here).

This is a decision document. Every section commits to one design and says what it
rejected. Where a decision cannot be made without a bench measurement or a product
call, it appears in **§13 Open questions** rather than being hedged in prose.

**Three constraints shape everything below.**

1. **No hardware is attached.** Every path in this design must be exercisable with
   mocks/replay/synthetic drivers. Anything that can only be validated on the bench is
   marked `HW-ONLY` and is deliberately confined to the driver layer.
2. **The capability-ABC boundary is the one part of today's architecture that is
   right.** Nothing above `drivers/` imports a vendor SDK. v2 *strengthens* this: the
   camera subprocess is, architecturally, a jail for a vendor SDK behind a driver.
3. **This is a scope reduction.** ~40 % of non-test application code is deleted
   (`GAP_ANALYSIS.md` §6). The default answer to "should we keep this?" is no.

---

## 1. Target module layout

```
core/
  config.py                          [modify]  + SpeedTier table, SimConfig, log/run paths,
                                               waypoints_file; camera_host retained
  waypoints.py                       [new]     device-scoped waypoint namespace (replaces
                                               the pose-library read path in workflows)
  teach_poses.py                     [modify]  becomes the *storage* layer under waypoints.py
  speeds.py                          [new]     SpeedTier -> (linear mm/s, angular deg/s) per kind
  obs/
    __init__.py                      [new]
    log.py                           [new]     stdlib logging + JsonlFormatter + contextvars
    tail.py                          [new]     backwards tail + follow, used by the log API
  motion/
    cap_ops.py                       [modify]  step_deg configurable (90 for decap); keep pre-flight
    path_teach.py                    [delete]
    pick_place.py                    [delete]
    adapters.py                      [delete]
  perception/
    __init__.py                      [modify]
    geometry.py                      [new]     Transform alias + tiny helpers (was worldmodel.entities)
    markers.py                       [new]     tag size registry + spec_for (was calibration.markers,
                                               minus every twin/entity field)
    fiducials.py                     [modify]  imports move to .geometry/.markers; annotate() gets callers
    capture.py                       [keep]    unchanged — the on-disk frame contract
    shapes.py                        [keep]    used as the tube fallback detector
    tip.py                           [new]     pipette-tip-bottom detection
    tube.py                          [new]     tube-rim / tube-top detection
    jacobian.py                      [new]     per-camera image jacobian: load, apply, jog-calibrate
    offset.py                        [new]     multi-view offset solve + confidence + deviation
    overlay.py                       [new]     annotated-image renderer + artifact writer
    fusion.py                        [delete]
    projection.py                    [delete]
  sim/
    __init__.py                      [new]
    world.py                         [new]     the one piece of shared sim state (tip<->tube offset)
  calibration/                       [delete]  (markers.py content salvaged into perception/markers.py)
  worldmodel/                        [delete]  (Transform salvaged into perception/geometry.py)
  verification/                      [delete]
  viz/                               [delete]
  kinematics.py                      [delete]
  sequences.py                       [delete]
  teach_paths.py                     [delete]

drivers/
  base.py                            [keep]
  registry.py                        [modify]  register camera_proc / replay / servo_sim / lh types
  capabilities/
    arm.py                           [keep]    the reference for how a capability ABC should look
    camera.py                        [modify]  + snapshot(fresh: bool) contract note
    liquid_handler.py                [modify]  REWRITTEN interface — see §6
  xarm/driver.py                     [keep]    678 LOC of bench knowledge. Do not touch.
  camera/
    driver.py                        [keep]    OpenCV/UVC
    realsense.py                     [keep]
    still.py                         [keep]
    remote.py                        [modify]  generalised: the client half of the subprocess IPC
    replay.py                        [new]     plays temp/training/<session>/<cam>/ per manifest
    servo_sim.py                     [new]     synthetic converging tip/tube view, reads core.sim.world
  opentrons/driver.py                [modify]  transport protocol + dead-reckoned position + limits
  opentrons/transport.py             [new]     SerialTransport | LoopbackTransport | NullTransport
  mock/__init__.py                   [modify]  MockLiquidHandler gains relative moves + writes sim.world
  camera_proc/
    __main__.py                      [new]     the camera subprocess entrypoint
    server.py                        [new]     its tiny HTTP surface (/snapshot /stream /detections
                                               /health /shutdown) + frame pump + detector

backend/app/
  main.py                            [modify]  lifespan = supervisor up, init plan, readiness gate
  schemas.py                         [modify]  strip twin/sequence/calibration models
  engine/
    __init__.py                      [new]
    actions.py                       [new]     Action union, ActionResult, registry (§2)
    plan.py                          [new]     Plan, stable ids, mutation, loop expansion (§3)
    runner.py                        [new]     the run loop, pause/resume/inject/abort (§3)
    blackboard.py                    [new]     run-scoped typed slots + declarative references
    events.py                        [new]     event models + the broadcast hub
    handlers/
      lifecycle.py                   [new]     initialize/reinitialize (all | one device)
      arm.py                         [new]     home/waypoint/relative/grip/decap/traverse/reconnect/manual
      liquid_handler.py              [new]     move_relative / initialize / retract_z
      camera.py                      [new]     snapshot / search_code
      vision.py                      [new]     identify_tip / identify_tube / calculate_offset /
                                               select_offset / overlay
      control.py                     [new]     pause marker, loop, wait
    plans/
      startup.py                     [new]     the initialization plan (§5)
      handover.py                     [new]    THE 20-step workflow, as data (§3.6)
  services/
    device_manager.py                [modify]  + claims, + per-device reinit, + logging
    camera_supervisor.py             [new]     process model, supervision, reaping (§4)
    camera_hub.py                    [delete]  replaced by the supervisor + camera_proc
    startup_snapshot.py              [delete]  becomes a lifecycle init action
    twin.py / twin_fusion.py / kinematics.py / path_recorder.py   [delete]
  api/
    instruments.py                   [modify]
    teach.py                         [modify]  strip path-teach endpoints; keep everything else
    teach_lh.py                      [new]     liquid-handler teach endpoints (§10)
    cameras.py                       [modify]  proxies the subprocesses instead of owning threads
    engine.py                        [new]     plan/run/pause/resume/inject/abort + /ws/engine
    logs.py                          [new]     GET /api/logs + /ws/logs
    runs.py                          [new]     artifact serving
    agent.py                         [modify]  reduced to ONE endpoint: inject proposal (§9.4)
    workflow.py / sequences.py / calibration.py                   [delete]
  agent/{engine,policy,tools}.py     [delete]
  workflows/uncap_aspirate.py        [delete]  (its CHOREOGRAPHY survives as plans/handover.py data)
  llm/
    inject.py                        [new]     the only LLM call site; schema-constrained (§9.4)

frontend/src/
  main.ts                            [modify]  + router
  router.ts                          [new]
  App.vue                            [modify]  shell: header strip + nav + <RouterView>
  stores/
    engine.ts                        [new]     singleton reactive() fed by /ws/engine
    fleet.ts                         [new]     singleton reactive() fed by /ws/state
    logs.ts                          [new]     ring buffer fed by /ws/logs
  api/{client,cameras,teach,engine,logs}.ts    [modify/new]
  views/
    WorkflowView.vue                 [new]
    TeachView.vue                    [modify]  = today's TeachPanel, minus path-teach
    CamerasView.vue                  [modify]
    LogsView.vue                     [new]
  components/workflow/
    PlanChain.vue                    [new]     the indexed action list
    ActionCard.vue                   [new]     state chip, outputs, expandable logs
    ActionOutputs.vue                [new]     offsets / tip / tube / images
    RunControls.vue                  [new]     start / pause / resume / abort / inject
    InjectChat.vue                   [new]
    ReadinessPanel.vue               [new]     init state machine + warnings
  components/teach/
    ArmTeach*.vue                    [keep]    JogPad, JointJog, MoveTo, GripperControl, CapTools,
                                               PoseLibrary, TeachChecklist, CommandLog
    LiquidHandlerTeach.vue           [new]     XYZ jog, retract, init, waypoint save (§9)
    PathTeach.vue                    [delete]
    SequenceBuilder.vue              [delete]
  components/cameras/*               [keep]    CameraTab/CameraView are good; retarget the endpoint
  components/worldmap/               [delete]
  components/{InstrumentPanel,WorkflowRunner}.vue  [delete]

justfile                             [modify]  backend (no --reload) · frontend · sim · logs · reap

scripts/
  record_footage.py                  [keep]    it makes the simulation fixtures
  calibrate_lh_jacobian.py           [new]     the 6-jog image-jacobian measurement (§7.4) HW-ONLY-real,
                                               fully runnable in sim
  init_xarm.py                       [keep]
  validate_camera.py                 [delete]  992 LOC of diagnostics
  validate_motion.py                 [delete]
  find_joint_limit.py                [delete]  its *output* is preserved in core/config.py
  gripper_sequence.py                [delete]

data/                                # gitignored except the taught data
  waypoints.json                     [new]     device-scoped waypoints (replaces teach_poses.json)
  camera_jacobians.json              [new]     per-camera image jacobians (§7.4)
  logs/lab.jsonl                     [new]     THE log file
  runs/<run_id>/                     [new]     artifacts (overlay images) + run.json
  runtime/cameras.json               [new]     child pid/port registry, for reaping leaks
  teach_paths.json                   [delete]
```

Test suite: delete `test_{worldmodel,worldmodel_concurrency,extrinsics,scene,fusion,projection,
verification,kinematics,sequences,path_teach,agent_engine,workflow_twin_effects,workflow_execute,
integration_loop}.py`. Add `test_{actions,plan,runner_pause,runner_inject,loop_expansion,
camera_supervisor,camera_proc_contract,lh_driver,lh_transport,tip_detect,tube_detect,offset_solve,
jacobian,overlay,logging,init_orchestrator,engine_api}.py`. Keep
`test_{registry,api,motion,cap_ops,fiducials,shapes,capture,camera_remote,teach_api,
camera_host_config,cameras_api,joint_limit_recovery}.py` (some with edits).

---

## 2. The action model

This is the load-bearing decision. Everything else in v2 is a consequence of it.

### 2.1 Requirements it must satisfy simultaneously

An `Action` must be: (a) a plan element with a stable identity and a display index;
(b) JSON-serializable both ways, because it is streamed to the browser, logged, and
**constructed by an LLM**; (c) validated before it can move a robot; (d) dispatchable
to a blocking handler; (e) able to reference outputs of earlier actions without a
Python closure (an injected action is data, not code).

(b) + (e) are what kill the `Act`/`Step` dataclass-with-optional-fields shape used in
today's three executors. Nothing that accepts arbitrary kwargs can be safely
LLM-authored.

### 2.2 Decision: a Pydantic v2 discriminated union, one model per kind

```python
# backend/app/engine/actions.py
from typing import Annotated, Literal, Union
from pydantic import BaseModel, ConfigDict, Field

SpeedTier = Literal["slow", "medium", "fast"]
FailurePolicy = Literal["halt", "continue", "retry"]

class Ref(BaseModel):
    """A declarative reference to an earlier action's output. Never a closure."""
    model_config = ConfigDict(extra="forbid")
    slot: str                      # blackboard slot name, e.g. "selected_offset"
    field: str | None = None       # optional dotted path inside the slot value

class ActionBase(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)  # keeps FiniteModel's win
    aid: int                       # immutable stable id, monotonic per run. UI keys on this.
    index: int = 0                 # display index, RECOMPUTED on every plan mutation
    label: str = ""                # human text; auto-filled from the kind if blank
    device: str | None = None      # fleet id; None for pure-compute actions
    speed: SpeedTier = "medium"
    on_failure: FailurePolicy = "halt"
    max_attempts: int = 1
    parent_aid: int | None = None  # set on actions materialized from a loop body
    iteration: int | None = None   # 1-based, for loop-materialized actions
    origin: Literal["plan", "inject", "expand"] = "plan"
    note: str = ""

# ---- lifecycle -------------------------------------------------------------
class InitializeAll(ActionBase):
    kind: Literal["lifecycle.initialize_all"] = "lifecycle.initialize_all"
    home_after: bool = True

class InitializeDevice(ActionBase):
    kind: Literal["lifecycle.initialize_device"] = "lifecycle.initialize_device"
    device: str
    home_after: bool = False

class Reconnect(ActionBase):
    kind: Literal["lifecycle.reconnect"] = "lifecycle.reconnect"
    device: str
    # "re-connect / re-enable / re-engage" from the brief collapse to one action with
    # a scope, because on an xArm they are three calls in a fixed order and doing one
    # without the others leaves the arm in a state nothing else expects.
    scope: Literal["connect", "enable", "engage"] = "engage"

# ---- arm -------------------------------------------------------------------
class ArmHome(ActionBase):
    kind: Literal["arm.home"] = "arm.home"
    device: str
    speed: SpeedTier = "slow"      # the brief: home moves are slow

class ArmWaypoint(ActionBase):
    kind: Literal["arm.waypoint"] = "arm.waypoint"
    device: str
    waypoint: str                  # e.g. "RIGHT_ARM_APPROACH_RACK" — opaque label, see §3.7
    dx: float = 0.0                # brief: "with x/y/z offsets, default 0", mm, TCP frame
    dy: float = 0.0
    dz: float = 0.0

class ArmRelative(ActionBase):
    kind: Literal["arm.move_relative"] = "arm.move_relative"
    device: str
    dx: float = 0.0; dy: float = 0.0; dz: float = 0.0
    droll: float = 0.0; dpitch: float = 0.0; dyaw: float = 0.0
    offset_ref: Ref | None = None  # e.g. Ref(slot="selected_offset") overrides dx/dy/dz

class ArmGripper(ActionBase):
    kind: Literal["arm.gripper"] = "arm.gripper"
    device: str
    state: Literal["open", "close"]
    width: float | None = None     # gripper units (counts). None = fully.

class ArmDecap(ActionBase):
    kind: Literal["arm.decap"] = "arm.decap"
    device: str
    step_deg: float = 90.0         # brief: 360 deg in 90 deg steps
    turns: float = 1.0             # -> 4 bites
    grip_counts: float | None = None
    speed: SpeedTier = "slow"

class ArmTraverse(ActionBase):
    kind: Literal["arm.traverse"] = "arm.traverse"
    device: str
    waypoints: list[str]           # blended where the driver supports a radius
    blend_deg: float | None = 5.0

class ArmManual(ActionBase):
    kind: Literal["arm.manual_move"] = "arm.manual_move"
    device: str
    on: bool                       # free-drive; the engine refuses `on` unless paused

# ---- liquid handler --------------------------------------------------------
class LHRelative(ActionBase):
    kind: Literal["lh.move_relative"] = "lh.move_relative"
    device: str
    dx: float = 0.0; dy: float = 0.0; dz: float = 0.0    # mm
    offset_ref: Ref | None = None
    clamp_mm: float = 15.0         # refuse a vision-derived step larger than this

class LHInitialize(ActionBase):
    kind: Literal["lh.initialize"] = "lh.initialize"
    device: str
    wiggle_mm: float = 3.0
    retract_z_mm: float | None = None   # None = full retract

# ---- cameras ---------------------------------------------------------------
class CameraSnapshot(ActionBase):
    kind: Literal["camera.snapshot"] = "camera.snapshot"
    device: str
    fresh: bool = True             # force a frame newer than the request. See §4.6.
    store: bool = True             # write to data/runs/<run_id>/ and emit an artifact
    slot: str = "frame"            # blackboard slot: "frame:<device>"

class CameraSearchCode(ActionBase):
    kind: Literal["camera.search_code"] = "camera.search_code"
    device: str
    marker_ids: list[int]          # AprilTag/ArUco ids to wait for
    timeout_s: float = 15.0
    require_all: bool = False

# ---- vision (computational) -----------------------------------------------
class IdentifyTip(ActionBase):
    kind: Literal["vision.identify_tip"] = "vision.identify_tip"
    device: str                    # the camera whose frame to use
    frame_ref: Ref | None = None   # default: "frame:<device>"

class IdentifyTube(ActionBase):
    kind: Literal["vision.identify_tube"] = "vision.identify_tube"
    device: str
    frame_ref: Ref | None = None

class CalculateOffset(ActionBase):
    kind: Literal["vision.calculate_offset"] = "vision.calculate_offset"
    device: str                    # per-camera partial offset
    tip_ref: Ref | None = None
    tube_ref: Ref | None = None

class SelectOffset(ActionBase):
    kind: Literal["vision.select_offset"] = "vision.select_offset"
    device: None = None
    offset_refs: list[Ref]         # the per-camera offsets to fuse/choose between
    slot: str = "selected_offset"
    max_deviation_mm: float = 1.0  # above this, fall back to the best single view

class RenderOverlay(ActionBase):
    kind: Literal["vision.overlay"] = "vision.overlay"
    device: str
    offset_ref: Ref

# ---- control ---------------------------------------------------------------
class Loop(ActionBase):
    kind: Literal["control.loop"] = "control.loop"
    device: None = None
    body: list["Action"]
    until: "Condition"
    max_iterations: int = 12
    no_progress_abort: int = 3     # consecutive iterations without improvement

class Checkpoint(ActionBase):
    """An explicit pause point in the plan. Distinct from the pause *button*."""
    kind: Literal["control.checkpoint"] = "control.checkpoint"
    device: None = None
    message: str = ""

Action = Annotated[
    Union[InitializeAll, InitializeDevice, Reconnect,
          ArmHome, ArmWaypoint, ArmRelative, ArmGripper, ArmDecap, ArmTraverse, ArmManual,
          LHRelative, LHInitialize,
          CameraSnapshot, CameraSearchCode,
          IdentifyTip, IdentifyTube, CalculateOffset, SelectOffset, RenderOverlay,
          Loop, Checkpoint],
    Field(discriminator="kind"),
]

class Condition(BaseModel):
    """Declarative, evaluated against the blackboard. No eval, no lambdas — it has to
    survive JSON and be authorable by an LLM."""
    model_config = ConfigDict(extra="forbid")
    all_of: list["Condition"] = []
    slot: str | None = None
    field: str | None = None        # e.g. "magnitude_mm"
    op: Literal["<", "<=", ">", ">=", "==", "exists"] = "<"
    value: float | None = None
```

**Why one model per kind and not `kind: str` + `params: dict`.** The union is what
makes `Action.model_json_schema()` a usable tool schema for the LLM injector (§9.4),
makes a bad `dz` a 422 instead of a crashed arm move, and makes the frontend's
per-kind renderers exhaustive-checkable in TypeScript (generate the TS types from the
schema; do not hand-write them).

### 2.3 ActionResult

```python
class Artifact(BaseModel):
    kind: Literal["image/frame", "image/overlay", "json"]
    camera: str | None = None
    path: str                      # repo-relative
    url: str                       # /api/runs/{run_id}/artifacts/{name}
    width: int = 0
    height: int = 0
    bytes: int = 0
    sha256: str = ""

class ErrorInfo(BaseModel):
    type: str                      # exception class name
    message: str
    retriable: bool
    device: str | None = None

class Warning_(BaseModel):
    code: str                      # e.g. "home_not_defined"
    message: str
    device: str | None = None

class LogRef(BaseModel):
    """How the frontend gets this action's log lines. Deliberately a QUERY, not a byte
    range: the log file rotates, and an offset into a rotated file is a wrong answer
    presented confidently."""
    run_id: str
    aid: int

class ActionResult(BaseModel):
    aid: int
    index: int
    kind: str
    device: str | None = None
    status: Literal["complete", "failed", "skipped", "aborted"]
    attempt: int = 1
    started_at: str                # ISO8601 UTC
    finished_at: str
    duration_ms: int
    simulated: bool                # true if ANY device it touched was simulated
    inputs: dict[str, Any]         # the action's own model_dump(), post-Ref-resolution
    outputs: Outputs               # typed union, discriminated on `kind`
    artifacts: list[Artifact] = []
    warnings: list[Warning_] = []
    error: ErrorInfo | None = None
    log_ref: LogRef
```

`Outputs` is a second discriminated union, one per action kind. The ones that carry
real payload:

```python
class MoveOutputs(BaseModel):
    kind: Literal["move"] = "move"
    pose_before: list[float]; pose_after: list[float]      # x y z r p y
    joints_before: list[float]; joints_after: list[float]
    waypoint: str | None = None
    resolved_speed: dict[str, float]                       # {"linear": 60.0, "angular": 15.0}

class TipOutputs(BaseModel):
    kind: Literal["tip"] = "tip"
    found: bool
    bottom_px: list[float] | None = None      # [u, v]
    axis_deg: float | None = None
    method: Literal["tag_anchored", "classical"] | None = None
    score: float = 0.0

class TubeOutputs(BaseModel):
    kind: Literal["tube"] = "tube"
    found: bool
    top_px: list[float] | None = None
    rim_ellipse: list[float] | None = None    # [cx, cy, major, minor, angle_deg]
    marker_id: int | None = None
    method: Literal["tag_anchored", "hough_rim"] | None = None
    score: float = 0.0

class OffsetOutputs(BaseModel):
    kind: Literal["offset"] = "offset"
    camera: str
    delta_px: list[float]                     # [du, dv] tube_top - tip_bottom
    offset_mm: dict[str, float | None]        # {"x": 2.1, "y": None, "z": -4.8}
    observed_axes: list[str]
    magnitude_mm: float | None                # over the observed axes only
    deviation_mm: float                       # see §7.5
    confidence: float
    scale_source: Literal["depth", "tag", "jacobian", "config"]

class SelectedOffsetOutputs(BaseModel):
    kind: Literal["selected_offset"] = "selected_offset"
    offset_mm: dict[str, float]               # all three axes, fused
    magnitude_mm: float
    deviation_mm: float
    confidence: float
    strategy: Literal["fused", "best_single"]
    contributions: list[dict[str, Any]]       # per-camera weight + residual
```

### 2.4 Registration and dispatch

```python
# backend/app/engine/actions.py
Handler = Callable[[ActionBase, "ExecContext"], "Outputs"]

_HANDLERS: dict[str, Handler] = {}

def handler(kind: str):
    def deco(fn: Handler) -> Handler:
        if kind in _HANDLERS:
            raise RuntimeError(f"duplicate handler for {kind!r}")
        _HANDLERS[kind] = fn
        return fn
    return deco
```

Handlers are **plain blocking functions** returning `Outputs`. They do not build an
`ActionResult`, do not touch the plan, do not emit events, and do not catch their own
errors. The runner wraps every call: it resolves `Ref`s, binds logging context, times,
catches, retries, records artifacts, emits events. This means a handler is directly
unit-testable with a mock driver and a stub `ExecContext`, and no handler can forget to
log.

```python
@dataclass
class ExecContext:
    run_id: str
    action: ActionBase
    devices: DeviceManager
    board: Blackboard
    artifacts: ArtifactStore         # .save_image(camera, image, tag) -> Artifact
    log: logging.LoggerAdapter       # already bound to run_id + aid + kind + device
    gate: Callable[[], None]         # cooperative pause/abort check, for long actions
    simulated: Callable[[str], bool] # is this device simulated?
    warn: Callable[[str, str], None] # (code, message) -> appended to result.warnings
```

`gate()` is what makes `arm.decap` and `control.loop` interruptible: they call it at
each bite/iteration boundary. See §3.3.

### 2.5 How simulation is expressed

**Decision: simulation is driver substitution, selected per device by config. There is
no `if simulate:` anywhere above the driver layer.**

The reason is the whole point of having a simulation: if the code path differs between
sim and real, a green sim run proves nothing about the real one. So the engine, the
handlers, the plan, the logging and the events are byte-identical in both modes; only
`build_driver()` returns something different.

The gap-analysis complaint that sim is all-or-nothing (`GAP_ANALYSIS.md` §3) is fixed
by making the selection per device:

```python
# core/config.py
SIM_SUBSTITUTIONS = {          # real type -> simulated type
    "xarm": "mock_arm",
    "opentrons": "mock_liquid_handler",
    "realsense": "replay",     # real recorded pixels, not synthetic
    "camera": "replay",
    "camera_proc": "replay",
}

@dataclass
class SimConfig:
    """HZ_SIM: "none" | "all" | a comma list of fleet ids or kinds
    ("arms", "cameras", "lh"). HZ_SIM_SESSION picks the replay session."""
    devices: frozenset[str]
    session: str = ""          # temp/training/<session>; "" = newest
    servo: bool = False        # cameras that participate in the servo loop use servo_sim
```

`Settings.load()` applies it last, after the env overrides and after `HZ_CAMERA_HOST`,
by rewriting `entry["type"]`. `ActionResult.simulated` is then derived from the actual
driver's `info.vendor == "mock"` / `meta["live"] is False`, so a result can never claim
to be real when it was not.

**The servo loop's converging fake images.** Replay cannot produce them: the 1 156
recorded frames do not converge, and a per-iteration counter that fakes convergence
tests nothing. Instead, `core/sim/world.py` holds exactly one piece of shared state:

```python
@dataclass
class SimWorld:
    """The single shared fact between the simulated LH and the simulated cameras:
    where the pipette tip is relative to the tube top, in mm.

    MockLiquidHandlerDriver.move_relative() SUBTRACTS the commanded delta from it (with
    a small multiplicative error and additive noise), and ServoSimCameraDriver renders
    tip and tube at the pixel positions that offset implies, per camera, through that
    camera's configured jacobian.

    So the loop converges because the moves it commands are actually applied — the same
    reason it will converge on hardware — and it fails to converge if the jacobian sign
    is wrong, which is exactly the bug this loop can have in the real world.
    """
    offset_mm: dict[str, float] = field(default_factory=lambda: {"x": 12.0, "y": -7.0, "z": 9.0})
    gain_error: float = 0.85       # the LH under-shoots; forces >1 iteration
    noise_mm: float = 0.15
```

This is the single most important testability decision in the document: it makes
workflow step 19 — the part with no existing implementation at all — a deterministic
pytest, with `random.seed()` fixed.

### 2.6 How log records attach to an action

Via `contextvars`, not via passing a logger down by hand. `core/obs/log.py` installs a
`logging.Filter` that stamps every record with the current `run_id` / `aid` /
`action_kind` / `device`. The runner sets those at the top of each attempt and resets
them after. `ActionResult.log_ref` therefore needs no offsets — the frontend asks
`GET /api/logs?run_id=…&aid=…`. Details in §8.

---

## 3. The engine

### 3.1 Concurrency model — decision and justification

**Decision: keep blocking work in threads. Exactly one worker thread executes actions;
`asyncio` owns only the websocket fan-out. Do not port the engine to `async def`.**

Justification, in order of weight:

1. **The xArm SDK is blocking and not asyncio-aware.** `move_joints(wait=True)` blocks
   for seconds inside a C-adjacent socket protocol with its own command lock. cv2 is
   blocking. `pyserial` is blocking. An async engine would `await
   loop.run_in_executor(...)` on every single driver call — the same threads, plus a
   layer of ceremony, plus a new class of bug (an accidentally-unwrapped blocking call
   freezes the event loop and with it `/ws/state`, the log stream, and the *pause
   button*). The existing codebase already learned this: `api/teach.py`'s module
   docstring and `_fleet_snapshot()`'s comment are both about exactly this hazard.
2. **One worker thread means no intra-run concurrency.** There is no lock discipline
   inside handlers, no two-moves-on-one-arm race, no reentrancy. The teach API's
   "one in-flight command per arm" invariant is preserved by having the runner take an
   exclusive **claim** on every device its plan touches for the duration of the run;
   teach endpoints return 409 while a run holds a claim.
3. **The existing thread→queue→websocket bridge already works.** `api/agent.py`'s
   `loop.call_soon_threadsafe(queue.put_nowait, event)` pattern is correct and tested;
   v2 generalises it into one broadcast hub instead of re-inventing it.

Rejected alternative: an asyncio engine with a task per action (§11.1). Rejected
alternative: a separate engine *process* (§11.6).

### 3.2 Plan representation

```python
class Plan(BaseModel):
    run_id: str
    name: str
    actions: list[Action]          # flat; loop bodies are materialized into this list
    cursor: int = 0                # index of the next action to execute
    revision: int = 0              # bumped on every mutation; the UI reconciles on it

    def renumber(self) -> None:
        for i, a in enumerate(self.actions):
            a.index = i
```

**Stable `aid` + recomputed `index`.** The brief requires both "every action has an
index" and "the agent can inject an action between indexes". Those conflict: inserting
at 7 renumbers 7..N, and if the UI keys rows on the index it loses per-action state and
scroll position on every injection. So each action carries an immutable `aid`
(monotonic per run), the frontend keys on `aid`, and `index` is a derived display
field recomputed by `renumber()`. Injection targets are expressed as
`after_aid` / `before_aid`, not as indexes, so a concurrent renumber cannot make an
injection land in the wrong place.

The plan and cursor live behind a `threading.Lock`. Only the runner thread advances the
cursor; only the API thread mutates the list, and only while paused (§3.4).

### 3.3 The run loop

```
RunController.start(plan)
  -> claim devices (409 to teach endpoints while held)
  -> emit run_started + plan_replaced(full plan)
  -> spawn ONE thread: _run()

_run():
  while True:
      self._gate()                       # blocks here if paused; raises Aborted if aborted
      with lock: action = plan.at(cursor)  or  None -> break
      for attempt in 1..action.max_attempts:
          bind log context(run_id, aid, kind, device)
          emit action_started
          try:
              inputs  = resolve_refs(action, board)
              outputs = HANDLERS[action.kind](action, ctx)
          except Aborted: emit action_finished(aborted); raise
          except Exception as e:
              if attempt < max_attempts and policy == "retry": emit action_retry; continue
              emit action_finished(failed)
              if policy == "halt":  -> pause the run, do NOT advance cursor
              if policy == "continue": -> advance and carry on
              break
          board.publish(action, outputs)
          emit action_finished(complete)
          break
      with lock: plan.cursor += 1
  emit run_finished
```

Three deliberate properties:

* **A `halt` failure pauses rather than terminates, with the cursor still on the failed
  action.** The operator can then jog by hand in the teach tab, inject a corrective
  action, and resume — which is the whole point of having pause/inject. Terminating the
  run throws away the state that makes recovery possible.
* **Retry is opt-in per action, and defaults off for motion.** Re-running a motion that
  already partly executed is how you crash a gripper into a rack. The kinds that
  default to `on_failure="retry", max_attempts=3` are exactly the idempotent ones:
  `camera.snapshot`, `vision.*`, `camera.search_code`.
* **`arm.decap` and `control.loop` call `ctx.gate()` at their internal boundaries** so
  pause takes effect between bites / iterations, not only between actions.

### 3.4 Pause, resume, abort

```python
class RunController:
    _resume = threading.Event()     # SET means "running". Cleared = paused.
    _abort  = threading.Event()

    def _gate(self) -> None:
        if self._abort.is_set(): raise Aborted()
        if not self._resume.is_set():
            emit(run_paused(reason))
            self._resume.wait()                 # the engine thread parks here
            if self._abort.is_set(): raise Aborted()
            emit(run_resumed())
```

* **Pause is cooperative and takes effect at the next boundary.** It does *not* stop a
  move in flight. This is stated in the UI: the pause button goes to a "pausing…" state
  until the current action finishes. Pretending otherwise would mean calling
  `driver.stop()` mid-trajectory, which leaves the arm somewhere no waypoint describes.
* **`abort` is the separate, hard thing**: it sets `_abort`, calls `driver.stop()` on
  every claimed arm and `retract_z()` on the LH, and unwinds the run. It bypasses the
  gate for the same reason `/stop` in `api/teach.py` bypasses the busy lock.
* `control.checkpoint` clears `_resume` from inside the plan — pause-as-data, so a plan
  can require an operator ack at a known point.
* `arm.manual_move(on=True)` is refused unless the run is paused or not running: free
  drive plus a queued motion command is a documented hazard in `arm.py`.

### 3.5 Injection

```
POST /api/engine/inject
{ "after_aid": 118, "actions": [ {...Action...}, ... ], "source": "chat|form",
  "rationale": "operator asked to re-snapshot before nudging" }
```

Applied by the API thread:

1. `was_running = _resume.is_set()`; clear `_resume`. **Injection always pauses first**
   — the brief says so, and it is also the only way to make the splice atomic against
   a cursor advance.
2. Wait (with a 10 s deadline) for the runner to report parked. If the deadline passes,
   409 — the operator is told the engine is busy inside a long move rather than having
   the plan mutated under it.
3. Under the lock: validate every action through the `Action` union; assign fresh
   `aid`s; reject if the resolved insertion point is `<= cursor` (you cannot inject into
   the past, and injecting *at* the cursor is ambiguous — inject after it and let the
   operator step); splice; `renumber()`; `revision += 1`.
4. Emit `plan_replaced` with the **whole** plan. Full replacement, not a patch: the plan
   is at most a few hundred small objects, and a patch protocol is a second source of
   truth about ordering that will drift from the server's.
5. If `was_running`, set `_resume` again. ("resumes once the injection is added".)

### 3.6 The loop construct as plan data

Workflow step 19 is a `control.loop` action. The runner **expands** it: on entry,
iteration *n*'s body is deep-copied, given fresh `aid`s, `parent_aid` = the loop's aid,
`iteration = n`, `origin = "expand"`, and spliced into the plan immediately after the
loop action; then `plan_replaced` is emitted and the cursor walks into it. After the
body completes, `until` is evaluated against the blackboard; if false and the caps
allow, iteration *n+1* is expanded the same way.

Why materialize instead of running a hidden inner loop: the brief requires that *every*
action past and future has an index, a state, and its own outputs and logs. A hidden
loop gives the UI one row for twelve snapshots, six overlays and four LH nudges. This
way iteration 3's overlay image is a first-class indexed action the operator can click.

The frontend renders materialized bodies indented under their `parent_aid` with an
iteration badge, which is also how it stays readable at ~50 rows.

```python
# backend/app/engine/plans/handover.py  (abridged: step 19 only)
Loop(
    aid=AUTO, label="align pipette tip to tube",
    max_iterations=12, no_progress_abort=3,
    until=Condition(all_of=[
        Condition(slot="selected_offset", field="magnitude_mm", op="<", value=1.5),
        Condition(slot="selected_offset", field="deviation_mm", op="<", value=1.0),
    ]),
    body=[
        CameraSnapshot(device="gripper_cam",  fresh=True),
        CameraSnapshot(device="handover_cam", fresh=True),
        IdentifyTip(device="gripper_cam"),   IdentifyTip(device="handover_cam"),
        IdentifyTube(device="gripper_cam"),  IdentifyTube(device="handover_cam"),
        CalculateOffset(device="gripper_cam"),  CalculateOffset(device="handover_cam"),
        RenderOverlay(device="gripper_cam",  offset_ref=Ref(slot="offset:gripper_cam")),
        RenderOverlay(device="handover_cam", offset_ref=Ref(slot="offset:handover_cam")),
        SelectOffset(offset_refs=[Ref(slot="offset:gripper_cam"),
                                  Ref(slot="offset:handover_cam")]),
        LHRelative(device="ot", offset_ref=Ref(slot="selected_offset"), clamp_mm=15.0),
    ],
)
```

`no_progress_abort=3`: if `magnitude_mm` has not improved by >5 % for three consecutive
iterations, the loop fails with `error.code = "servo_stalled"`. Without this the loop
oscillates forever on a wrong jacobian sign — the single most likely real failure.

### 3.7 Waypoints are device-scoped data, never code

`core/waypoints.py` reads `data/waypoints.json`:

```json
{ "right": { "RIGHT_ARM_APPROACH_RACK": { "joints": [...], "pose": {...},
             "gripper_width": 420, "saved_at": "..." },
             "LEFT_ARM_TRANSITION_MID_TABLE": { "...": "..." } } }
```

The name is an **opaque label scoped to a device id**. This is the resolution of the
brief's steps 14–18, which name `LEFT_ARM_*` waypoints while the right arm acts: they
are stored under `right` with those exact names, and nothing breaks, because no code
parses a waypoint name. Renaming them is a data edit, not a code change. Recorded as
open question **Q-WP-1** because the *bench* still needs to decide whether the names or
the acting device are the typo.

The engine pre-flights the whole plan before the first move (`GET
/api/engine/preflight`): every referenced waypoint exists for its device, every joint
target passes `check_joint_target`, every device is in the fleet. This preserves the one
good property all three of today's executors share.

### 3.8 Speed tiers

`core/speeds.py`:

```python
ARM_TIERS = {"slow":   {"linear": 30.0,  "angular": 8.0},    # mm/s, deg/s
             "medium": {"linear": 80.0,  "angular": 20.0},
             "fast":   {"linear": 150.0, "angular": 45.0}}
LH_TIERS  = {"slow": 10.0, "medium": 30.0, "fast": 60.0}     # mm/s
```

Resolved at dispatch and then clamped by `ArmLimits.max_speed_*`, so a tier can never
widen a configured soft limit. `resolved_speed` is recorded in `MoveOutputs` — the log
says what actually got commanded, not what was asked for.

### 3.9 Event protocol (`/ws/engine`)

One socket, one envelope. `seq` is monotonic per run so a reconnecting client can tell
it missed something and re-fetch `GET /api/engine/run`.

```json
{"seq": 1, "ts": "2026-07-26T09:41:02.114Z", "type": "run_started",
 "run_id": "r_20260726T094102Z", "name": "handover",
 "simulated": {"left": false, "right": false, "ot": true, "gripper_cam": true},
 "action_count": 42}
```

```json
{"seq": 2, "type": "plan_replaced", "run_id": "...", "revision": 1, "cursor": 0,
 "actions": [
   {"aid": 1, "index": 0, "kind": "arm.gripper", "device": "right",
    "label": "right arm open gripper", "speed": "fast", "state": "planned",
    "parent_aid": null, "iteration": null,
    "params": {"state": "open", "width": null}}
 ]}
```

```json
{"seq": 3, "type": "action_started", "run_id": "...", "aid": 1, "index": 0,
 "attempt": 1, "kind": "arm.gripper", "device": "right",
 "inputs": {"state": "open", "width": null}}
```

```json
{"seq": 4, "type": "action_log", "run_id": "...", "aid": 1,
 "level": "INFO", "msg": "right: release()", "ts": "..."}
```

`action_log` is a *convenience mirror* of the log file, capped at 50 lines per action on
the socket; the full record always comes from `/api/logs`. This keeps the "expand an
action to see its logs" interaction instant for the live action without making the
engine socket the log transport.

```json
{"seq": 9, "type": "action_finished", "run_id": "...", "aid": 12, "index": 11,
 "status": "complete", "attempt": 1, "duration_ms": 812, "simulated": true,
 "outputs": {"kind": "offset", "camera": "gripper_cam",
             "delta_px": [18.4, -31.2],
             "offset_mm": {"x": 3.9, "y": null, "z": -6.6},
             "observed_axes": ["x", "z"], "magnitude_mm": 7.67,
             "deviation_mm": 0.41, "confidence": 0.72, "scale_source": "jacobian"},
 "artifacts": [{"kind": "image/overlay", "camera": "gripper_cam",
                "path": "data/runs/r_.../0011_gripper_cam_overlay.jpg",
                "url": "/api/runs/r_.../artifacts/0011_gripper_cam_overlay.jpg",
                "width": 960, "height": 540, "bytes": 78412, "sha256": "…"}],
 "warnings": [], "error": null,
 "log_ref": {"run_id": "r_...", "aid": 12}}
```

```json
{"seq": 20, "type": "run_paused",  "reason": "operator" }
{"seq": 21, "type": "run_resumed"}
{"seq": 22, "type": "run_paused",  "reason": "action_failed", "aid": 27}
{"seq": 23, "type": "loop_iteration", "aid": 30, "iteration": 3,
            "metrics": {"magnitude_mm": 4.1, "deviation_mm": 0.5}}
{"seq": 40, "type": "run_finished", "status": "complete|failed|aborted",
            "completed": 41, "failed": 0, "duration_ms": 184320}
{"seq": 41, "type": "readiness", "state": "READY",
            "warnings": [{"code": "home_not_defined", "device": "left",
                          "message": "no HOME waypoint taught for 'left'; skipped the home move"}]}
```

State values a client needs for an action row: `planned | running | complete | failed |
skipped | aborted`. The frontend derives them from `action_started` /
`action_finished`; `plan_replaced` resets everything past the cursor to `planned`.

---

## 4. Camera subprocess architecture

### 4.1 Why subprocesses at all (the real reason)

Not performance. Opening a UVC device on macOS can block in an **uninterruptible kernel
wait** that `kill -9` cannot reap — observed on this bench and documented in
`startup_snapshot.py:12-15`. Additionally, repeatedly opening and closing the same
device inside one process degrades it, while a *fresh process* reads the same camera
fine (`camera_hub.py:28-33`). Both facts have the same consequence: **a thread cannot be
abandoned, a process can.** Everything below follows from "the parent must never block
on, and must never require the death of, a camera."

### 4.2 Process model

One child per camera slot. Four slots → four children. The child is
`python -m drivers.camera_proc --camera <fleet_id> --config <json> --port 0`. It owns
exactly one `CameraDriver` (`realsense` / `camera` / `still` / `replay` / `servo_sim`)
plus one frame pump plus the detectors — i.e. it is today's `CameraWorker` and
`FiducialDetector`/`ShapeDetector`, moved across a process boundary essentially
unchanged. The bench-tuned detector parameters travel with it and are not re-derived.

Handshake: the child prints **one JSON line to stdout** and then never writes
unstructured output again:

```json
{"ready": true, "camera": "overview_cam", "pid": 40122, "port": 51733,
 "has_depth": true, "intrinsics": {"fx": 615.2, "fy": 615.2, "cx": 424.0, "cy": 240.0,
 "width": 848, "height": 480, "coeffs": [0,0,0,0,0]}, "driver": "realsense", "live": true}
```

Port 0 (kernel-assigned) rather than a fixed `8110 + n`: a leaked child from a previous
run still holds its port, and a fixed scheme turns that into an unexplainable bind
failure on the *new* child. The assigned port goes into `data/runtime/cameras.json`
along with the pid, so the next boot can find and reap the leaks (§4.5).

### 4.3 IPC / frame transport — decision

**Decision: MJPEG multipart + JSON over HTTP on a loopback port, one port per child,
consumed by a generalised `drivers/camera/remote.py`.**

Child surface (deliberately tiny — it is not a web app):

| route | purpose |
|---|---|
| `GET /health` | `{ok, seq, fps, error, uptime_s}` — the supervisor's liveness probe |
| `GET /stream` | `multipart/x-mixed-replace` MJPEG, the continuous feed |
| `GET /snapshot?fresh=1&min_seq=N` | one JPEG; `fresh` blocks (bounded) for `seq > N` |
| `GET /detections` | `{seq, w, h, detections: [...]}` — same shape as today's `CameraSnapshot.as_dict()` |
| `POST /shutdown` | release the device, then exit 0 |

Why this and not the alternatives:

* **It already exists and is hardened.** `RemoteCameraDriver` (384 LOC) is a working
  MJPEG client with a background reader thread, a latest-frame buffer, a
  **staleness guard** (`STALE_AFTER_S`, raise rather than serve a stale frame as
  current), part-size sanity limits, and socket-close-under-a-blocked-read shutdown.
  All of those are things a hand-rolled transport would have to relearn. v2 changes
  ~nothing in it except making `base_url` point at `127.0.0.1:<port>` instead of a
  bench host, and adding `/snapshot?fresh=`.
* **The browser is a first-class consumer.** `GET /api/cameras/{id}/stream` becomes a
  byte-for-byte relay of the child's `/stream` (`StreamingResponse` over an
  `http.client` response). No decode, no re-encode, no second JPEG generation. Shared
  memory and pipes cannot do this without the parent rebuilding a multipart stream.
* **Three concurrent, independent consumers.** The browser stream, the engine's
  synchronous snapshot, and the detection read all want different things at different
  rates. HTTP gives three endpoints; a pipe gives one ordered byte stream and a framing
  protocol to invent.
* **Volume is trivial.** 4 cameras × 15 fps × ~50 KB JPEG ≈ 3 MB/s over loopback. This
  is not a bandwidth problem, so paying complexity to avoid a copy is a bad trade.
* **`HZ_CAMERA_HOST` keeps working for free** — a remote bench and a local child are the
  same client against the same contract, which is the property that lets a laptop with
  no hardware run the whole stack.

Rejected: shared memory (§11.2), pipes (§11.2), gRPC/ZeroMQ (a dependency and a schema
for something four HTTP routes cover).

### 4.4 Where detection runs

**In the child, next to the pixels.** Detection is the one thing that must not cross
the boundary as raw frames: it needs the full array, at ~5 Hz, per camera. Running it in
the child also preserves today's good decoupling (`detect_every`, so a slow detector
never stalls the video) and keeps the failure isolated — a cv2 fault in the detector
kills one camera process, not the backend.

Consequence, stated honestly: **frames the engine pulls for vision actions are decoded
twice** (encoded in the child, decoded in the parent). That is the same trade
`remote.py` already documents and accepts. At the rates involved (a handful of frames
per servo iteration, not 15 fps) it is irrelevant.

Depth is the real limitation: MJPEG carries colour only. So the child additionally
serves `GET /depth?seq=N` returning a **16-bit PNG in millimetres** — exactly the format
`core/perception/capture.save_frame` already writes and `load_depth_mm` already reads.
Reusing that encoding rather than inventing one means the depth path is testable against
the existing fixtures.

### 4.5 Supervision, shutdown, restart

`CameraSupervisor` (one monitor thread, not one per child):

* **Start:** spawn, read the handshake line with a **6 s deadline**. On timeout the slot
  is marked `unavailable: handshake timeout` — this is the macOS-hang case — the child
  is *recorded as leaked* and the supervisor moves on. It never joins, never waits, and
  the backend finishes booting. `/api/cameras` shows the slot red with that reason.
* **Liveness:** `GET /health` every 2 s with a 1 s timeout. Three consecutive failures,
  or `proc.poll() is not None`, → restart.
* **Restart backoff:** 1 s, 2 s, 4 s, 8 s, capped at 15 s. **Crash budget: 5 restarts in
  120 s → `permanently_failed`.** A camera that cannot open must stop consuming the log
  and the operator's attention; the UI offers a manual "retry" button
  (`POST /api/cameras/{id}/restart`), which is the explicit human override.
* **Clean shutdown, in order:** `POST /shutdown` (preferred — the child is inside its
  own loop and can release the `VideoCapture` *before* its socket dies, which is what
  "quit cleanly" means for a UVC device) → `SIGTERM` at +2 s → `SIGKILL` at +5 s →
  at +8 s give up, record the pid in `data/runtime/cameras.json` as leaked, log
  `ERROR camera_leaked`, return. Shutdown of the backend is never allowed to block on a
  camera.
* **Reaping leaks at boot:** read `data/runtime/cameras.json`; for each recorded
  pid/port, if the pid is alive and its `/health` answers with our own camera id, adopt
  it instead of spawning a duplicate (this makes `uvicorn --reload` survivable); if it
  is alive but unresponsive, `SIGKILL` once and then leave it alone.
* Children are started with `start_new_session=True` so a `Ctrl-C` in the terminal does
  not race the supervisor's ordered shutdown by delivering SIGINT to the whole group.

**`--reload` trap, stated because it will bite someone:** uvicorn's reloader replaces the
app process, so children spawned in `lifespan` are orphaned on every code change. Hence
`just backend` (the "one command") runs *without* `--reload`, `just dev` keeps reload for
frontend-adjacent work, and the boot-time adoption above makes the reload case merely
untidy rather than broken.

### 4.6 Coexisting with a synchronous `take_snapshot`

The engine's `camera.snapshot` handler must return a frame, now, and must not hang.

* The parent-side driver keeps a background reader on `/stream`, so the newest frame is
  already in memory. `fresh=False` is a memory read — microseconds.
* `fresh=True` (the default inside the servo loop) sends `GET
  /snapshot?fresh=1&min_seq=<seq at request time>` with a **2.5 s deadline**. The child
  blocks on its own frame condition and returns the first frame strictly newer.
  **This is not optional:** after an LH nudge, a cached pre-move frame makes the loop
  measure the offset it just corrected and never converge. `min_seq` is the correctness
  mechanism; the deadline is the liveness mechanism.
* Timeout → `DriverError` → `camera.snapshot` has `on_failure="retry", max_attempts=3`
  → then the action fails and the run pauses. At no point does a thread park forever.

---

## 5. Initialization orchestrator

### 5.0 The two commands

```
just backend     # uv run uvicorn backend.app.main:app --port 8000
                 #   NO --reload: lifespan spawns the camera children (§4.5)
just frontend    # cd frontend && npm install && npm run dev
```

`just backend` is the whole backend: FastAPI + the device manager + the four camera
subprocesses + the initialization plan, in one process tree that exits cleanly on
`Ctrl-C`. Supporting recipes: `just sim` (`HZ_SIM=all just backend`), `just test`,
`just logs` (pretty-print the JSONL tail), `just reap` (kill any recorded leaked camera
children). `justfile` is `[modify]`; `dev` keeps `--reload` for frontend-adjacent work
and is documented as the reload-orphans-children path.

`main.lifespan` order, with the reasoning for each position:

```
configure_logging()                 # first: everything after this is logged
device_manager.load_fleet()         # build drivers (incl. sim substitution)
camera_supervisor.reap_stale()      # adopt/kill children recorded by a previous run
camera_supervisor.start_all()       # non-blocking; a hung child cannot delay boot
init_orchestrator.start()           # runs plans/startup.py on the engine, backgrounded
yield                               # <- the API is serving here, state = INITIALIZING
init_orchestrator.abort()
engine.abort_if_running()           # stop() every claimed arm, retract LH Z
camera_supervisor.stop_all()        # ordered: /shutdown -> TERM -> KILL -> leak-record
device_manager.disconnect_all()     # arms brake here; must be last
```

The API comes up *before* initialization completes, on purpose: the frontend must be
able to show the init state machine progressing, including a device that is failing.
The readiness gate (§5.4), not the lifespan, is what prevents a premature Start.

### 5.1 The state machine

```
IDLE ─▶ DISCOVERING ─▶ CONNECTING ─▶ INITIALIZING ─▶ HOMING ─▶ READY
                            │              │            │
                            └──────────────┴────────────┴──▶ DEGRADED   (some devices failed)
                                                          └──▶ FAILED   (no device usable)
```

Held by `InitOrchestrator`, exposed at `GET /api/engine/readiness` and pushed as a
`readiness` event. Transitions are logged as first-class records.

* `DISCOVERING` — build drivers from the fleet (`DeviceManager.load_fleet`), start the
  camera supervisor, enumerate what actually answered. Adds real information over
  today's config-only fleet: a slot whose child never handshook is *discovered as
  absent*.
* `CONNECTING` — `connect()` per device, in parallel across *kinds* (arms are TCP,
  cameras are already child processes, the LH is serial) but sequential within a kind.
  Per-device outcome recorded; one failure never aborts the rest — the existing
  `connect_all()` already gets this right.
* `INITIALIZING` — run the per-device init routines (§5.2).
* `HOMING` — slow move to the taught `HOME` waypoint per arm (§5.3).
* `READY` / `DEGRADED` / `FAILED`.

### 5.2 Init runs as a plan on the engine

**Decision: the initialization procedure is a `Plan` executed by the same runner as the
workflow**, built by `backend/app/engine/plans/startup.py`. It is not a separate
imperative function.

Consequences, all of them wanted: init gets per-action indexes, states, outputs, and
logs in the same log file and the same UI as the workflow; the brief's "initialize /
reinitialize all devices" and "initialize / reinitialize an individual device" actions
are then literally the same code as boot; and an operator can pause and inspect a boot
that is going wrong.

Per-device routines (each is one `lifecycle.initialize_device` handler branch, dispatched
on `InstrumentKind`):

| kind | routine | outputs |
|---|---|---|
| camera | take one picture, after `SETTLE_FRAMES=5` throwaway grabs, write to `temp/captures/<slot>/` + `data/runs/<run_id>/` | frame artifact, `w/h`, `intrinsics`, `has_depth` |
| arm | `clear_errors()` → `enable(True)` → read pose+joints → verify `mode == 0` (not free-drive) | `joints`, `pose`, `gripper_info`, `error_code` |
| liquid_handler | `initialize()`: ±`wiggle_mm` on each of x, y, z, returning to start each time, then `retract_z()` | per-axis `moved: bool`, `position_after`, `z_retracted_mm` |

The camera settle-frame logic and the "one frame per slot so you can see what a slot
actually got this run" rationale are lifted wholesale from `startup_snapshot.py`, which
is then deleted. That file's reasoning is bench knowledge; its *thread* is not.

### 5.3 The home move and the "no home defined" path

`HOMING` looks up the waypoint literally named `HOME` for each arm in
`data/waypoints.json`:

* **Defined:** `ArmHome` → resolve joints → `check_joint_target` → `move_joints(joints,
  speed=slow_tier)`. Never `xArm.move_gohome()`: the controller's zero pose is not a
  bench-safe pose, and today's `ArmDriver.home()` gives no speed control. `ArmDriver`
  keeps `home()` (it is the controller primitive) but the *engine action* uses the
  taught waypoint.
* **Not defined:** emit `Warning_(code="home_not_defined", device=…, message="no HOME
  waypoint taught for 'left'; skipped the home move — teach it in the Teach tab")`,
  set the action `status="skipped"`, and continue. It never blocks readiness.

The warning surfaces in exactly three places, all fed from `ActionResult.warnings`:
`readiness.warnings[]` on the socket, an amber banner in `ReadinessPanel.vue`, and a
`WARNING`-level JSONL record. Neither arm has a `HOME` pose today
(`GAP_ANALYSIS.md` §1.6), so **this warning path is the default experience on day one**
and must look intentional, not broken.

### 5.4 The readiness gate

`Start` in the workflow tab is enabled when `state == READY`. At `DEGRADED` it is
enabled but requires a confirm dialog listing the unusable devices; the engine
additionally refuses at pre-flight if the plan *touches* a device that is not connected.
At `FAILED` it is disabled. `POST /api/engine/reinitialize` (all) and
`POST /api/instruments/{id}/reinitialize` (one) re-enter the state machine.

---

## 6. Liquid-handler capability redesign

### 6.1 The interface

Today's ABC is wrong for the target scope in both directions: it *requires*
`pick_up_tip / drop_tip / aspirate / dispense` (which the target workflow never uses)
and *lacks* relative motion and an init routine (which it needs on every iteration of
step 19). Rewrite:

```python
# drivers/capabilities/liquid_handler.py
@dataclass
class LHPosition:
    x: float | None = None
    y: float | None = None
    z: float | None = None
    source: Literal["measured", "dead_reckoned", "unknown"] = "unknown"

@dataclass
class LHLimits:
    """Soft envelope enforced ABOVE the transport. Load-bearing: the inputs to
    move_relative come from a vision estimate, so a detector failure must not be able
    to drive the head into the deck."""
    x: tuple[float, float] = (0.0, 400.0)       # mm, machine frame
    y: tuple[float, float] = (0.0, 350.0)
    z: tuple[float, float] = (0.0, 120.0)
    max_step_mm: float = 25.0                   # per single relative command
    max_speed: float = 60.0                     # mm/s

@dataclass
class InitReport:
    axes_moved: list[str]
    z_retracted_mm: float | None
    detail: str
    ok: bool

class LiquidHandlerDriver(InstrumentDriver):
    kind = InstrumentKind.LIQUID_HANDLER

    @property
    def axes(self) -> tuple[str, ...]: return ("x", "y", "z")
    @property
    def limits(self) -> LHLimits: ...

    @abstractmethod
    def get_position(self) -> LHPosition: ...
    @abstractmethod
    def move_relative(self, dx: float = 0.0, dy: float = 0.0, dz: float = 0.0,
                      speed: float | None = None, wait: bool = True) -> LHPosition: ...
    @abstractmethod
    def move_to(self, position: LHPosition, speed: float | None = None,
                wait: bool = True) -> LHPosition: ...
    @abstractmethod
    def retract_z(self, height_mm: float | None = None) -> LHPosition:
        """Raise Z to a safe height (None = the top of the envelope)."""
    @abstractmethod
    def home(self) -> None: ...
    @abstractmethod
    def initialize(self, wiggle_mm: float = 3.0,
                   retract_z_mm: float | None = None) -> InitReport:
        """Small back-and-forth on every axis, then retract Z. This is the brief's
        per-device init routine, at the driver layer, so it can encode
        vendor-specific settling without leaking upward."""

    # Pipetting is OUT of the target scope. Left as non-abstract so the OT can grow it
    # back without an ABC change, and so no mock has to implement dead code.
    def aspirate(self, volume_ul: float, at: LHPosition | None = None) -> None:
        raise NotImplementedError(f"{self.device_id}: no aspirate")
    def dispense(self, volume_ul: float, at: LHPosition | None = None) -> None:
        raise NotImplementedError(f"{self.device_id}: no dispense")
```

`DeckLocation` is deleted: slot/well addressing needs a labware model the target scope
does not have, and `LHPosition` covers everything step 19 and 20 ask for.
`check_move` (the analogue of `check_joint_target`) returns a reason string or `None`
and is called by the handler *before* the driver, so a refused move is a clean
`ActionResult` and not a driver exception.

### 6.2 Keeping it testable when the real driver has no transport

`OpentronsDriver` is a stub whose `_send()` returns `None` and reports success
(`GAP_ANALYSIS.md` §1.3). The fix is not to wait for the serial branch to merge; it is
to make the part that *can* be tested, testable, and to make the untested part
obviously untested.

Split at the transport:

```python
# drivers/opentrons/transport.py
class Transport(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def send(self, line: str, timeout_s: float = 5.0) -> str: ...   # one command, one reply

class SerialTransport:      """pyserial. HW-ONLY — never exercised in CI."""
class LoopbackTransport:    """A scripted G-code responder: parses G0/G28/M114, keeps an
                             internal position, answers 'ok' and 'X:… Y:… Z:…'. This is
                             what makes the ENCODING testable without hardware."""
class NullTransport:        """Records lines, always answers 'ok'. For plan-level tests."""
```

`OpentronsDriver` then contains only encoding + envelope checks + position bookkeeping,
and `test_lh_transport.py` asserts on the exact wire lines
(`move_relative(dx=1.5)` → `G91` / `G0 X1.500 F1800` / `G90`). The bug class this
catches — wrong units, wrong axis letter, absolute-vs-relative confusion, an
unacknowledged command treated as success — is *the* bug class that made the current
stub dangerous.

**Position feedback is dead-reckoned, and says so.** The OT-One over this path has no
trustworthy absolute readback, so the driver maintains position from `home()` plus the
sum of accepted moves and reports `source="dead_reckoned"`. `M114`, where available,
is used to *reconcile* and logs a `WARNING lh_position_drift` on disagreement > 1 mm.
The servo loop only needs relative moves, so nothing in the target scope depends on the
absolute number being right — which is why this honesty costs nothing.

`MockLiquidHandlerDriver` implements the same ABC, enforces the same `LHLimits`, and its
`move_relative` writes `core.sim.world` (§2.5) — so it is not merely a null object, it
is the thing that closes the simulated servo loop.

---

## 7. Vision computational actions

### 7.1 What is and is not achievable — stated first

There are **no extrinsics between the gripper camera and the handover camera**, no
stereo rig, and (per `GAP_ANALYSIS.md` §3) **no intrinsics and no depth in any recorded
session**. Therefore:

* A metric 3-D triangulation of the tip↔tube vector from the two views is **not
  achievable**, and any design claiming it is lying.
* What *is* achievable, needs no calibration board, and is fully simulatable: a
  **per-camera image jacobian** — how many pixels each image axis moves per millimetre
  of each liquid-handler axis. That is exactly the quantity a servo loop needs, it is
  measurable by the machine itself in six moves, and it makes each view an independent
  partial observation of the offset in machine coordinates.

Everything below is built on that. The loop closes on *the offset expressed in the
axes the liquid handler can actually move*, which is the only frame that matters.

### 7.2 Tip-bottom detection (`core/perception/tip.py`)

Two methods, tried in order; the method used is reported in `TipOutputs.method`.

1. **`tag_anchored` (primary).** A tag36h11 sticker on the pipette carriage. The tip
   bottom is a fixed offset from the tag origin, configured per camera as a
   `(du, dv)` in *tag widths* so it is scale-invariant:
   `bottom_px = tag_center + R(tag_angle) · (offset_tagwidths · tag_edge_px)`.
   Why primary: `fiducials.py` is the most reliable, most bench-tuned, most tested
   component in the repo, and it already returns sub-pixel corners. Deriving the tip
   from it inherits all of that. Cost: one sticker and one one-time offset measurement.
2. **`classical` (fallback).** Config-driven ROI → `preprocess()` (the same
   gated-CLAHE from `fiducials.py`, reused, not reimplemented) → Canny → vertical
   closing kernel → largest component with `bbox.h/bbox.w > 2.5` and monotonically
   narrowing row-widths in its lower third → `bottom_px` = centroid of the lowest 3
   rows of the mask; `axis_deg` from the component's second moment.
   `score = clamp(component_area / expected_area) · taper_consistency · roi_containment`.

No learned detector (§11.7).

### 7.3 Tube-top detection (`core/perception/tube.py`)

1. **`tag_anchored` (primary).** The brief explicitly permits ArUco/AprilTag. A tag on
   the tube (or on the rack cell) gives identity *and* orientation; the tube top is a
   configured offset in tag widths, as above. Identity matters here in a way it does not
   for the tip: with a rack in frame, "which circle is the tube" is otherwise a guess.
2. **`hough_rim` (fallback).** Reuse `ShapeDetector` for candidate circles inside the
   ROI, then refine: take the candidate's annulus, run `cv2.fitEllipse` on its edge
   points, and define `top_px` as the ellipse point with minimum *v*. The rim of a tube
   viewed obliquely is an ellipse, and its topmost point is the visible top of the
   opening — which is what the brief asks for and is markedly more stable than the
   circle centre plus a radius.
   `score` combines the ellipse fit residual, the axis ratio (a plausible viewing
   obliquity), and the diameter bucket agreement from `shapes.py`.

`ShapeDetector`'s diameter buckets need depth *and* intrinsics to produce metres; with
neither, `kind` degrades to `"circle"` and only the pixel geometry is used. That is
already how the module behaves — no change, just no pretending.

### 7.4 The per-camera image jacobian (`core/perception/jacobian.py`)

```json
// data/camera_jacobians.json
{ "gripper_cam": {
    "J": [[ 8.41, 0.12, -0.33],      // du/dx, du/dy, du/dz   (px per mm)
          [-0.21, 0.44, -7.98]],     // dv/dx, dv/dy, dv/dz
    "measured_at": "2026-07-26T09:12:00Z", "residual_px": 0.7,
    "session": "r_20260726T091200Z", "frames": 12 } }
```

Measured by `scripts/calibrate_lh_jacobian.py`: from a start pose, jog the LH ±5 mm on
each of x, y, z (6 moves), take a `fresh=True` snapshot at each end, track the tip
marker's pixel centre, and least-squares fit the 2×3 matrix. Runs identically in sim
(where the ground truth is `core.sim.world`, so the test asserts the recovered `J`
matches the configured one), which means the calibration *procedure itself* is unit
tested with no hardware.

This replaces the deleted `core/calibration/` — a 9-step pipeline of which 5 steps were
TODO and which needed a printed board and an unmeasured spacing constant — with 6 moves
the machine performs on itself.

**Fallbacks when `J` is absent**, in priority order, recorded in
`OffsetOutputs.scale_source`:
`depth` (RealSense: `mm_per_px = depth_mm / fx`, isotropic, axis mapping from config) →
`tag` (a tag of known size in frame: `mm_per_px = tag_size_mm / tag_edge_px`) →
`config` (a constant; forces `confidence ≤ 0.4`). `jacobian` is the only source that
gives per-axis scale *and* sign *and* cross-coupling; the others need a configured axis
map and are explicitly weaker.

### 7.5 The offset solve, confidence, and deviation

`vision.calculate_offset` (per camera) produces a **partial** offset:

```
Δp = tube_top_px − tip_bottom_px                     # [du, dv], 2 values
solve  J · d = Δp   for d ∈ R³                        # 2 equations, 3 unknowns
```

Underdetermined per view, so per view we solve the **minimum-norm** solution restricted
to the axes that view actually observes: an axis *a* is "observed" iff
`‖J[:, a]‖ ≥ 0.15 · max_col_norm` — i.e. the view responds to motion on that axis. In
practice the right-gripper camera observes {x, z} and the handover camera observes
{y, z}, which is why two views are needed and why z is measured twice. `offset_mm` is
`None` on unobserved axes. Never zero — a zero would be silently obeyed by the mover.

`vision.select_offset` then does the real work, on the stacked system:

```
[ J_A ]        [ Δp_A ]
[ J_B ] · d  = [ Δp_B ]        # 4 equations, 3 unknowns -> overdetermined
```

Weighted least squares with per-row weights `w = confidence_view / mm_per_px_view`, then:

```python
d_hat        = argmin  Σ w_i (J_i·d − Δp_i)²
residual_px  = J·d_hat − Δp                     # 4-vector
deviation_mm = sqrt(mean( (residual_px / scale_px_per_mm)² ))
magnitude_mm = ‖d_hat‖
```

**`deviation_mm` is therefore the extent to which the two views cannot be explained by
any single rigid offset** — geometrically meaningful, unitful, and zero when the views
agree. That is a defensible metric, and it is precisely what a wrong detection, a
stale frame, or a stale jacobian produces. It is *not* a repeatability estimate, and the
document should not pretend it is: with two views you get one degree of redundancy, so
`deviation_mm` is a one-DoF disagreement, not a covariance.

Single-view degradation: with only one usable view, `deviation_mm` is set to the
**detector-implied** uncertainty instead — `(tip.score_sigma_px + tube.score_sigma_px) /
scale` — and `confidence` is capped at 0.5. Honest, and it keeps the loop condition
evaluable rather than undefined.

Confidence, a product of documented factors, each in [0, 1]:

```python
confidence = ( tip.score
             * tube.score
             * SCALE_TRUST[scale_source]          # jacobian .95 | depth .90 | tag .85 | config .40
             * exp(-deviation_mm / 2.0)           # disagreement penalty
             * (0.6 + 0.4 * observed_axes / 3)    # coverage
             * clamp(1.0 - magnitude_mm / 60.0, 0.3, 1.0) )  # the jacobian is LOCAL:
                                                  # a huge offset is a linear extrapolation
```

`SelectOffset.strategy`: `"fused"` normally; if `deviation_mm > max_deviation_mm` the
two views are in real conflict, so fusing them averages a wrong answer with a right one
— fall back to `"best_single"`, take the highest-`confidence` view, and move **only its
observed axes**. This is the brief's "select the offset with higher confidence", applied
where it is actually the better rule rather than everywhere.

The `LHRelative` handler applies a **damping gain of 0.7** and clamps to `clamp_mm=15`.
Damping is not optional: an undamped servo with a 15 % jacobian scale error oscillates,
and 0.7 turns that into monotone convergence in ~4 iterations. The gain lives on the
handler, is logged, and is the first thing to look at when `servo_stalled` fires.

### 7.6 Annotated images — output and storage contract

`core/perception/overlay.py`, one function:

```python
def render(frame, *, tip: TipOutputs, tube: TubeOutputs, offset: OffsetOutputs,
           header: str) -> np.ndarray
```

Draws: the tip marker + its axis; the tube rim ellipse + a caret at `top_px`; the
offset vector from tip-bottom to tube-top with a mid-line label
`7.7 mm  (x +3.9, z −6.6)`; a corner block with `confidence`, `deviation_mm`,
`scale_source`, `method` for both detectors; and a footer
`r_20260726T094102Z · #0011 · gripper_cam · iter 3 · SIM`. `FiducialDetector.annotate()`
— which today has zero call sites — is called for the tag layer, so it finally earns
its keep.

Storage: `ArtifactStore` writes to `data/runs/<run_id>/<index:04d>_<camera>_<tag>.jpg`
(JPEG q92; these are for looking at, and a 960×540 PNG per iteration per camera adds up)
and returns the `Artifact` model from §2.3, including `sha256` so an image referenced in
a log line is verifiable. `data/runs/` — not `temp/` — because a run's evidence must
survive; `temp/` is explicitly transient in this repo. `data/runs/<run_id>/run.json`
holds the full plan + every `ActionResult`, making a run replayable in the UI after a
restart. Retention: keep the 50 most recent runs, prune oldest at boot, log what was
pruned.

---

## 8. Logging architecture

### 8.1 Setup

`core/obs/log.py`, called once from `main.lifespan` before anything else:

```python
def configure(level="INFO", path=settings.log_file) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(_jsonl_handler(path))     # RotatingFileHandler + JsonlFormatter
    root.addHandler(_console_handler())       # StreamHandler + human formatter
    root.addFilter(ContextFilter())           # stamps run_id/aid/kind/device
```

Every `print()` in `backend/`, `core/`, `drivers/` (30 of them) is replaced by a module
logger. The `[camera_hub]` / `[config]` / `[device_manager]` hand-written prefixes
become the `logger` field, which is what they were imitating.

### 8.2 JSONL, not text — the argument

The requirement is three things at once: **every action's inputs and outputs** in the
file, **the frontend can read it**, and **log records attach to an action**. Structured
inputs/outputs in a text line means inventing a serialization (and a parser for it) that
JSON already is. Filtering by `run_id` + `aid` in the API means either a JSON field or a
regex over a format that will change. So: JSONL.

The counter-argument — "you can't read it with `tail`" — is answered by *also* emitting
a human line to the console through the same logger (a second handler, same records, so
they cannot disagree) and by `just logs`, which pretty-prints the tail. One file, one
writer, one format.

```json
{"ts":"2026-07-26T09:41:03.882Z","level":"INFO","logger":"engine.handlers.vision",
 "msg":"offset computed","run_id":"r_20260726T094102Z","aid":12,"index":11,
 "action_kind":"vision.calculate_offset","device":"gripper_cam","simulated":true,
 "event":"action_output",
 "inputs":{"frame_ref":{"slot":"frame:gripper_cam"}},
 "outputs":{"kind":"offset","offset_mm":{"x":3.9,"y":null,"z":-6.6},
            "deviation_mm":0.41,"confidence":0.72},
 "artifacts":["data/runs/r_.../0011_gripper_cam_overlay.jpg"],
 "duration_ms":812}
```

Reserved `event` values: `action_start | action_output | action_error | action_warning |
run_start | run_end | plan_mutated | readiness | device_state | camera_event`. Anything
without an `event` is an ordinary diagnostic line. Non-finite floats are coerced to
strings by the formatter, reusing `main._json_safe`'s lesson — a NaN that breaks
`json.dumps` inside a log call is a log line that eats an exception.

### 8.3 Action-scoped context binding

`contextvars.ContextVar` for `run_id`, `aid`, `index`, `action_kind`, `device`, set by
the runner at the top of each attempt and reset in a `finally`. Contextvars are
per-thread, and the runner *is* one thread, so this is exact rather than best-effort.
A handler that calls into `drivers/` gets driver-level log lines stamped with the
action that caused them **for free** — which is the property that makes "expand an
action to see the logs it produced" real instead of approximate.

### 8.4 One file, two processes

Camera children must not append to the rotating file: two writers plus rotation is a
corruption hazard and a lost-records hazard. **Children log JSONL to stdout; the
supervisor reads their stdout on a per-child thread and re-emits each record through the
parent logger with `proc: "camera:<id>"` added.** The single-writer invariant holds, the
"same log file" requirement holds, and a child that dies mid-line loses one line
instead of corrupting the file. A child's unparseable stdout line is logged as
`WARNING camera_stdout_unparsed` with the raw text — never dropped.

### 8.5 Rotation and the tail transport

`RotatingFileHandler(maxBytes=64 MiB, backupCount=5)` — size, not time: an image-heavy
run produces a burst, and a daily rotation either loses a busy afternoon or keeps a
month of idle files. 320 MiB ceiling, bounded and predictable.

* `GET /api/logs?run_id=&aid=&level=&event=&limit=500&since_seq=` — `core/obs/tail.py`
  reads the file **backwards** in 64 KiB blocks, parses, filters, stops at `limit`. No
  index, no database: filtering a few hundred thousand JSON lines backwards is
  milliseconds, and an index is a second thing that can be wrong.
* `WS /ws/logs?level=&run_id=` — one follower thread per process (not per client)
  watching the file for appends, handling the rotation case (inode change → reopen),
  publishing into the same broadcast hub as the engine events. Clients get a filtered
  stream. A separate socket from `/ws/engine` on purpose: logs must be viewable with no
  run in progress, and the Logs tab must not have to open a run socket.

---

## 9. Frontend architecture

### 9.1 Router and state — decisions

**Add `vue-router`. Do not add Pinia.**

Router, because the workflow tab needs deep links the app cannot express today:
`/workflow`, `/workflow/:runId`, `/workflow/:runId/:aid` (an expanded action),
`/logs?run_id=…&aid=…`, `/teach/:deviceId`. The current `localStorage`-backed `tab` ref
is a router with one feature and no URLs; "send me the link to the action that failed"
is a real operator need during bring-up.

No Pinia, because there is exactly one producer per domain (one websocket) and zero
cross-store transactions. A module-scoped `reactive()` singleton with exported actions
is the same thing minus a dependency and minus a second idiom for people to learn:

```ts
// stores/engine.ts
const state = reactive({
  runId: null as string | null,
  status: "idle" as "idle"|"running"|"paused"|"finished"|"failed"|"aborted",
  revision: 0, cursor: 0,
  actions: [] as ActionRow[],                 // ordered; keyed by aid
  byAid: new Map<number, ActionRow>(),
  results: new Map<number, ActionResult>(),
  liveLogs: new Map<number, LogLine[]>(),     // capped ring per aid
  readiness: { state: "IDLE", warnings: [] as Warning[] },
  lastSeq: 0,
});
```

`plan_replaced` rebuilds `actions`/`byAid` while **preserving `results` by `aid`** —
this is the payoff of stable ids: an injection renumbers indexes without the UI losing a
single completed action's outputs. If `event.seq !== state.lastSeq + 1`, the store
re-fetches `GET /api/engine/run` rather than rendering a plan with a hole in it.

TypeScript types for actions/outputs are **generated** from the FastAPI OpenAPI schema
into `src/api/generated.ts`. Hand-written mirrors of a 21-member discriminated union
will drift within a day.

### 9.2 Tab / component tree

```
App.vue                                    header: readiness chip · sim badge · run status
 └ RouterView
    ├ WorkflowView.vue          /workflow[/:runId[/:aid]]
    │   ├ ReadinessPanel.vue          init state machine, per-device rows, warnings, Reinit
    │   ├ RunControls.vue             Start · Pause/Resume · Abort · Inject · sim toggle
    │   ├ CurrentActionCard.vue       the "what is happening now" node
    │   └ PlanChain.vue               the indexed chain
    │       └ ActionCard.vue  (xN)    index · kind · device · state chip · duration
    │           ├ ActionOutputs.vue   offsets table · tip/tube badges · artifact thumbs
    │           └ ActionLogs.vue      lazy GET /api/logs?run_id&aid on expand
    ├ TeachView.vue             /teach/:deviceId
    │   ├ (arms)  JogPad · JointJog · MoveTo · GripperControl · CapTools ·
    │   │         PoseLibrary · TeachChecklist · CommandLog        [all kept]
    │   └ LiquidHandlerTeach.vue                                    [new, §9.3]
    ├ CamerasView.vue           /cameras                            [kept, retargeted]
    └ LogsView.vue              /logs                               [new]
```

Deleted: `fleet` tab (`InstrumentPanel` folds into `ReadinessPanel`), `world` tab and
`WorldMapTab.vue`, `WorkflowRunner.vue`, `PathTeach.vue`, `SequenceBuilder.vue`.

### 9.3 Plan / action rendering, and the liquid-handler teach tool

`ActionCard` renders from `(ActionRow, ActionResult | null)` and is exhaustive over
`kind` for the params summary and over `outputs.kind` for the outputs panel — a new
action kind is a TypeScript error until it has a renderer. Loop-materialized actions are
indented under their `parent_aid` with an `iter n` badge; the loop row itself shows a
sparkline of `magnitude_mm` per iteration, which is the one chart that tells an operator
whether the servo is converging.

Artifacts render as thumbnails from `artifact.url` with a lightbox; overlays and raw
frames are visually distinguished, because "the offset was computed on *this* pixel
data" is the whole point of storing them.

`LiquidHandlerTeach.vue` (absent today in every form) mirrors the arm teach tool's
proven shape: a 3-axis jog pad (±0.1 / 1 / 5 / 10 mm), a step-size selector, a live
`LHPosition` readout **labelled with its `source`** (a dead-reckoned number presented as
measured is a trap), `Retract Z`, `Initialize`, and `Save as waypoint` writing into the
same `data/waypoints.json` under the `ot` device. Every button is one POST to
`/api/lh/{id}/…` (§10) and is logged like everything else.

### 9.4 Inject-chat, and where the LLM call happens

**The LLM call happens on the backend, in `backend/app/llm/inject.py`. It is the only
LLM call site in the system, and it never mutates the plan.**

```
InjectChat.vue
  ──POST /api/engine/inject/propose {"message": "...", "after_aid": 118}
      backend: build the tool schema from Action.model_json_schema()
               + a context block: the plan window around after_aid, the current
                 blackboard summary, the device list, the last 3 ActionResults
               -> LLM -> JSON -> validate through the Action union
  <──{"proposal_id":"p_…","after_aid":118,
      "actions":[{...validated Action...}], "rationale":"…",
      "warnings":["'ot' is simulated in this run"]}
  UI renders a DIFF of the plan with the proposed rows highlighted
  ──POST /api/engine/inject {"proposal_id":"p_…"}   (operator pressed Apply)
```

Three non-negotiables:

* **Server-side**, because the API key must never reach the browser, and because the
  proposal must be validated against the *same* pydantic union the engine dispatches on.
  That validator is Python; duplicating it in TypeScript would create a second, weaker
  gate.
* **Propose ≠ apply.** An LLM never mutates a plan that drives a robot without an
  explicit human Apply. The proposal is inert data with a TTL.
* **A no-LLM path is required.** `InjectChat` has a "manual" mode: pick a `kind` from
  the union, fill a form generated from that kind's JSON schema, insert. Everything in
  this system must be demonstrable with no API key, and the manual form is also the
  fastest way for an operator who already knows what they want.

Rejected: calling the LLM from the browser (key exposure, no validation, no logging).
The proposal, the raw model response, and the applied result are all logged as
`event: "plan_mutated"` records with the operator's original message.

---

## 10. API surface

### Kept

| method | path | note |
|---|---|---|
| GET | `/api/health` | |
| GET | `/api/instruments` | |
| GET | `/api/instruments/{id}/status` | |
| POST | `/api/instruments/{id}/connect` | |
| GET | `/api/arms` · `/api/arms/{id}/state` | |
| POST | `/api/arms/{id}/{jog,move_to,home,gripper,enable,cap,free_drive,clear_errors,stop}` | the teach tool, unchanged |
| GET/POST/DELETE | `/api/arms/{id}/poses[/{name}]`, `/api/arms/{id}/poses/{name}/goto` | now backed by `waypoints.json` |
| GET | `/api/cameras` · `/api/cameras/devices` | |
| GET | `/api/cameras/{id}/{stream,snapshot,detections}` | same contract; now proxied to a child |
| WS | `/ws/state` | fleet + camera detections, 2 Hz |

### New

| method | path | purpose |
|---|---|---|
| GET | `/api/engine/readiness` | init state machine + warnings |
| POST | `/api/engine/reinitialize` | re-enter the state machine (all devices) |
| POST | `/api/instruments/{id}/reinitialize` | one device |
| GET | `/api/engine/plans` | available named plans (`startup`, `handover`) |
| GET | `/api/engine/plans/{name}/preflight` | waypoints/devices/limits, before anything moves |
| POST | `/api/engine/run` | `{plan, overrides}` → start; 409 if a run holds claims |
| GET | `/api/engine/run` | full current run: plan + results + status (reconnect path) |
| POST | `/api/engine/pause` · `/resume` · `/abort` | |
| POST | `/api/engine/step` | execute exactly one action while paused |
| POST | `/api/engine/inject/propose` | LLM → validated proposal (no mutation) |
| POST | `/api/engine/inject` | apply a proposal or an explicit action list |
| WS | `/ws/engine` | the event protocol of §3.9 |
| GET | `/api/runs` · `/api/runs/{run_id}` | history from `data/runs/*/run.json` |
| GET | `/api/runs/{run_id}/artifacts/{name}` | overlay/frame images |
| GET | `/api/logs` | filtered backwards tail |
| WS | `/ws/logs` | follow |
| GET | `/api/lh` · `/api/lh/{id}/state` | liquid-handler teach |
| POST | `/api/lh/{id}/jog` | `{dx,dy,dz,speed}` |
| POST | `/api/lh/{id}/move_to` · `/retract_z` · `/initialize` · `/home` | |
| GET/POST/DELETE | `/api/lh/{id}/poses[/{name}]` | LH waypoints |
| POST | `/api/cameras/{id}/restart` | manual override for a `permanently_failed` child |
| GET | `/api/cameras/{id}/depth` | 16-bit depth PNG (mm), proxied |

### Removed

`/ws/workflow`, `/ws/agent`, `/ws/calibrate`, `/api/agent/goal`,
`/api/workflow/{plan,required_poses,preflight}`, all of `/api/sequences*`,
`/api/worldmodel*` (incl. `/scene`, `/scene.svg`), `/api/arms/{id}/paths*` (6 routes),
`/api/instruments/connect` (superseded by `/api/engine/reinitialize`),
`/api/instruments/{id}/frame` (superseded by `/api/cameras/{id}/snapshot`),
`/api/cameras/{id}/{connect,stop}` (the supervisor owns child lifecycle).

Net effect: the three overlapping "run a workflow" websockets (`/ws/workflow`,
`/ws/agent`, `/ws/calibrate`) collapse into one `/ws/engine`; ~20 twin, sequence, and
path-teach routes are removed; the additions are the engine control surface, the log
transport, and the liquid-handler teach tool — the three things the brief asks for that
have no endpoint today.

---

## 11. Rejected alternatives

**11.1 Port the engine to asyncio (one task per action).** Rejected — see §3.1. Short
form: every driver call is blocking, so async means `run_in_executor` everywhere (same
threads, more ceremony) plus a failure mode where one forgotten `await` freezes the
pause button. Async also buys concurrency we actively do not want.

**11.2 Shared memory (or pipes) for frame transport.** Rejected. Shared memory needs a
hand-rolled ring buffer, generation counter, and reader-liveness scheme, and still
cannot feed the browser — the parent would have to rebuild a multipart stream anyway.
Pipes give one ordered consumer where three concurrent ones are needed. At ~3 MB/s over
loopback the copy-avoidance argument is worthless, and MJPEG-over-HTTP reuses 384 lines
of hardened client code including the staleness guard.

**11.3 Keep cameras as in-process threads.** Rejected — see §4.1. A thread blocked in an
uninterruptible kernel `open()` cannot be killed or joined and takes shutdown with it; a
process can be abandoned and recorded. Threads also cannot isolate a cv2/librealsense
segfault to one viewpoint.

**11.4 Pinia (or Vuex) for frontend state.** Rejected. One websocket producer per
domain, no cross-store transactions, no time-travel need. A module-scoped `reactive()`
singleton is the same code minus a dependency and minus a second pattern. `vue-router`
*is* added, because deep-linking a specific action of a specific run is a real need
that no amount of local state solves.

**11.5 Keep `core/sequences.py` as the plan format.** Rejected. It is arm-only, has 4
action kinds, and its `Step` is a flat dataclass with optional fields — which cannot be
safely LLM-authored, cannot express a loop, cannot carry typed outputs, and cannot
represent a camera or a computational action. Extending it to the 21 kinds of §2 means
rewriting it. Its two good rules (pre-flight everything before the first move; a step
that cannot run must fail rather than be skipped) are carried into the engine verbatim.

**11.6 An external workflow engine (Temporal / Prefect / Celery + Redis) or a separate
engine process.** Rejected. Durable-execution engines solve retries across process
restarts and multi-worker scheduling; here there is one operator, one bench, one run at
a time, and a run that outlives a backend restart is *undesirable* (the arms' physical
state did not survive it either). The cost is a broker, a worker topology, and an
opaque scheduling layer between the pause button and the arm.

**11.7 A learned tip/tube detector (fine-tune on the 1 156 recorded frames).** Rejected
for v2. There are no labels, no held-out set, no intrinsics, and no depth in those
sessions; a learned detector's failures are also non-reproducible in a unit test, which
breaks the "everything testable with mocks" constraint. The tag-anchored primary path is
more accurate than a small fine-tune would be, and the classical fallback is
deterministic. The recorded frames are kept as replay fixtures, which is where their
value is today. Revisit once labelled data and per-camera intrinsics exist.

**11.8 Metric 3-D triangulation from stereo extrinsics between the two cameras.**
Rejected — see §7.1. It needs hand-eye calibration of a *moving* eye-in-hand camera plus
a board plus per-camera intrinsics, none of which exist; the deleted
`core/calibration/` pipeline was 5/9 TODO on exactly this path. The 6-move image
jacobian (§7.4) needs none of it and produces the quantity the loop consumes.

**11.9 A human-readable text log format.** Rejected — see §8.2. Structured per-action
inputs/outputs in text means inventing a serialization and a parser, and filtering by
run/action means a regex over a format that will change.

**11.10 Simulation as an `if simulate:` branch inside handlers.** Rejected — the most
tempting wrong answer. A sim path that differs from the real path validates only itself.
Driver substitution (§2.5) keeps engine, handlers, plan, events, and logs identical
between modes, which is what makes a green sim run evidence about the real one.

---

## 12. Risks and load-bearing assumptions

**Load-bearing assumptions** (if one is false, a section above needs rework, not a
tweak):

1. **The two cameras respond to LH motion with a well-conditioned, roughly constant 2×3
   jacobian over the working volume.** All of §7 rests on this. It fails if a camera
   views the handover nearly along an axis (that axis becomes unobservable — detectable,
   because the column norm test in §7.5 will drop it) or if the eye-in-hand camera moves
   between calibration and use (**it does — it is on the right arm's flange**). Mitigation:
   the jacobian is measured with the arm parked at `LEFT_ARM_LIQUID_HANDLER_DECK`, that
   waypoint is recorded in `camera_jacobians.json`, and the offset handler logs a
   `WARNING jacobian_pose_mismatch` if the arm is not within tolerance of it. **This is
   the highest-risk item in the design.**
2. **A tag can be placed on the pipette carriage and on the tube/rack.** The primary tip
   and tube detectors are tag-anchored. If tags are not acceptable on those parts, both
   fall back to the classical detectors, and §7's confidence numbers drop across the
   board.
3. **The LH accepts small relative moves and acknowledges them.** The transport is a stub
   today (`GAP_ANALYSIS.md` §1.3). If the OT-One's control path turns out not to support
   relative moves, `LHRelative` becomes read-position → absolute-move, which is a
   handler change plus a dependency on absolute feedback the driver currently
   dead-reckons. §6 is written so this is a contained change.
4. **A camera subprocess can be started and stopped repeatedly on this bench.** The whole
   §4 supervision design assumes restart is a recovery, not a degradation. The
   in-process degradation observed today (`camera_hub.py:28-33`) is evidence *for* this,
   but it has not been measured across process boundaries at rate.
5. **`uvicorn` without `--reload` is acceptable as "the one command".** The camera
   children are the reason. Adoption-at-boot (§4.5) softens it; it does not remove it.
6. **One run at a time, one operator.** The claim mechanism, the single runner thread,
   and the single blackboard all assume this.

**Risks**, with the mitigation already designed in:

| risk | mitigation |
|---|---|
| The servo loop oscillates or stalls (wrong jacobian sign, scale error) | 0.7 damping gain, `clamp_mm=15`, `max_iterations=12`, `no_progress_abort=3` → `servo_stalled`; the sim reproduces the wrong-sign case deterministically |
| Deleting the twin breaks more than expected | The twin's consumers (verification agents, projection overlays, world map, fusion, kinematics) are *all* on the delete list, so the graph is cut cleanly. The only salvage edges are `Transform` → `perception/geometry.py` and `markers.spec_for` → `perception/markers.py`; both are one-file moves |
| A leaked macOS camera process accumulates across restarts | `data/runtime/cameras.json` + boot-time adoption/reaping + an explicit `camera_leaked` ERROR record; the operator can see the count |
| Pause feels broken because it waits for a long move | The UI shows an explicit "pausing…" state and names the action being waited on; `abort` is offered as the immediate alternative |
| The LLM proposes a plausible-looking but wrong action | Propose ≠ apply; every proposal is schema-validated, diff-rendered, human-applied, and logged with the original message; the manual form path exists so the LLM is never load-bearing |
| Waypoint renaming (`LEFT_ARM_*` on the right arm) causes a wrong move on the bench | Names are opaque, device-scoped data; the pre-flight lists every `(device, waypoint)` pair the plan will visit *with the device that will execute it*, so the mismatch is visible before anything moves |
| Recorded sessions have `intrinsics: null` and no depth, so replay cannot exercise depth paths | `scale_source` degrades to `jacobian`/`tag`; the depth path is exercised by `MockTagCameraDriver` (flat plane + plausible K), which already exists for exactly this |
| Only ~30 % of the required waypoints are taught (8 poses, none of the 15 names, no `HOME`) | `TeachChecklist` is driven off the plan's referenced waypoints (the mechanism `/api/workflow/required_poses` already proves), so untaught waypoints are a visible checklist, and pre-flight refuses the run |
| 64 MiB × 6 of JSONL plus 50 runs of JPEGs fills a laptop disk | Bounded by construction: rotation ceiling 320 MiB, run retention 50 with prune-at-boot logging what it removed |

---

## 13. Open questions

* **Q-WP-1** — Are steps 14–18's `LEFT_ARM_*` waypoint names a typo, or is the *acting
  device* the typo? The design is indifferent (names are opaque, device-scoped data),
  but the bench must decide before teaching them, because teaching them under the wrong
  device id is the one version of this that moves the wrong arm. (Raised as Q3 in
  `GAP_ANALYSIS.md` §2.4.)
* **Q-SUBPROC-1** — The brief lists arm and LH controllers alongside the cameras as
  "subprocesses that quit cleanly". This design ships subprocess isolation for cameras
  only (that is where the uninterruptible-open hazard is) and reserves the seam: the
  isolation boundary is a *driver type* (`camera_proc`), so an `xarm_proc` child is the
  same pattern later. Is camera-only isolation acceptable for v2? Putting a blocking
  multi-second `move_joints` behind local HTTP needs an out-of-band abort channel and
  generous timeouts, which is real work for a benefit (crash isolation of a pure-Python
  SDK) that has not been observed to be needed.
* **Q-DEV-1** — "Deviation below threshold" (step 19): this design reads the loop's
  *termination* metric as `magnitude_mm` (the offset that remains) and `deviation_mm`
  (two-view disagreement) as the *trust* gate, requiring both. If the intent was that
  `deviation` means the residual offset itself, only the `until` condition changes.
  Confirm.
* **Q-TIP-1** — May a fiducial sticker be placed on the pipette carriage and on the
  tube/rack? The primary detectors assume yes (assumption 2 above).
* **Q-PATH-1** — Dense joint-path recording (`teach_paths`, `path_teach`, RDP thinning,
  `PathTeach.vue`, ~415 LOC, 1 recorded path on disk) is deleted here on the grounds
  that `arm.traverse` over named waypoints covers the brief. If any bench move is only
  reachable as a dense recorded path, that call must be reversed before deletion.
  (Raised as Q7 in `GAP_ANALYSIS.md` §6.)
* **Q-LH-1** — Does the OT-One's available control path accept relative moves and
  acknowledge them, and does anything report absolute position (`M114` or equivalent)?
  Determines whether §6's dead-reckoning is a permanent property or a stopgap.
* **Q-SIM-1** — Should `HZ_SIM` default to `all` (a fresh clone runs the whole workflow
  with no hardware and no env file) or to `none` (the bench machine is never surprised
  by a simulated arm)? This design defaults to `none` with a loud `SIM` badge whenever
  any device is simulated, but the demo case argues the other way.
