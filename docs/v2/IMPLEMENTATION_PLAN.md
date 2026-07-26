# Phased implementation plan — v2

Date 2026-07-26 · Baseline commit `a275723`, backup tag `backup/pre-scope-reduction-20260726`,
216 tests passing.

Reads with: [REQUIREMENTS.md](REQUIREMENTS.md) (`R-*` ids), [GAP_ANALYSIS.md](GAP_ANALYSIS.md),
[ARCHITECTURE.md](ARCHITECTURE.md) (`§n`), [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md)
(`B*` blocking, `N*` non-blocking, `S*` simplification).

---

## 0. Decisions locked before any code

The architecture and its review disagree in places. These are the reconciled decisions. **They are
settled — do not relitigate them mid-implementation.** Everything here is either "the design was
right" or "the review overturned it", with the reason.

### 0.1 Upheld from the design

| # | Decision | Why it stands |
|---|---|---|
| D1 | **Threads, one worker thread executing actions; asyncio only for websocket fan-out.** | The xArm SDK, cv2 and pyserial all block; `move_joints(wait=True)` blocks for seconds and the driver holds no lock of its own. Async buys nothing and adds a failure mode where one forgotten `await` freezes the pause control. |
| D2 | **Simulation by driver substitution, never `if simulate:` inside a handler.** | A sim path that runs different code from the real path validates only itself. (R-SIM-2) |
| D3 | **One shared piece of simulated world state** (the tip↔tube offset) that the mock liquid handler *decrements* and the synthetic camera *renders*. | This is the single best idea in the design. It makes the servo loop converge because the commanded moves close it — so a wrong-sign jacobian becomes a reproducible pytest failure instead of a bench surprise. (R-SIM-5) |
| D4 | **Stable action identity + derived display index; injection targets "after <identity>", never "at index N".** | Lets injection renumber the plan without losing a completed action's recorded outputs. (R-ENG-4) |
| D5 | **Initialization is a plan run on the same engine.** | Gets indices, states, per-action logs and pause for free, and makes "reinitialize a device" literally the boot code rather than a parallel imperative path. (R-INIT-5/7) |
| D6 | **Liquid-handler transport split with a loopback transport asserting exact wire lines.** | The current driver reports success while doing nothing. Exact-line assertions catch precisely that class of bug without the instrument. (R-LH-5) |
| D7 | **JSONL, one writer; camera children log to stdout, the parent re-emits into the single file.** | (R-LOG-2/3) |
| D8 | **Materialize each servo-loop iteration into the flat plan as real indexed actions.** | It is the only way R-VIS-8 / R-UI-2..5 can show "what the loop saw on iteration 4". Must be bounded — see D14. |
| D9 | **Pause is cooperative, at action boundaries, and the UI says `pausing…` honestly.** | Do not let this be "improved" into a mid-trajectory `driver.stop()`. Abort stays the separate hard path. (R-ENG-8/10) |
| D10 | **`vue-router` yes, Pinia no** — singleton `reactive()` stores, which is what the app already does well. |
| D11 | **No metric 3D reconstruction, no learned detector.** | Zero intrinsics and zero depth frames on disk; both were correctly rejected. |
| D12 | **Generalize `cap_ops` 180° → configurable step (90° for decap), keeping net-zero wrist travel and the up-front pre-flight.** | `plan_unscrew` already emits the turn/open/unwind/close ratchet the brief describes; 90° bites make the pre-flight *easier* because peak wrist excursion halves. (R-ARM-5) |
| D13 | **Camera subprocess isolation only; arms stay in-process.** Closed as decided, not open. | The uninterruptible-open hazard is a UVC/AVFoundation property. The arm path is a pure-Python TCP client with no equivalent failure mode, and putting a blocking `move_joints` behind local HTTP would need an out-of-band abort channel and would break the one thing that works well today. |

### 0.2 Overturned by the review

| # | Change | Reason |
|---|---|---|
| D14 | **Bound loop materialization.** Hard cap on total materialized actions; no nested loops. | Nested loop + unbounded `max_iterations` is a quadratic plan-growth path to OOM. (B5) |
| D15 | **Delete the `select_offset` "best single view" strategy.** One weighted solve, plus an explicit `low_observability` **refusal**. | The best-single branch guarantees a stalled loop. (B5b, S12) |
| D16 | **Rework the offset metric.** Report `residual_offset_mm` (the remaining offset — this is what terminates the loop) and, separately, per-axis `sigma_mm` from the solve covariance, plus `view_disagreement_mm` as advisory only. Delete the per-camera "deviation" field. | Per-view the residual is **structurally zero** (2 equations, 2 unknowns after axis restriction), so the old metric could never report a problem, and its confidence factor was identically 1.0. Fused, it is also blind to the correlated failures that matter most. (B2, R-VIS-4/9, Q4) |
| D17 | **Axis-observability gate is an SVD conditioning test on the restricted submatrix, not a column-norm test.** Reject an axis subset unless the smallest singular value clears a floor and the condition number is bounded. Log the singular values. | A 45°-yawed camera passes a column-norm gate at condition ≈ 76, turning a 3 px detection error into ~20 mm of commanded motion — with zero reported deviation. (B2c) |
| D18 | **Per-camera calibration is keyed on a verified camera fingerprint + resolution, and mismatch is a refusal, not a warning.** | The same fleet id has been observed as two different physical viewpoints at two different resolutions three hours apart. A silent 2× resolution change is a silent 2× servo gain error. (B1, R-VIS-10) |
| D19 | **Do not hardcode which cameras the servo loop uses.** Servo cameras come from config; the pair is chosen by measured conditioning. On current evidence `gripper_left_cam` is a better second view than `gripper_cam`. | (B1) |
| D20 | **Frame freshness cannot rely on a sequence number crossing the MJPEG wire — it does not.** Use an explicit request/response snapshot path that returns the child-side capture timestamp, and fail the servo iteration if that timestamp predates the last liquid-handler move. Also drain the capture buffer, since `capture()` is a bare `read()` that can return a queued older frame. | The design's `min_seq` mechanism is unimplementable from the client it reuses, and the "correctness mechanism" was load-bearing. (B3, R-CAM-2) |
| D21 | **The engine must not lock the operator out of e-stop.** Device claims must exempt `/api/arms/{id}/stop`, which deliberately bypasses the teach lock today and carries a "do not fix that" comment. | (B4, R-ENG-11) |
| D22 | **Injection must be able to land at the cursor** (immediately next), because that is exactly where a failure leaves it. Refuse with a reason rather than silently inserting elsewhere. Do not 409 an injection merely because a long move is in flight. | (B6, R-ENG-13) |
| D23 | **Bind the log context on the handler, not the root logger's filter.** | Logger-level filters are not applied to records propagated from child loggers, so every run/action stamp would have been missing and the per-action log drill-down would have returned nothing while looking implemented. (B7) |
| D24 | **The event protocol carries a monotonically increasing sequence number, and there is a snapshot endpoint that returns the full current run state.** | Otherwise a websocket reconnect mid-run cannot reconstruct the UI. (B8, R-ENG-18) |
| D25 | **"Simulated" is determined per action from the resolved simulation configuration, not by inspecting a device's vendor string.** Pure-computation actions in a simulated run report simulated. | Device metadata has no `live` key on real drivers (it would raise), and compute-only actions touch no device — so they would have reported "real" in a fully simulated run. (B10, R-SIM-6) |
| D26 | **Camera restart is not a recovery for the hazard that motivated subprocesses.** A child stuck in an uninterruptible device open cannot be killed either; escalate SIGTERM→SIGKILL, bound the attempts, and mark the camera unavailable. Report escalations. | (B9, R-START-5/6) |

### 0.3 Cuts adopted from the review (scope discipline)

The design added ~35 new files; these cuts remove ~10 modules, 7 action kinds, 2 transports and a
whole persistence layer. All adopted.

- **No new waypoint module** — `core/teach_poses.py` is already device-scoped with the right shape; extend it rather than migrating to a new file and orphaning the 8 taught poses. (S1)
- **No generic reference/blackboard interpreter** — five named slots (`frame`, `tip`, `tube`, `offset`, `selected_offset`). A dotted-path resolver is an interpreter you debug during bring-up. (S2)
- **No condition expression language** — the loop has one termination test; make it a named predicate with thresholds. It is also the part an LLM is most likely to author wrongly. (S3)
- **Fewer action kinds** (~14, not 21): merge initialize-all / initialize-one; fold "home" into a waypoint move; merge identify-tip / identify-tube into one action with a target; fold overlay rendering into the offset action; **drop the "manual move" plan action** — it is a teach-tab control wearing a plan action's clothes. (S4)
- **No single-step mode** — pause → inject → resume already covers it. (S5)
- **No run-history persistence layer** — the JSONL already carries every action result. (S6)
- **No artifact hash/size/dimension metadata** — nothing consumes it. (S7)
- **Simplest possible marker-search action** — one marker id, one timeout. (S8)
- **No child-process registry file / boot-time adoption** — a `just reap` recipe. (S9)
- **No log-follow websocket** — polled `GET` with a cursor is ~15 lines; a follower plus rotation-inode handling is ~150 whose failure mode is invisible until you need the log. (S10)
- **Defer the LLM half of inject**; ship the schema-generated manual form first (R-UI-14). (S11, Q9)
- **Delete dense path teaching** after one sanity check (Q6). (Q-PATH-1)
- **Delete the redundant current-action card** — it is the plan chain's row at the cursor. (S13)

### 0.4 Answers to the open questions — **all resolved 2026-07-26**

Full text in [REQUIREMENTS.md §16](REQUIREMENTS.md). Binding decisions:

| Q | Answer | Effect on this plan |
|---|---|---|
| **Q1** vision approach | The cameras **do** see the handover; the bench work is config + mounting, not perception research | Phase 7 proceeds against **real reference fixtures** as well as the synthetic world. Risk table updated |
| **Q2** fiducials | **Both** rack/tube **and** pipette carriage tags allowed | D27 below. Largest single de-risking of Phase 7 |
| **Q3** waypoint names | Drop the device prefix; store under the acting device | Phase 4 item 4.6, before any pose is taught |
| **Q4** deviation | Terminate on the **remaining offset**; uncertainty and inter-view disagreement are reported separately, advisory | Confirms D15/D16 as written |
| **Q5** "re-engage" | `clear_errors()` → `enable(True)` → verify with a zero-distance move | Phase 2 item 2.7 as written |
| **Q6** path teaching | **Delete** | Phase 1 item 1.5 as written |
| **Q7** pipetting | **Keep it — do not touch the pipetting interface** | **Changes Phase 5.** See D28 |
| **Q8** simulation default | **Simulation stays the default** | **Changes Phase 1.** See D29 |
| **Q9** LLM chat | Manual form first, chat immediately after | Phase 8 items 8.1 then 8.2 as written |

### 0.5 Additional locked decisions from those answers

| # | Decision | Consequence |
|---|---|---|
| D27 | **Fiducial-first vision.** A flat tag on the pipette carriage and a flat tag on the tube/gripper assembly are the **primary** signal for tip-bottom and tube-top; the classical detectors become a **fallback**, not the main path. | Phase 7's highest-risk item drops from "tune five constants of a classical detector" to "read two tag poses, with a fallback". Both detectors still ship, because a tag can be occluded by the jaws mid-approach. **Hard constraint: flat mounting only** — a tag wrapped on the cylindrical tube is not detected even at ~60 px and clearly legible (measured). |
| D28 | **The liquid-handler pipetting interface is not touched.** `aspirate`, `dispense`, `pick_up_tip`, `drop_tip` stay on the capability ABC exactly as they are. Phase 5 is purely **additive**: relative XYZ, initialize, retract-Z, envelope limits, transport split. | Reverses review cut S4's liquid-handler element and withdraws R-LH-6. Slightly larger ABC than the minimum this workflow needs — accepted deliberately, because the interface is stable and removing it would be a breaking change for no gain. |
| D29 | **Simulation is the default.** `just backend` comes up simulated; real hardware is an explicit opt-in. | Reverses the review's answer to its own Q-SIM-1. A fresh clone runs the whole workflow with zero configuration, which is the primary demo path. **The price:** a simulated run must never be mistakable for a real one, so the frontend must show device-reality state persistently and unmissably, and `ActionResult` must carry it per action (D25). Treat that indicator as a correctness requirement, not chrome. |
| D30 | **The two operator-identified frames are the vision reference fixtures**, committed as test data: `temp/captures/handover_cam/20260726T071143_135Z_color.png` (tag 225 detected on the assembly, pipette descending, deck tags visible) and `temp/captures/gripper_left_cam/20260726T071850_224Z_color.png` (eye-in-hand, tip silhouetted, tube rim as a bright ellipse). | Phase 7's detectors are written against these, so "works on real imagery" is a pytest assertion from day one rather than a bench discovery. Note `temp/` is gitignored — copy them into a tracked fixtures directory. |

---

## 1. Sequencing rationale

Four constraints set the order:

1. **Logging is first** because R-LOG-5 makes the action wrapper the thing that logs. If logging
   arrives after the engine, every handler gets retrofitted and some get missed.
2. **Deletion is second, not last.** Removing 40 % of the surface before building on it means the
   engine is written against a small codebase. Deleting afterwards means writing adapters to code
   that is about to go.
3. **The engine before the frontend**, because the frontend's whole job is rendering the engine's plan
   and event stream. But the engine is testable head­lessly, so the frontend can start as soon as the
   event protocol is frozen (end of Phase 2).
4. **Vision last**, because it is the highest-risk area, it is gated on two open questions and on
   bench prerequisites, and everything else must be demonstrable without it.

Phases 3/4/5 are independent of each other and can run in parallel. Phase 6 needs Phase 2's protocol.

```
P1 foundations ──► P2 engine core ──┬──► P3 camera subprocesses ──┐
                                    ├──► P4 init orchestrator ────┼──► P7 vision + servo loop ──► P8 polish ──► P9 bench
                                    ├──► P5 liquid handler ───────┘
                                    └──► P6 frontend shell + workflow tab
```

---

## Phase 1 — Foundations and the deletion pass

**Goal:** a smaller codebase with real logging and named speed tiers. No new features.

| # | Work | Requirements |
|---|---|---|
| 1.1 | Logging module: stdlib `logging`, JSONL formatter, one file, context bound **on the handler** (D23). Convert all 30 `print()` sites. | R-LOG-2/3/6 |
| 1.2 | Log read API: `GET /api/logs` with a cursor and filters. No websocket (S10). | R-LOG-4 |
| 1.3 | Speed tiers: central `fast`/`medium`/`slow` → per-device linear/angular speeds, clamped by the existing soft limits. | R-ENG-14 |
| 1.4 | Simulation configuration: per-device-class selection, resolved once at boot; `just backend`, `just frontend`, `just real`, `just reap`. **Simulation is the default** (Q8/D29), real hardware an explicit opt-in. Ship the persistent frontend reality indicator alongside it — a simulated run must never read as real. | R-SIM-1/3/7/8, R-START-1/2/3 |
| 1.5 | **Deletion pass** — twin/world model, calibration pipeline, verification agents, world-map viz, projection, fusion, P0 agent loop, sequences, pick-and-place planner, path teaching (Q6), CAD meshes, 4 diagnostic scripts, and their tests and frontend components. Salvage the two edges the review identified: the transform helper and the marker-spec registry, which `fiducials.py` imports. | R-NFR-6, §14 |
| 1.6 | Extend `core/teach_poses.py` in place — home waypoint per device, no new module (S1). | R-ARM-9, R-INIT-4 |
| 1.7 | Docs consolidation: 22 files → this `docs/v2/` set plus a rewritten README. | R-NFR-7 |

**Exit criteria**
- One log file receives structured records from the API, the device manager and a test action; `GET /api/logs` returns them filtered.
- `just sim` and `just backend` both boot; the test suite is green and demonstrably cannot open a device.
- Net LOC has fallen. Deleted modules have no importers (verified by a grep gate in CI, not by eye).
- **Risk:** the deletion pass is the one step that can quietly break something not covered by tests. Do it as several small commits, one subsystem each, test suite green after every one.

---

## Phase 2 — The action model and engine core

**Goal:** the engine runs a plan headlessly, with pause/resume/inject, against mock drivers. No UI, no
cameras-as-subprocesses, no vision.

| # | Work | Requirements |
|---|---|---|
| 2.1 | Action model: discriminated union (~14 kinds, S4), typed outputs, registry + dispatch. Five named blackboard slots (S2). No condition language (S3). | R-ENG-6 |
| 2.2 | The action wrapper: timing, retry, artifact recording, event emission, and logging — so a handler cannot forget to log (R-LOG-5), and simulated-ness is decided here from the resolved configuration (D25). | R-LOG-1/5, R-SIM-6 |
| 2.3 | Plan: stable identity + derived index, mutation, bounded loop materialization (D14). | R-ENG-4, R-VIS-8 |
| 2.4 | Runner: one worker thread (D1), cooperative pause gate at action boundaries plus inside decap steps and loop iterations (D9), resume, abort. | R-ENG-8/9/10 |
| 2.5 | Injection: target "after <identity>", must be able to land at the cursor, refuse with a reason rather than relocate (D22). | R-ENG-12/13 |
| 2.6 | Event protocol with a sequence number, plus a **full-state snapshot endpoint** for reconnect (D24). Freeze this — Phase 6 builds against it. | R-ENG-5/18 |
| 2.7 | Arm handlers: home, waypoint (+offsets), relative, grip/release, decap (D12), traverse, reconnect/re-enable/re-engage (Q5). Device claims that **exempt e-stop** (D21). | R-ARM-1…8, R-ENG-11 |
| 2.8 | Pre-flight: waypoints exist, devices present, joint targets inside soft limits; a step that cannot run says so (R-ENG-17). | R-ENG-16/17 |
| 2.9 | The 20-step workflow as **plan data**, with the servo loop as a bounded loop construct. Waypoint names per Q3. | R-VIS-6 |

**Exit criteria**
- The full 20-step plan runs end-to-end against mock arms and a mock liquid handler, with the vision
  actions stubbed to a scripted convergence — proving the plan shape, the loop construct and the event
  stream before the detectors exist.
- Pause during a multi-second mock move stops at the next boundary and reports `pausing` in between.
- Injection at the cursor, after a completed action, and after the last action all work; injection
  into the past is refused with a reason.
- Killing and reopening the event consumer mid-run reconstructs the full plan and per-action state
  from the snapshot endpoint.
- A test asserts the plan cannot grow without bound under a loop that never converges.

---

## Phase 3 — Camera subprocesses

**Goal:** four camera processes, supervised, with the frontend streaming from them and the engine
taking fresh snapshots.

| # | Work | Requirements |
|---|---|---|
| 3.1 | The camera child process: owns one device, pumps frames, runs detection next to the pixels, serves stream + snapshot + detections + health + shutdown. | R-START-4, R-CAM-4 |
| 3.2 | Supervisor: spawn, health, bounded restart, mark-unavailable, SIGTERM→SIGKILL escalation with reporting (D26). `just reap` instead of a pid registry (S9). | R-START-5/6, R-CAM-4 |
| 3.3 | **Snapshot freshness** (D20): an explicit request/response path returning the child-side capture timestamp; drain the capture buffer; the engine's servo iteration fails if the timestamp predates the last liquid-handler move. | R-CAM-2 |
| 3.4 | Client half: generalize the existing remote-camera driver. Note the review's correction — it has no sequence concept, no snapshot-over-HTTP path, and a 5 s staleness bound, so this is real work, not a rename. | R-CAM-4 |
| 3.5 | Replay camera driver: plays a recorded session per its manifest. Restrict which sessions are usable per the imagery audit (four of nine folders are IR). | R-SIM-4 |
| 3.6 | ArUco dictionary support alongside AprilTag `tag36h11`, selectable by config. **Do not touch the tuned detector parameters.** | R-CAM-5, R-NFR-3 |
| 3.7 | Camera handlers: snapshot, search-code (one marker, one timeout — S8). | R-CAM-1/3 |
| 3.8 | Child logs flow to the parent's single log file (D7). | R-LOG-2 |

**Exit criteria**
- Four processes visible in `ps` under `just sim`; `Ctrl-C` leaves none; a `kill -9` of one child is
  detected, logged, restarted within the bound, and marks the camera unavailable when it will not stay up.
- Two concurrent stream consumers plus an engine snapshot on the same camera, no interference.
- A test proves a snapshot taken after a simulated move is not the pre-move frame.
- Existing camera-tab behaviour is unchanged from the operator's point of view.

---

## Phase 4 — Initialization orchestrator

**Goal:** one command brings the system to a stated readiness, visibly.

| # | Work | Requirements |
|---|---|---|
| 4.1 | Discovery + connect with per-device outcomes; the service comes up regardless (R-START-7). | R-INIT-1 |
| 4.2 | Per-device init routines: camera snapshot with settle frames (reuse the existing startup-snapshot logic — it exists for a documented reason); arm enable + clear faults; liquid-handler axis wiggle + Z retract. | R-INIT-2 |
| 4.3 | Init **as a plan on the engine** (D5), so it is indexed, logged and pausable. | R-INIT-5/7 |
| 4.4 | Slow home move; **warning path** when no home is defined — in the log *and* as structured readiness state, not a `print`. | R-INIT-3/4 |
| 4.5 | Readiness gate and states; Start refused on `failed`, explicit confirmation on `degraded`. | R-INIT-6, R-ENG-2 |
| 4.6 | **Teach the 15 workflow waypoints + a home per arm** — resolve Q3 first. In simulation this is fixture data; on the bench it is P-4. | R-ARM-9, P-4 |

**Exit criteria**
- `just sim` reaches `ready`. With the home waypoint removed from the fixture, it reaches `degraded`
  with a warning naming the arm — and the arm has not moved.
- "Reinitialize device X" is the same code path as boot, provable by test.

---

## Phase 5 — Liquid handler

**Goal:** a liquid-handler capability that can actually be driven and cannot lie about it.

| # | Work | Requirements |
|---|---|---|
| 5.1 | **Extend** the capability interface — additively — with relative XYZ move, initialize, retract Z and envelope limits. **Do not touch the pipetting methods** (Q7/D28): `aspirate`, `dispense`, `pick_up_tip`, `drop_tip` stay exactly as they are. | R-LH-1/2/3 |
| 5.2 | Transport protocol + loopback transport asserting exact emitted lines + a null transport (D6). | R-LH-5 |
| 5.3 | Implement relative moves as **read → clamp → absolute command** where a position readback exists, keeping relative *semantics* at the interface. The servo loop needs relative semantics, never relative commands. | R-LH-1/3 |
| 5.4 | Report position provenance (measured vs dead-reckoned) and warn on drift. | R-LH-4 |
| 5.5 | Mock liquid handler writes the shared simulated world state (D3), so its moves close the servo loop's offset. | R-SIM-5 |
| 5.6 | Refuse rather than silently clamp an out-of-envelope request. | R-LH-3 |
| 5.7 | Liquid-handler teach endpoints (jog, retract, init, save waypoint). | R-UI-10 |

**Exit criteria**
- A loopback test asserts the exact command sequence for a relative move, including the
  absolute/relative mode framing.
- An out-of-envelope move is refused with a stated reason, not clamped.
- The mock's moves measurably reduce the shared simulated offset.
- **Note:** a real transport is prerequisite P-6 and is not in this phase's exit criteria.

---

## Phase 6 — Frontend shell, workflow tab, logs

**Goal:** the operator can watch, pause, resume and inject.

| # | Work | Requirements |
|---|---|---|
| 6.1 | Router + singleton reactive stores (D10); shell with nav; retire the two obsolete components. | R-NFR-4 |
| 6.2 | Plan chain: indexed rows, state chips, expandable per-action detail. Drop the redundant current-action card (S13). | R-UI-1/2/3/5 |
| 6.3 | Action outputs rendering: images, tip/tube results, offsets. | R-UI-4 |
| 6.4 | Per-action logs on expansion. | R-UI-8, R-LOG-4 |
| 6.5 | Run controls: start, pause (with the honest `pausing` state), resume, abort, inject. | R-UI-6, R-ENG-8/9/10 |
| 6.6 | Reconnect handling via the snapshot endpoint (D24). | R-ENG-18 |
| 6.7 | Logs view. | R-UI-12 |
| 6.8 | Readiness panel with warnings. | R-UI-13 |
| 6.9 | Retarget the camera tab to the subprocess endpoints — **do not rewrite it**, the MJPEG-plus-SVG-overlay approach works. Keep the arm teach tab minus path teaching. | R-UI-9/11 |

**Exit criteria**
- A full simulated run is watchable action-by-action, with the servo-loop iterations expanding to show
  their own overlays and logs.
- Refreshing the browser mid-run restores the complete view.
- Acceptance criteria 4, 5 and 6 in `REQUIREMENTS.md` §17 demonstrably pass.

---

## Phase 7 — Vision and the servo loop

**Goal:** the computational actions, honest about their uncertainty.

**Q1/Q2/Q4 are answered**, and the answers substantially de-risk this phase: both required viewpoints
are confirmed to see the handover in colour, fiducials are allowed on **both** the carriage and the
tube assembly (D27), and two real frames are the reference fixtures (D30). Remaining bench
prerequisites are P-1, P-2, P-2b, P-3, P-5 — all configuration or mounting. P-7 (intrinsics) is **not**
required by this approach.

| # | Work | Requirements |
|---|---|---|
| 7.1 | Two fixtures, built first: the synthetic servo camera rendering the shared simulated world (D3), **and** the two committed real frames (D30). Every detector below is written against both from day one. | R-SIM-5 |
| 7.2 | Tip-bottom detection: **carriage-tag-anchored primary** (D27), classical silhouette fallback. Reports a quality score, which path produced it, and **reports absence honestly**. | R-VIS-1 |
| 7.3 | Tube-top detection: **assembly-tag-anchored primary** (tag 225 is already detected on the real handover frame), tube-rim ellipse fallback. Same contract. | R-VIS-2 |
| 7.4 | Per-camera image jacobian: measurement procedure (a small set of jogs), storage keyed on **camera fingerprint + resolution**, load-time **refusal** on mismatch (D18). Runnable end-to-end in simulation. | R-VIS-10 |
| 7.5 | The offset solve: stacked weighted least squares; **SVD conditioning gate** on the restricted submatrix (D17); per-axis uncertainty from the solve covariance; `residual_offset_mm` + `sigma_mm` + advisory `view_disagreement_mm` (D16). Servo cameras from config, not hardcoded (D19). | R-VIS-3/4 |
| 7.6 | Overlay rendering + artifact storage, linked to the producing action. (`annotate()` already exists and has never had a caller — give it one.) | R-VIS-5 |
| 7.7 | The loop: bounded iterations, per-iteration move clamp, no-progress detection, and the three honest outcomes. Terminate on the **remaining offset** (D16, Q4). No best-single-view fallback (D15). Fail the iteration on a stale frame (D20). | R-VIS-6/7/9 |
| 7.8 | Final Z-up move. | R-VIS-11 |

**Exit criteria**
- The loop converges in simulation **because the commanded moves close the offset** — verified by a
  test that inverts one jacobian column's sign and asserts the loop *fails* rather than converging.
- A test asserts an ill-conditioned camera geometry is **refused**, not silently solved.
- A test asserts a mismatched camera fingerprint or resolution is **refused**.
- A missing detection degrades the reported uncertainty rather than producing a confident wrong answer.
- Overlay images appear as artifacts on the right action in the frontend.

- Both detectors report which path produced the result (tag or classical), and a test asserts the
  classical fallback engages when the tag is occluded — the jaws will occlude it mid-approach.

**This phase still carries the project's most technical risk, but it is no longer a research risk.**
The fiducial-first decision (D27) plus two real reference frames (D30) mean the primary path is a tag
read that is *already demonstrated to work on real imagery from both viewpoints*. What remains is
ordinary engineering: the conditioning gate, the uncertainty reporting, the loop's termination
behaviour, and a classical fallback good enough for the occluded case.

---

## Phase 8 — Inject assist and polish

| # | Work | Requirements |
|---|---|---|
| 8.1 | Manual inject form generated from the action schemas — **the required path** (R-UI-14). | R-UI-14 |
| 8.2 | Inject chat: propose-only, server-side, operator accepts or rejects (Q9). Never applies directly. | R-UI-7 |
| 8.3 | Liquid-handler teach UI wired to Phase 5's endpoints. | R-UI-10 |
| 8.4 | README rewrite: the three commands, the simulation story, what the bench needs. | R-NFR-7 |
| 8.5 | Full acceptance pass against `REQUIREMENTS.md` §17. | all |

---

## Phase 9 — Bench bring-up (hardware, not software)

Not schedulable until §15's prerequisites are met. Tracked here so they are not mistaken for software
work.

1. **P-1** — set the two servo cameras to **≥1280×720**. Config only; the tube tag is undetectable at
   640×480 and detectable at 1280×720.
2. **P-2** — force **colour** streams on the servo cameras (they currently flip to RealSense IR with
   the dot projector on after ~07:23). Stream selection, not a physical limitation.
3. **P-2b** — mount the two fiducials (pipette carriage, tube/gripper assembly) on **flat** surfaces.
   A tag wrapped on the cylindrical tube is not detected even when clearly legible.
4. **P-3** — pin each camera slot to a stable identity and resolution, and **fix the slot naming**:
   the physical right-arm camera is currently the slot called `gripper_left_cam`.
5. **P-5** — extend the tag registry to the ids actually on the bench (180, 181, 183, 184, 185, 188,
   189, 191, 202, 203, 218, 219, 225, 227).
6. **P-4** — teach the 15 waypoints plus a home per arm, under the correct device (Q3).
7. **P-6** — a real liquid-handler transport.
8. Measure the image jacobians on the real cameras; confirm the tag-anchored detectors on live frames
   and re-tune the classical fallback.
9. First bench run, at reduced speed tiers, with a hand on the e-stop.

**P-7 (intrinsics) is not required** by the chosen approach and is deliberately not listed as a
blocker.

---

## 2. Risks

Revised after the Q1–Q9 answers and the superseding imagery audit. The top two risks in the first
draft of this plan are **retired**: the cameras do see the handover, and the detectors have real
fixtures.

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| ~~The servo loop cannot be validated because no camera sees the handover~~ | **retired** | — | Both viewpoints confirmed in colour with a detected tag on the tube assembly ([GAP_ANALYSIS §3.1](GAP_ANALYSIS.md)) |
| ~~Detectors tuned on synthetic imagery fail on real frames~~ | **retired** | — | Two real frames are committed fixtures (D30); detectors are written against them from day one |
| **The fiducial is occluded by the gripper jaws mid-approach**, dropping the loop onto its least-tested path at the worst moment | medium | medium | Both detectors ship; a test asserts fallback engagement on an occluded tag; report which path produced each result so the log shows when the fallback carried a run |
| **A simulated run is mistaken for a real one**, now that simulation is the default (D29) | medium | high | Persistent, unmissable frontend reality indicator shipped *with* the default in Phase 1.4, not after; per-action `simulated` flag (D25); treat the indicator as a correctness requirement |
| **A camera slot silently rebinds to a different device or resolution** — observed, not hypothetical | high | high | Fingerprint + resolution binding with **refusal** on mismatch (D18); the slot named `gripper_left_cam` is physically the right arm's camera, so never trust a slot name |
| **The deletion pass breaks something untested** | medium | medium | One subsystem per commit, suite green after each, backup tag already pushed |
| **The liquid handler never gets a real transport** | medium — 6 review cycles of no movement per `PROJECT_PLAN.md` | high | Loopback transport makes everything except the wire testable; the servo loop is demonstrable in simulation without it |
| **Waypoints never get taught** | medium — same history | high | Phase 4 exits on simulated fixture waypoints, so software is not blocked; P-4 is called out as a hardware task with an owner |
| **Pause is expected to be instant** | medium | low | D9 makes the `pausing` state visible in the UI; abort is the separate hard path. Set this expectation in the README |
| **Scope creeps back** | medium | medium | §0.3's cuts are decisions, not suggestions; §14 lists what must stay deleted |

## 3. What "done" means

`REQUIREMENTS.md` §17, all ten criteria, **with no hardware attached**. Bench validation is Phase 9
and is explicitly not part of the software acceptance criteria, because it is not something software
can deliver.
