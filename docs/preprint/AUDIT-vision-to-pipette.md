# Audit: can a camera detection command the pipette?

A structured multi-agent audit run 2026-07-26, answering one question exhaustively:
**can the OT-One pipette be commanded to the location of an object seen by a camera,
on this setup, today?** 45 agents, four independent investigation angles, each claimed
blocker then subjected to an adversarial attempt to refute it. 7 blockers survived;
33 claimed blockers were refuted.

**Answer: no.** But the remaining gap is smaller than this project believed, and it is
mostly physical rather than software.

---

## Provenance warning

This document mixes two evidence classes and labels every claim. Per the standard
`CORRECTIONS.md` item 23 applies to the whole folder, this file states which is which
rather than presenting all of it as observed.

- **[VERIFIED]** — re-run directly by the author of this file, command and output
  inspected.
- **[AGENT]** — reported by an audit agent with a file:line citation, **not**
  independently re-checked. Treat as a lead to confirm, not as a measurement.

Nothing in this file was observed on hardware. Both the robot and every camera were
disconnected at the time of the audit.

---

## The most actionable result

**The pixel-to-millimetre mapping this project has been treating as missing already
exists, written and tested, on an unmerged branch.**

[VERIFIED] Commit `b4b635c`, "Map pixels to deck millimetres without depth", on
`lukas/fix-cameras-api-segfault`:

    git log --oneline -1 b4b635c
      b4b635c Map pixels to deck millimetres without depth
    git merge-base --is-ancestor b4b635c HEAD  ->  NOT an ancestor
    git show --stat b4b635c
      backend/tests/test_deck_homography.py | 164 +++++
      core/calibration/deck_homography.py   | 201 +++++
      2 files changed, 365 insertions(+)

Two new files, **365 insertions and zero deletions**, so the cherry-pick is
conflict-free by construction. `core/calibration/deck_homography.py` is absent from
the working branch [VERIFIED]. Its commit message states 11 tests requiring neither a
camera nor the robot, recovering a known homography to 1e-6, surviving 0.5 mm
labelling noise, and catching a swapped correspondence [AGENT, from the commit
message].

    git cherry-pick b4b635ca15f2155b134fe0e9e7de11c11c46492e

Do not write a replacement.

---

## What is actually missing, shortest path first

1. **The robot on a bus.** `_opentrons_port()` globs `/dev/cu.usbmodem*`, finds
   nothing, and `require_port()` exits. Every bench script dies before opening the
   board. [VERIFIED — no node present]
2. **A camera that sees the deck.** No USB camera attached. The two the host knows
   about are not aimed at the deck and sit behind an ungranted macOS privacy gate.
   [AGENT]
3. **Something on the gantry a detector can find.** `MARKER_MAP` assigns tags 180-186
   and 224 entirely to static furniture — `left_base`, `right_base`, `ot_base`,
   `rack_1`, `tipbox_1`, `tube_1_cap`. Nothing rides the carriage. [VERIFIED — map read
   directly] This is the single genuine physical blocker.
4. **A red detector.** No hits for `COLOR_BGR2HSV`, `inRange` or `red_mask` anywhere in
   the tree. The only untagged detector converts to greyscale *before* finding
   circles, so a red tube and a white tube are the same object to it. Roughly 15 lines
   to fix. [AGENT]
5. **The mapping**, which exists — see above.
6. **~50 lines of glue.** Neither `ot_hand_eye.py` nor `deck_homography.py` has a
   production caller; only tests import them. [AGENT]

---

## Refuted: what a reader will wrongly assume is the blocker

This section matters more than the list above, because this project — and the author
of this file, repeatedly and in writing — believed several of these.

**The missing datum, the zeroed counters, the un-homeable Y.** Not a blocker.
Every step of a visual correction is *relative*: "move 12 mm that way" needs no
origin. `jog()` and `jog_path()` gate on `_reference_lost` only, never on `_homed`,
and both `correction_mm` and `servo_correction_mm` return relative millimetres. The
datum gates exactly one thing — `goto_point`, which replays a *taught point by name*
— and that is irrelevant to a vision target. [AGENT, high confidence, cites
`drivers/opentrons/driver.py:405-409,463` and
`backend/app/api/liquid_handler.py:340-352`]

This is a correction to guidance issued repeatedly during the session, which pushed
the operator toward homing the machine — an operation that on this unit grinds a
stepper against a hard stop — as a supposed prerequisite for vision-guided motion. It
is not one. Two distinct uses of a coordinate were conflated: *returning to a
remembered place*, which needs a durable origin, and *closing a visual error*, which
does not.

**Null intrinsics, absent depth, missing `pyrealsense2`.** Not a blocker. A homography
over a plane absorbs fx, fy, cx, cy and the camera pose into a single fit. This is why
`deck_homography` is the right model and `ot_hand_eye` is not: for a fixed camera over
a flat workspace, the planar fit is not a degraded substitute for the 3D one. [AGENT]

**`REPLAYABLE_AXES = ("X","Z")` meaning Y cannot move.** It means Y cannot be
*replayed*. `JOGGABLE_AXES` includes Y and `REFUSED_AXES` is empty; only Y *homing* is
forbidden. [AGENT] The session's own write-ups stated this imprecisely enough to imply
otherwise.

**The uncalibrated digital twin, and the calibration pipeline TODOs.** Off the path
for this purpose; they serve the arm twin, and nothing reads `world_pose` back out to
command the OT. [AGENT]

**The existing teach points proving it already works.** They do not. `red_target` and
`tip_at_target` were reached by a human watching a video feed and telling the
controller which way to move. Both carry `datum.homed: false`, and the counters they
reference were zeroed by a power cycle. **That is teleoperation, not a detection
driving a move**, and the paper must say so plainly. [VERIFIED — both entries read
from `data/teach_poses.json`]

---

## Minimal procedure

A coarse 2D planar homography on the deck plane is *sufficient* for "move above the
target". Full 3D hand-eye is not required and is the wrong model.

**Needs a human at the bench — none of this is software:**

- **H1.** Plug in and power the OT-One; confirm a `/dev/cu.usbmodem*` node appears.
- **H2.** Mount **one** camera looking down at the deck, with the nozzle and the target
  in the same frame. Any plain UVC webcam. No RealSense, no depth, no intrinsics.
- **H3.** Grant camera access to the host process and fully restart it.
- **H4.** Stick one 20 mm tag36h11 on the pipette carriage, using an id **not** in
  `MARKER_MAP` — 190 is free.
- **H5.** Clear the gantry's travel and stay on the power switch. No endstops, no
  current sensing: a crash is invisible to software.

**Software:**

- **S1.** Cherry-pick `b4b635c`.
- **S2.** Register the camera in the fleet config, respecting `CAM_EXCLUDE_INDICES`.
- **S3.** Add the red detector: `BGR2HSV`, two `inRange` masks for red's hue
  wraparound, `bitwise_or`, `findContours`, largest-contour moments, emit a `Shape`
  with a normalised centre — the identical contract the camera hub and the detections
  endpoint already consume.
- **S4.** Collect **at least 5** correspondences, spread over the working area and
  never collinear. At each, read the carriage tag's pixel centre and accumulate deck
  millimetres from your own relative jogs. Steps of 15 mm or less. Never home Y.
- **S5.** Fit. **Refuse to move unless the fit is trustworthy** (at least 5 points,
  RMS at or below 2.0 mm); print why not. Do not lower those thresholds.
- **S6.** Compute the correction, issue it as bounded relative jogs, **re-observe**,
  repeat two or three times. The re-observation is simultaneously the only crash
  detection this machine has.
- **S7.** Keep Z separate and human-watched. The homography returns x and y only, by
  design.

---

## For the paper

Three things here are worth reporting rather than filing.

**The refuted-blocker list is itself a finding.** Seven blockers survived scrutiny;
thirty-three did not. A team working on this system — including its most active
contributor that evening — held several beliefs about why vision could not drive the
robot, and most were wrong. The false beliefs were not random: each was a *real*
constraint transplanted from a neighbouring context. The datum genuinely does gate
teach-point replay. Intrinsics genuinely are required for metric 3D pose. Y genuinely
cannot be homed. Every one was true somewhere and imported where it did not apply.

This is a distinct hazard from the fifteen in `FINDINGS.md`. Those concern a system
reporting what it cannot observe. This concerns *engineers* reporting constraints that
do not obtain — and it produced the same outcome, work deferred against a blocker that
was not there.

**The mapping existed on an unmerged branch for hours** while the same capability was
described as absent and partially rewritten from scratch. That belongs with
`CONTEXT-project-plan.md`'s Q-ALLOC-1 and Q-COMMIT-2: the cost of uncommitted or
unmerged work is not only risk of loss, it is duplicated effort elsewhere in the team
who cannot see it.

**The homing recommendation should be reported as an error made during the study.**
The session repeatedly advised a physical operation that grinds a stepper, as a
prerequisite for something that never needed it. It sits alongside the position-drift
case in `FINDINGS.md` finding 3 as a second instance of the same underlying pattern:
confident reasoning over a premise imported from the wrong context, expressed in
fluent prose that carried no marker of its own uncertainty.
