# Requirements — reduced-scope lab cell (v2)

Status: **draft for review.** Date 2026-07-26. Supersedes `PROJECT_PLAN.md` and the target scope in
`docs/ARCHITECTURE.md`.

Companion documents:
- [GAP_ANALYSIS.md](GAP_ANALYSIS.md) — what exists today, with file:line references
- [ARCHITECTURE.md](ARCHITECTURE.md) — the proposed design
- [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md) — adversarial review of that design
- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) — the phased plan

**Reading guide.** §1–2 set scope. §3–11 are the numbered requirements; each has an ID (`R-<area>-<n>`)
and is written to be testable. §12 is non-functional. §13 is explicitly out of scope. §14 lists
prerequisites that are **not** software and that gate §9. §15 is the open questions — **please answer
Q1–Q9**; the highest-impact ones are Q1, Q2 and Q4.

Priority tags: **[M]** must — the system is not deliverable without it. **[S]** should — deliverable
but degraded. **[C]** could — take it if it is free.

---

## 1. Purpose and one-line scope

A single operator, at one bench, runs a 20-step cooperative uncap-and-present workflow across two
6-axis arms and one liquid handler, watching it execute action-by-action in a browser, able to pause,
inspect, and inject a corrective step at any point — and able to do all of that on a laptop with no
hardware attached.

The system is **deliberately smaller** than what exists today. §13 lists roughly 40 % of current
application code that is being removed.

## 2. Actors and interfaces

| Actor | Interface |
|---|---|
| **Operator** | Vue frontend: workflow tab, teach tools (2 arms + liquid handler), camera tab, logs tab |
| **Engine** | Backend service; executes an indexed plan of actions against the controllers |
| **Agent** | LLM assist reachable *only* from the inject button; proposes an action, never applies one |
| **Controllers** | 2× xArm Lite 6, 1× Opentrons OT-One, 4× cameras |

Hardware inventory (from `core/config.py:45-67`): arms `left` (192.168.3.11) and `right`
(192.168.3.13); liquid handler `ot`; cameras `gripper_cam` (right flange), `gripper_left_cam` (left
flange), `overview_cam` (fixed), `handover_cam` (fixed, sees the OT deck).

---

## 3. Startup and process model

| ID | Pri | Requirement |
|---|---|---|
| **R-START-1** | M | Exactly **one command** starts the whole backend: the API, the device manager, the four camera controllers, and the initialization procedure. `just backend`. No second terminal, no manual connect step. |
| **R-START-2** | M | Exactly **one command** starts the frontend: `just frontend`. |
| **R-START-3** | M | A **third command starts the whole system in simulation** with no hardware present: `just sim`. This is the documented first-run command and the command CI uses. |
| **R-START-4** | M | The backend runs the four camera controllers as **separate OS processes**, one per camera, not as in-process threads. |
| **R-START-5** | M | On normal shutdown (SIGINT/SIGTERM to the backend) every camera process **exits cleanly** — releases its device and terminates — within a bounded time (target ≤ 5 s), and the parent reports any child that had to be escalated to SIGKILL. No orphaned processes, no device left open. |
| **R-START-6** | M | If a camera process dies unexpectedly the supervisor detects it, records it in the log, and marks that camera **unavailable** to the engine. Restart is attempted at most a bounded number of times; a camera that will not stay up must be visibly unavailable rather than silently absent. |
| **R-START-7** | S | The backend starts and serves the API **even if every device fails to connect.** A dead camera or an unreachable arm degrades that device, never the service. (This already holds today and must not regress.) |
| **R-START-8** | M | Backend start must not be able to hang on a camera. Rationale: opening a UVC device on macOS can block in an uninterruptible kernel wait that `kill -9` cannot reap ([startup_snapshot.py:12-15](../../backend/app/services/startup_snapshot.py#L12-L15)) — this is the *reason* R-START-4 exists, because a process can be killed and a thread cannot. |

## 4. Initialization

| ID | Pri | Requirement |
|---|---|---|
| **R-INIT-1** | M | Initialization **discovers** the configured devices, attempts to connect each, and records a per-device outcome. One device failing must not stop the others. |
| **R-INIT-2** | M | Per-device init routines run after connect: **cameras** — capture and store one frame (with settle frames discarded); **arms** — enable all axes and clear latched faults; **liquid handler** — move each axis a small amount in both directions and return, then **retract Z fully up**. |
| **R-INIT-3** | M | After per-device init, each arm moves to its **home position slowly** (the `slow` speed tier, R-ENG-14). |
| **R-INIT-4** | M | If an arm has **no home position defined**, the system must **emit a warning** — visible in the log *and* in the frontend readiness panel — and skip that arm's home move. It must not move the arm to a guessed home, and it must not fail initialization. |
| **R-INIT-5** | M | Initialization is **observable**: it is itself an indexed plan rendered in the frontend with per-step state, so the operator can see which device is being initialized and what failed. |
| **R-INIT-6** | M | The engine exposes a **readiness state** (`initializing` / `ready` / `degraded` / `failed`) and the list of warnings. The workflow **Start** control is enabled per R-ENG-2. |
| **R-INIT-7** | M | `initialize`/`reinitialize` is available as an **engine action**, for all devices or one named device, so re-initialization mid-session is the same code path as boot (R-ENG-3). |
| **R-INIT-8** | S | Initialization must be fully exercisable in simulation, including the "no home defined" warning path. |

## 5. Execution engine — general

| ID | Pri | Requirement |
|---|---|---|
| **R-ENG-1** | M | One engine, one plan model, one event stream. The three overlapping executors that exist today (hero workflow, sequences, agent skill loop — see [GAP_ANALYSIS §2.1](GAP_ANALYSIS.md)) are replaced by it. |
| **R-ENG-2** | M | The engine begins the workflow when the operator presses **Start** in the workflow tab, and only then. Start is refused with a stated reason when readiness is `failed`; when readiness is `degraded` Start requires an explicit operator confirmation naming the degradations. |
| **R-ENG-3** | M | The engine can reach every controller: arm motion, liquid-handler motion, camera snapshot, camera video stream. |
| **R-ENG-4** | M | A plan is an **ordered list of actions**. Every action has a **stable identity** and a **displayed index**. Injection may renumber indices; it must never change an action's identity or lose a completed action's recorded outputs. |
| **R-ENG-5** | M | Every action has a **state**: `planned` → `running` → `complete`, plus `failed`, `skipped`, `aborted`. State transitions are pushed to the frontend. |
| **R-ENG-6** | M | Every action records **typed inputs and typed outputs**. Outputs include, per action kind: the images captured, the analysis results (tip identified, tube identified), the offsets calculated, and any produced artifact files. |
| **R-ENG-7** | M | Every action's **log records are attributable to it**, so the frontend can show "the logs this action produced" (R-UI-8). |
| **R-ENG-8** | M | **Pause** is available between any two actions, from a single control. After a pause request the engine finishes the action in flight and stops at the next boundary; the frontend must show the intermediate `pausing` state honestly rather than claiming an instant stop. |
| **R-ENG-9** | M | **Resume** continues from the paused position. |
| **R-ENG-10** | M | **Abort** is distinct from pause and stops the run. Rationale for keeping them separate: a mid-trajectory hard stop is a different, riskier operation than a boundary pause. |
| **R-ENG-11** | M | The operator's **emergency stop must remain reachable at all times**, including while a run is in flight and while an action is blocking. Any device-claim or locking scheme introduced by the engine must exempt it. (Today `/api/arms/{id}/stop` deliberately bypasses the teach lock — [api/teach.py:17-19](../../backend/app/api/teach.py#L17-L19) — and that must not regress.) |
| **R-ENG-12** | M | **Inject**: the operator can add an action to the plan as the next step. Injecting pauses the run if it is running, and resumes once the injection is accepted. Any engine action kind may be injected. |
| **R-ENG-13** | M | Injection must be safe against the run loop: it cannot corrupt the plan, cannot silently displace the action currently executing, and cannot land in the past. Where the operator's intent cannot be honoured exactly, the engine refuses with a stated reason rather than inserting somewhere else. |
| **R-ENG-14** | M | Motion actions take a **named speed tier** — `fast` / `medium` / `slow` — resolved centrally to per-device linear/angular speeds and clamped by the existing soft limits ([core/config.py:23-29](../../core/config.py#L23-L29)). Raw speed numbers must not appear in plan data. |
| **R-ENG-15** | M | A failed action stops the run at that action with the failure recorded, and leaves the plan inspectable and injectable so the operator can recover. Retries, where allowed, are bounded and recorded per attempt. |
| **R-ENG-16** | S | The engine **pre-flights** a plan before the first motion: every referenced waypoint exists, every referenced device is present, every joint target is inside the soft limits. Rationale (existing, retained): discovering at step 7 that a waypoint was never taught leaves an arm holding an open tube in mid-air. |
| **R-ENG-17** | M | A plan step that cannot run must **report that it cannot run**, never be skipped silently. A silently skipped step reads downstream as "it ran". |
| **R-ENG-18** | S | The event protocol must let a frontend that reconnects mid-run **reconstruct the complete current state** (plan, per-action state, outputs so far) without having observed the earlier events. |

## 6. Engine actions — arms

| ID | Pri | Requirement |
|---|---|---|
| **R-ARM-1** | M | `move to home` — move to the arm's defined home waypoint at a given speed tier; warn and refuse if undefined. |
| **R-ARM-2** | M | `move to waypoint` — move to a **named** waypoint, with optional **x/y/z offsets, default 0**. |
| **R-ARM-3** | M | `move_relative` — move by x/y/z (and optionally orientation) offsets. |
| **R-ARM-4** | M | `gripper close` and `gripper open`. |
| **R-ARM-5** | M | `decap` — a **360° rotation of the tool axis in 90° steps**, rewinding the wrist back by the same amount between steps. Net wrist travel must be zero and the rotation must be commanded in **joint space** on the tool axis, not as a cartesian yaw. The whole sequence must be pre-flighted against joint soft limits before the first step, because the intermediate angles exceed the endpoints. (This generalizes `core/motion/cap_ops.py`, which does the same ratchet in 180° bites.) |
| **R-ARM-6** | M | `traverse to` — move through an **ordered set of named waypoints** as one action, blended where the controller supports it, reporting progress per waypoint. |
| **R-ARM-7** | M | `re-connect`, `re-enable`, `re-engage` as recovery actions. **See Q5** — "re-engage" needs a definition. |
| **R-ARM-8** | M | `start manual move` / `stop manual move` — enter and leave hand-guiding (free-drive) mode. Programmed motion must be impossible while active, and the action must guarantee the mode is left. |
| **R-ARM-9** | M | Waypoint names are **device-scoped data**, never code. Adding or renaming a waypoint must not require a code change. |

## 7. Engine actions — liquid handler

| ID | Pri | Requirement |
|---|---|---|
| **R-LH-1** | M | `move by x, y, z` — a **relative** move of the pipette head, in mm. This is the action the servo loop drives (R-VIS-6), so it is the single most load-bearing liquid-handler capability. |
| **R-LH-2** | M | `initialize` — move each axis a small amount in both directions and back, then **retract Z up**. |
| **R-LH-3** | M | Every commanded move is **clamped to a configured envelope** and refuses rather than clamps silently when a request would leave it. |
| **R-LH-4** | M | The driver must report **how it knows its position** (measured vs dead-reckoned) so downstream consumers cannot mistake an assumption for a reading. |
| **R-LH-5** | M | The liquid-handler driver's **wire protocol must be testable without the instrument**: a loopback transport that asserts the exact commands emitted. Rationale: today `OpentronsDriver.connect()` assigns a dummy object and `_send()` returns `None`, so **every liquid-handling call is a no-op that reports success** ([drivers/opentrons/driver.py:33-55](../../drivers/opentrons/driver.py#L33-L55)) — the most dangerous failure shape in the repo. |
| ~~R-LH-6~~ | — | ~~Pipetting operations should be dropped from the capability interface.~~ **WITHDRAWN (Q7).** The pipetting interface stays untouched: `aspirate`, `dispense`, `pick_up_tip` and `drop_tip` remain on the capability ABC. Phase 5 only *adds* relative XYZ, initialize and retract-Z. |

## 8. Engine actions — cameras

| ID | Pri | Requirement |
|---|---|---|
| **R-CAM-1** | M | `take snapshot` — capture and store one frame from a named camera, returning the stored artifact reference. |
| **R-CAM-2** | M | A snapshot requested by the engine must be **newer than the motion that preceded it**. A cached or queued pre-move frame is the failure that makes a servo loop never converge, so freshness must be enforced by an explicit mechanism and the frame's capture time must be reported in the action's outputs. |
| **R-CAM-3** | M | `search code continuously` — watch a camera for a given AprilTag/ArUco **marker ID** until found or a timeout expires; report found/not-found and where. |
| **R-CAM-4** | M | Live **video streaming** from every camera, consumable by the frontend concurrently with engine snapshots — one owner of each device, many consumers of its frames. |
| **R-CAM-5** | M | Marker detection covers **AprilTag `tag36h11`** (already bench-tuned, [core/perception/fiducials.py:60](../../core/perception/fiducials.py#L60) — do not re-derive those parameters) and **at least one ArUco dictionary**, selectable by configuration. |

## 9. Computational (vision) actions

> **These requirements are gated on the bench prerequisites in §14.** As measured on 2026-07-26, the
> two cameras the target workflow names for this loop do not currently produce imagery that supports
> it: `gripper_cam` is aimed across the room and detects zero AprilTags in 53 sampled frames, and
> `handover_cam`'s most recent session is greyscale IR with the structured-light dot projector on.
> See [GAP_ANALYSIS §3](GAP_ANALYSIS.md) and [ARCHITECTURE_REVIEW B1](ARCHITECTURE_REVIEW.md). The
> software below is buildable and testable in simulation regardless; it cannot be *validated* until
> §14 is met.

| ID | Pri | Requirement |
|---|---|---|
| **R-VIS-1** | M | `identify tip` — locate the pipette tip and report the **bottom of the tip** in image coordinates, from the right-gripper camera and from the liquid-handler handover camera. Must report a per-detection quality score and must **report absence honestly** rather than returning a low-confidence guess. |
| **R-VIS-2** | M | `identify tube` — locate the tube and report the **top of the tube** in image coordinates, from the same two cameras. May use an ArUco/AprilTag marker as the anchor where one is available, with a classical-CV fallback. Same honesty requirement as R-VIS-1. |
| **R-VIS-3** | M | `calculate offset` — compute the offset between tip-bottom and tube-top in **x, y, z (mm)** from the two viewpoints, and report **confidence** and **deviation**. |
| **R-VIS-4** | M | The confidence and deviation metrics must be **defensible and derived from the observed data**, not assigned constants. Specifically: (a) the metric must degrade when an axis is poorly observed from the available views; (b) it must degrade when a detection is missing or low quality; (c) it must not be structurally incapable of reporting a problem. Two separately meaningful numbers are required — the magnitude of the remaining offset, and an estimate of its uncertainty — and they must not be conflated. See **Q4**. |
| **R-VIS-5** | M | `create overlay` — render the distances/offsets onto both source images and **store the processed images** as retrievable artifacts, linked to the action that produced them. |
| **R-VIS-6** | M | **The servo loop**: repeat { snapshot both cameras → identify tip on each → identify tube on each → calculate offset + confidence + deviation per view → render an overlay per view → select the offset with the higher confidence → move the liquid handler by that offset } until the deviation is below a configured threshold. |
| **R-VIS-7** | M | The loop is **bounded**: a maximum iteration count, a per-iteration move clamp, and a no-progress detector. It must terminate with a clear outcome — converged, stalled, or aborted — never spin. |
| **R-VIS-8** | M | Every iteration of the loop appears in the plan as **indexed actions with their own outputs and logs**, so the operator can inspect what the loop saw on iteration 4. |
| **R-VIS-9** | M | The loop's convergence criterion must be the **remaining offset**, not the disagreement between views. Gating termination on inter-view disagreement can prevent a converged loop from ever terminating. |
| **R-VIS-10** | M | Whatever per-camera calibration the offset solve depends on must be **bound to a verifiable camera identity** (serial or equivalent fingerprint) **and resolution**, and must **refuse to run** on mismatch rather than warn. Rationale: the same fleet id has been observed pointing at two different physical viewpoints at two different resolutions across sessions three hours apart; a silent 2× resolution change is a silent 2× gain error in the loop. |
| **R-VIS-11** | S | `move liquid handler up (Z)` terminates the workflow after the loop converges. |

## 10. Simulation

| ID | Pri | Requirement |
|---|---|---|
| **R-SIM-1** | M | **Every** action is executable in simulation. The workflow runs end-to-end, including the servo loop, with no hardware attached. |
| **R-SIM-2** | M | Simulation is achieved by **substituting the driver behind a device**, not by branching inside action logic. Rationale: a simulated path that runs different code from the real path validates only itself. |
| **R-SIM-3** | M | Simulation is selectable **per device class**, so cameras can be simulated while real arms are driven, and vice versa. |
| **R-SIM-4** | M | Simulated cameras replay the **existing recorded frames** on disk. §14/Q1 constrains which sessions are usable for which purpose. |
| **R-SIM-5** | M | For the servo loop, simulation must generate imagery in which the **tip↔tube difference shrinks per iteration until the threshold is reached** — and it must shrink *because the commanded liquid-handler moves are actually applied*, not because a counter is ticking. A loop that converges on a script proves nothing; a loop that converges because the moves close it will also fail when the sign is wrong, which is the bug worth catching. |
| **R-SIM-6** | M | Every action's result states **whether it was simulated**, and that determination must be correct for pure-computation actions too (they touch no device, so "no device was mocked" must not be read as "this was real"). |
| **R-SIM-7** | M | The whole test suite runs in simulation and **cannot reach hardware**. |
| **R-SIM-8** | M | Default: simulation **on** (Q8), so a fresh clone runs the full workflow with no configuration and no hardware. Driving real instruments is an **explicit opt-in**. The frontend must state unambiguously, at all times, whether it is looking at simulated or real devices — a simulated run that reads as real is the failure mode this default trades for convenience. |

## 11. Logging

| ID | Pri | Requirement |
|---|---|---|
| **R-LOG-1** | M | Every action logs its **inputs and its outputs**. |
| **R-LOG-2** | M | All log records go to **one shared log file**, including records from the camera subprocesses. One writer to that file. |
| **R-LOG-3** | M | Records are **structured** and machine-readable, and carry enough context to attribute a record to its run and its action. |
| **R-LOG-4** | M | Logs are **viewable in the frontend**: a log view with filtering, and per-action logs surfaced on the action itself (R-UI-8). |
| **R-LOG-5** | M | The logging path is **not the action's responsibility** — an action handler must not be able to forget to log. The wrapper that runs actions does the logging. |
| **R-LOG-6** | M | Replace the current `print()`-only diagnostics (30 sites, zero use of `logging`) with the standard logging framework. Warnings that matter operationally — such as R-INIT-4's undefined home — must be reachable programmatically, not only visible in a terminal. |
| **R-LOG-7** | S | The log file is size-bounded (rotation), and rotation must not break the frontend's view of it. |
| **R-LOG-8** | C | A log record must never contain a full image; images are artifacts referenced by path. |

## 12. Frontend

| ID | Pri | Requirement |
|---|---|---|
| **R-UI-1** | M | **Workflow tab** showing what is happening now and the plan ahead. |
| **R-UI-2** | M | Every action in the plan — **past and future** — shows its **index**. |
| **R-UI-3** | M | Every action shows its **state**: planned / running / complete (plus failed / skipped / aborted). |
| **R-UI-4** | M | Every action shows its **outputs**: images taken, images analyzed, offsets calculated, tip identified, tube identified. |
| **R-UI-5** | M | Actions are rendered as a **chain**, and **expand** to reveal the logs they produced. |
| **R-UI-6** | M | **Pause**, **resume** and **add-input / inject** controls. |
| **R-UI-7** | M | The inject control opens a **chat window** for agent interaction; the agent can propose an action to insert **between given indices**, based on the operator's input. The operator sees the proposal and accepts or rejects it — the agent never applies a change directly. |
| **R-UI-8** | M | Per-action logs are visible from the action (the frontend side of R-LOG-4). |
| **R-UI-9** | M | **Teach tool for both arms.** This largely exists today and is good (jog cartesian/joint, absolute move with preview, gripper incl. width, pose library, cap tools, keyboard bindings) — preserve it. |
| **R-UI-10** | M | **Teach tool for the liquid handler** — XYZ jog, Z retract, run init, save a named waypoint. **This does not exist in any form today**; `liquid_handler` appears in the frontend only as a string in a type union. |
| **R-UI-11** | M | **A camera feed per camera**, with AprilTag/ArUco detection overlays. Exists today and works (MJPEG `<img>` + SVG overlay, clickable detections) — retarget it to the new camera transport, do not rewrite it. |
| **R-UI-12** | M | A **logs view** (R-LOG-4). |
| **R-UI-13** | M | A **readiness panel** showing initialization state and warnings, including the "no home defined" warning (R-INIT-4/6). |
| **R-UI-14** | S | The inject flow must work with **no LLM available**: a schema-driven manual form to build the action. The chat is an accelerator, not the only path. |

## 13. Non-functional requirements

| ID | Pri | Requirement |
|---|---|---|
| **R-NFR-1** | M | Nothing above the driver layer imports a vendor SDK. The capability-ABC boundary in `drivers/capabilities/` is preserved. |
| **R-NFR-2** | M | The whole system is runnable and testable on a laptop with no instruments. No requirement may be designed such that it can only be validated on hardware. |
| **R-NFR-3** | M | Bench knowledge already paid for is **preserved, not re-derived**: the xArm driver's measured joint soft limits and the J5 flange-camera clearance ([core/config.py:34-43](../../core/config.py#L34-L43)), its braking-on-disconnect and gripper auto-detect; the tuned AprilTag detector parameters and their measured justifications. |
| **R-NFR-4** | M | Python ≥3.11, FastAPI, uv, pytest. Vue 3 + Vite + Tailwind. |
| **R-NFR-5** | S | Test suite stays green throughout (baseline: 216 passing) and runs in under ~60 s. |
| **R-NFR-6** | S | Prefer deleting code to keeping it. Net LOC should **fall**. |
| **R-NFR-7** | S | Documentation consolidates from 22 files / 2 537 lines to a small set. |

## 14. Out of scope — to be removed

Removed because the target scope does not reference them. Full dependency analysis in
[GAP_ANALYSIS §6](GAP_ANALYSIS.md); the review independently traced the deletion graph and found it
clean.

- **Digital twin / world model** — scene graph, arm-FK loop, perception→twin fusion, twin→image projection, the top-down world map.
- **Calibration pipeline** — world board, extrinsics solve, hand-eye, orbit scan. Five of its nine steps are `TODO` stubs and it requires a printed board with an unmeasured spacing constant.
- **Twin-based verification agents** — replaced by the image-based offset/confidence actions.
- **The P0 agent skill-selection loop** (`backend/app/agent/`) — the remaining agent role is plan injection, which is a different job.
- **Sequences** (`core/sequences.py`, its API and `SequenceBuilder.vue`) — subsumed by the engine plan plus inject.
- **The hardcoded hero workflow** as an executor — its choreography survives as plan *data*.
- **Pick-and-place planner** and its adapter — no caller in scope.
- **Dense joint-path teaching** (record/RDP-thin/replay, ~415 LOC + a Vue component + 6 routes, for one recorded path) — `traverse to a set of named waypoints` is a different, simpler requirement. **See Q6.**
- **CAD meshes and the twin's labware definitions.**
- **Bench diagnostic scripts** — `validate_camera.py` (992 LOC), `validate_motion.py`, `find_joint_limit.py` (its measured *output* is preserved in config), `gripper_sequence.py`. Keep `record_footage.py`: it produces the simulation fixtures.

Also out of scope: multi-user access, authentication, multi-run concurrency, protocol/experiment
authoring beyond this one workflow, recap-and-return, and metric 3D reconstruction of the scene.

## 15. Prerequisites that are not software

These gate §9 and are **not** fixable in code. They are listed so the schedule reflects them.

**Revised 2026-07-26** after the superseding imagery audit ([GAP_ANALYSIS §3.1](GAP_ANALYSIS.md)).
Both required viewpoints **do** see the handover in colour with a detectable fiducial on the tube
assembly, so P-1/P-2 turn out to be configuration and mounting tasks rather than perception research.
This materially de-risks §9.

| ID | Prerequisite | Evidence |
|---|---|---|
| **P-1** | The two servo cameras must run at **≥1280×720**. At 640×480 the tube marker subtends too few pixels and detection fails; the same frame upscaled 2× detects it. Config only (`CAM_WIDTH_<ID>`/`CAM_HEIGHT_<ID>`, [core/config.py:225-234](../../core/config.py#L225-L234)). | `gripper_left_cam/…065209Z`: 0 tags raw, tag 218 at 2× |
| **P-2** | The servo cameras must deliver **colour**, not RealSense IR with the dot projector on. Both `handover_cam` and `overview_cam` deliver colour before ~07:23 and IR after — stream selection, not a limitation. | 07:11–07:19 colour, 4–6 tags each; 07:23–07:42 IR, none |
| **P-2b** | **Fiducials must be mounted on flat surfaces** — the gripper jaw or a flat tab — **never wrapped around the tube.** Curvature warps the tag and the square-quad fit fails even at ~60 px and clearly legible. | 1280×720 gripper frame: flat table tag 218 detected, curved tube tag not |
| **P-3** | A camera slot must map **stably** to one physical camera at one resolution, or carry a fingerprint that lets software detect a change (R-VIS-10). **Confirmed necessary:** the physical *right*-arm gripper camera is the slot named `gripper_left_cam`, while `gripper_cam` looks across the room. | operator-confirmed slot/device mismatch; cause documented at `startup_snapshot.py:3-8` |
| **P-4** | The **15 workflow waypoints must be taught**, under the correct device, plus a **home per arm**. Today 8 differently-named poses exist and no home for either arm. | `data/teach_poses.json` |
| **P-5** | Extend the tag-size/id registry to the markers actually on the bench: **180, 181, 183, 184, 185, 188, 189, 191, 202, 203, 218, 219, 225, 227**. The bench is *more* densely marked than configured (map lists 180–186, 224). | measured across `temp/captures/` |
| **P-6** | A **working liquid-handler transport**. The driver is a stub that reports success while doing nothing; the real serial work is on an unmerged branch. | `drivers/opentrons/driver.py:33-55` |
| **P-7** | Camera **intrinsics**, if any requirement is to depend on metric image geometry. No recorded session has them (`intrinsics: null` in every manifest) and there are zero depth frames on disk. Not required by the chosen approach (§16 A1). | all three manifests |

---

## 16. Open questions — **ANSWERED 2026-07-26**

All nine are resolved. The answers below are **binding**; the original questions are retained after
the table as the rationale that produced them.

| Q | Answer | Consequence |
|---|---|---|
| **Q1** vision approach | **The cameras do see the handover** — the operator identified the correct frames and the audit confirms it ([GAP_ANALYSIS §3.1](GAP_ANALYSIS.md)). Build the pipeline now against real fixtures **and** the synthetic world; the bench work reduces to P-1/P-2/P-2b/P-3, which are config and mounting. | §9 proceeds in Phase 7 as planned, at materially lower risk. Use `temp/captures/handover_cam/20260726T071143_135Z_color.png` and `temp/captures/gripper_left_cam/20260726T071850_224Z_color.png` as the **reference fixtures** — they are the ground truth the detectors are written against. |
| **Q2** fiducials | **Both allowed** — tag on the rack/tube assembly **and** on the pipette carriage. | Largest single de-risking. Tip detection becomes a tag read (primary) with the classical detector as fallback; tube detection likewise. Subject to **P-2b**: flat mounting only. |
| **Q3** waypoint names | **Drop the device prefix**; store under the acting device (`TRANSITION_MID_TABLE`, `LIQUID_HANDLER_DECK`, …). | Resolve before any pose is taught (Phase 4). |
| **Q4** meaning of deviation | **Terminate on the remaining offset.** Report per-axis uncertainty and inter-view disagreement separately, advisory only. | Confirms D16/D15 in the plan. Delete the per-view "deviation" field, which is structurally always zero. |
| **Q5** "re-engage" | Not separately specified → use the stated assumption: `clear_errors()` → `enable(True)` → verify with a zero-distance move. | R-ARM-7 implemented as three distinct recovery actions. |
| **Q6** dense path teaching | **Delete it.** | ~415 LOC + a Vue component + 6 endpoints removed in Phase 1. Do the one traverse sanity check first. |
| **Q7** pipetting | **Keep it — do not touch the liquid-handler pipetting interface.** | **R-LH-6 is withdrawn.** `aspirate` / `dispense` / `pick_up_tip` / `drop_tip` stay on the capability ABC. Phase 5 *adds* relative XYZ, initialize and retract-Z; it does not remove anything. |
| **Q8** simulation default | **Keep simulation as the default.** | `just backend` comes up simulated; driving real hardware is an explicit opt-in. A fresh clone runs the full workflow with no configuration. Phase 1 item 1.4 changes accordingly — the "real by default" option was **not** taken. |
| **Q9** LLM inject chat | **Manual form first, chat immediately after.** | Phase 8: 8.1 ships the schema-driven form; 8.2 adds the chat. The inject feature is never blocked on an API key. |

Two answers change the plan as written and are reflected in
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md): **Q7** (pipetting stays) and **Q8** (simulation
stays the default).

---

### Original questions and rationale

**Q1 — Vision, given the cameras don't currently see the handover.** [highest impact]
The servo loop (R-VIS-1…11) is the most valuable and least supported requirement: the two named
cameras produce no usable imagery of the handover today (§15 P-1, P-2), and no session has intrinsics
or depth. Which do you want?
- **(a)** Build the vision pipeline now against simulation only, and treat the bench work (re-aim
  `gripper_cam`, force colour streams, teach the waypoints) as a parallel hardware task. Software
  ships testable but unvalidated.
- **(b)** Fix the bench first — one session with both servo cameras aimed at the handover in colour
  — then build against real fixtures. Slower to start, far lower risk of building the wrong detector.
- **(c)** Reduce the requirement: use a **fiducial marker on the rack/tube** as the primary anchor
  and accept a coarser offset, dropping the classical tip/tube detectors to a fallback.

*My recommendation:* **(b) then (a) in parallel** — the bench task is one recording session and it
determines what the detector has to be. Note the review's finding that on the current evidence
`gripper_left_cam` is a better second viewpoint than `gripper_cam` (16/25 frames with a tag vs 0/53).

**Q2 — Is a fiducial marker allowed on the pipette carriage and/or the tube rack?**
This single answer changes the vision work from "tune five magic constants of a classical detector
against imagery that doesn't exist yet" to "read a tag pose". A sticker on the rack is cheap and the
deck-rail tags are already detected in ~72 % of the usable `handover_cam` frames. A sticker on the
moving pipette carriage is more intrusive but would make R-VIS-1 near-trivial.
*Assumption if unanswered:* rack tag **yes**, carriage tag **no** — classical tip detection primary.

**Q3 — Confirm the waypoint names.** The brief names steps 14–18 `LEFT_ARM_TRANSITION_*` and
`LEFT_ARM_LIQUID_HANDLER_*` while the acting device is the **right** arm. Waypoints are keyed by
`(device, name)`, so the prefix is redundant either way — but a pose taught under the wrong *device*
is the version of this mistake that moves the wrong arm.
*Recommendation:* drop the device prefix from the names entirely and store them under the acting
device (`TRANSITION_MID_TABLE`, `LIQUID_HANDLER_DECK`, …). Needs resolving **before anyone teaches a
pose**.

**Q4 — What does "deviation below threshold" mean?** [highest impact]
R-VIS-6 terminates when "deviation is below threshold", and R-VIS-3 asks for "confidence and
deviation". Two different readings:
- **(a)** *deviation = the remaining offset magnitude* — i.e. "the tip is within 1.5 mm of the tube".
- **(b)** *deviation = the disagreement between the two views* — i.e. "both cameras agree".
These are different numbers and only (a) is safe to terminate on: gating on (b) can leave a
perfectly converged loop running forever because the views never agree to within the threshold.
*Recommendation:* report **both**, terminate on (a), and use (b) as an advisory warning only. Please
confirm, and give the numeric threshold and units you want (mm).

**Q5 — Define "re-engage".** R-ARM-7 lists `re-connect`, `re-enable`, `re-engage`. The first two map
to existing driver calls. Is "re-engage" (i) clear latched faults and re-enable the servos after a
collision/limit event, (ii) re-attach the gripper end-effector API, or (iii) something else?
*Assumption if unanswered:* (i) — `clear_errors()` then `enable(True)`, then verify the arm accepts a
zero-distance move.

**Q6 — Keep or delete dense path teaching?** Record-hand-guided-motion / RDP-thin / replay is ~415
LOC plus a Vue component plus 6 endpoints, and `data/teach_paths.json` holds **one** recorded path.
`traverse to a set of named waypoints` (R-ARM-6) is achievable with blended waypoint moves and is
what the brief asks for.
*Recommendation:* **delete**, after one sanity check that a 3–4-waypoint blended traverse gives an
acceptable path shape. Say if the recorded-path workflow is something you actually use.

**Q7 — Does the liquid handler ever need to pipette in this scope?** The workflow ends with "move
liquid handler up (Z)" — no aspirate, no dispense, no tips. R-LH-6 proposes dropping pipetting from
the capability interface entirely. Confirm, or say that aspirate must stay.

**Q8 — Simulation default.** Should `just backend` default to real hardware (and fail visibly with no
devices), with `just sim` as the explicit simulation command?
*Recommendation:* yes — a bench machine silently driving a simulated arm is worse than a laptop
needing one extra word.

**Q9 — Is the LLM inject chat required for the first deliverable, or is a manual form enough?**
R-UI-7 asks for a chat window; R-UI-14 requires the manual path anyway. The LLM half adds a
dependency, an API key, a proposal lifecycle and a diff view on top of a form that already does the
job.
*Recommendation:* ship the manual form in the main phase and the chat immediately after, so the
feature is never blocked on a key. Say if the chat specifically is what you want to demo.

---

## 17. Acceptance criteria

The deliverable is accepted when, **with no hardware attached**:

1. `just sim` brings the whole system up in one command; `just frontend` in one more. Four camera
   processes are running; `Ctrl-C` leaves none behind. (R-START-1…5)
2. Initialization runs visibly as an indexed plan, every device reports an outcome, and an arm with
   no taught home produces a **warning** visible in both the log and the readiness panel — not a
   failure, and not a guessed home move. (R-INIT-1…6)
3. Pressing **Start** runs the full 20-step workflow to completion, including the servo loop, which
   converges because the simulated liquid-handler moves actually close the offset. (R-ENG-2, R-VIS-6,
   R-SIM-5)
4. **Pause** mid-run stops at the next action boundary with an honest `pausing` state; **resume**
   continues; **inject** adds an action as the next step and resumes. (R-ENG-8/9/12)
5. The workflow tab shows every action with its index, state, outputs (including the overlay images
   the servo loop produced) and, on expansion, the log records that action emitted. (R-UI-1…5, R-UI-8)
6. One log file contains every action's inputs and outputs plus the camera subprocesses' records, and
   the frontend log view shows it. (R-LOG-1…4)
7. The liquid-handler teach tool jogs XYZ, retracts Z, runs init and saves a waypoint. (R-UI-10)
8. Every camera tile streams and draws detection overlays. (R-UI-11, R-CAM-4/5)
9. A loopback test asserts the exact wire commands the liquid-handler driver emits for a relative
   move. (R-LH-5)
10. Test suite green, and no test can reach hardware. (R-SIM-7, R-NFR-5)

On hardware, additionally: the workflow runs on the bench once §15 P-1…P-6 are met. That is
explicitly **not** part of the software acceptance criteria, because it is not something software can
deliver.
