# OT-One handoff — state as of 2026-07-26

Written to hand this over to a fresh session. Everything below is either verified
on the bench or explicitly flagged as unverified. Read
`docs/OT_ONE_HARDWARE.md` alongside it — that holds the measured numbers.

## Where the work is

    branch:  initial-setup-and-repo-structure   (the team's convergence branch)
    remote:  lukas -> https://github.com/Lbromig/hackathon-zeon.git
    local:   ~/Downloads/hackathon-zeon-poc     (origin = a private mirror)
    NOT:     ~/Downloads/bay-hack is the OLD zeon project, unrelated.
             ~/Downloads/hackathon-zeon is a second, different checkout.

`main` is deliberately stale at `6df89e7`. Never push there.

## Works, verified on hardware

- **All six axes jog**: X, Y, Z, A + plungers B, C. Driver
  `drivers/opentrons/driver.py`, G-code over USB serial to a Smoothieboard
  `v1.0.3`. Zero `TODO`s in it.
- **Continuous multi-axis motion.** `jog_path()` takes multi-axis entries so one
  `G0` can name X, Y and Z together, and queues a whole path before draining once.
  Measured +5 to +6 ms per leg of overhead over an 806 mm 3D sweep.
- **Absolute positioning** via `move_to_machine(X=..., Z=...)`, because the
  position counter survives reconnection and a homed axis therefore has a real
  machine coordinate.
- **Tip pickup** at a measured 53 mm engagement depth.
- **Browser jog UI**: `OpentronsJog.vue` -> `/api/liquid-handlers/{id}/jog`. A
  click moves the gantry. All six axes, plungers capped tighter (3 mm vs 15 mm).
- **Emergency stop** that reports honestly whether bytes reached the board.
- **Envelope measured**: see `docs/OT_ONE_HARDWARE.md` and
  `hardware/ot_one_envelope.json`.

## Blocked, and on what

**`aspirate` / `dispense` / `drop_tip`** — implemented but refuse until the fleet
config carries `plunger_axis` and `plunger_ul_per_mm`. Both are measurements this
unit has never had taken. This is `Q-OT-PLUNGER-1`, and it is the OT half of
Lukas's floor path.

The blocking step needs a human eye: run
`scripts/calibrate_plunger.py --identify`. It nudges B, returns it, then C,
returns it. **Both axes accept the command and complete in the predicted time**,
so the firmware drives both — nothing in software can say which one physically
moved. Whichever visibly moves is `plunger_axis`. Then `--measure` for µL/mm.

**Vision -> motion** — `core/calibration/ot_hand_eye.py` is written and tested
against a mock camera. It needs: a marker on the nozzle, and real observations.
Two environment blockers on this Mac:

- `cv2` cannot open any camera: `not authorized to capture video`. That is macOS
  Privacy & Security -> Camera, granted per-app, then restart.
- The camera is a RealSense D435i. `pyrealsense2` has no PyPI wheel for macOS, but
  the team ships `scripts/build_pyrealsense2_macos.sh` to build it — and
  librealsense needs root here, which is why the team's own config runs all three
  cameras as plain UVC (`camera` type): RGB works, depth and factory intrinsics do
  not.

**A shortcut worth knowing:** the hand-eye fit only needs *paired data*, not
camera access from this process. If another session can report where it sees the
nozzle at two or three known coordinates — and the OT can be parked at any
coordinate on request — `fit_rigid_transform` produces the transform without this
process ever opening a camera.

## The thing that shapes every decision here

**No endstops register on any axis, and there is no current sensing.** M119 was
polled for 18 s while the Z limit switch was pressed by hand and no bit ever
changed. So when an axis reaches a stop, the driver keeps issuing steps, the motor
skips them silently, the move completes in exactly the predicted duration, and the
open-loop counter reports the commanded value. Even an out-and-back loop closes to
0.00 on paper.

**Every software signal says the move was fine.** Motion duration proves a move
ran, never that the path was clear. The operator is the only detector, until vision
becomes one. This is why the servo-correction loop lives in the calibration module:
positioning and verification are the same loop on this machine.

## Gotchas that cost real time

1. **Closing the serial port does NOT stop an in-flight move.** An early version
   raised on timeout and closed the port; the axis kept grinding. Motion paths must
   fire the estop *before* raising. `tour_search_space.py`, `find_envelope.py` and
   the driver all do; **do not drive motion from ad-hoc inline scripts**, which is
   how this was reintroduced once.
2. **A halted board still answers `version` while ignoring every G-code.** So a
   responsiveness check does not prove it will accept commands. `connect()` now
   sends `M999` unconditionally.
3. **`M400` is the real "arrived" signal.** `G0` and `G28.2` ack on *queue*. An ack
   timeout on the move itself detects nothing.
4. **Long queued paths need a long ack budget.** Smoothieware stops reading serial
   once the planner fills, so a later `G0`'s ack can arrive many seconds late. A
   flat 20 s budget aborted a healthy 150 mm sweep and fired the estop mid-run.
5. **Never mix a small axis move into a long one.** A 6 mm Z inside a 150 mm X leg
   makes Z crawl at 25:1 and the steppers growl. Give each axis its own leg.
6. **Do not raise acceleration.** Pushing `M204` to 900 (config is 250) produced an
   audible strain for no real gain, and skipped steps are invisible here. It has
   been restored to 250 / junction 0.05.
7. **macOS renumbers the serial port** and **plugging a camera in can drop the
   robot off the bus.**

## Lukas's asks — all closed

- Merge the two camera PRs into the integration branch and delete the stale
  branches: **done**. #4 MERGED, #3 CLOSED, both branches deleted after verifying
  containment, `main` untouched.
- Earlier branch cleanup: **done**, and his "all three are fully contained" claim
  was wrong for two of three — the tip-pickup commits had not landed. Fixed before
  deleting.
- `agent-loop-p0/structure-refactor` follow-up merge: **not reachable** from this
  machine, never published to the remote.

One judgement call left for him: there are now **two real `cap_removed`
verifiers** — his wrist-torque + marker fusion (active) and PR #4's depth-based
check (`core/verification/depth_height.py`, preserved intact). His is kept because
he shipped it deliberately; the depth argument is worth reading, since it does not
depend on lighting or a marker surviving a wet bench.

## Physical state at handoff

    X = 300   (of ~375 usable, from the homed near end)
    Y = 0     (relative to session start; +Y is toward the operator)
    Z = 0     (homed top)
    tip:      FITTED
    steppers: energised, holding

Steppers are deliberately left on: `M18` drops holding torque and with a tip
fitted a Z sag moves toward the deck.

## What to do next, in order

1. **Plunger identify** — 5 minutes, needs one look, unblocks `aspirate` and with
   it the OT half of `Q-EXEC-1`.
2. **Camera permission + a nozzle marker** — then the hand-eye fit runs and
   detections become moves.
3. **Y's datum** — vision is the answer; it cannot be homed, ever.
