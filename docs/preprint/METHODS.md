# Methods

## Apparatus

A bench-scale autonomous cell, assembled from commodity components. The
unremarkableness is the point: every mechanism in `FINDINGS.md` arises from
default behaviour of widely used parts, not from a pathological configuration.

**Liquid handler.** Opentrons OT-One gantry, Smoothieboard v1.0.3 controller,
G-code over USB serial at 115200 baud. Axes X, Y, Z, A (second mount), and B, C
(plungers). A disposable tip was fitted throughout.

**Arms.** Two UFactory xArm 6, connected over Ethernet, reporting joint angles and
Cartesian pose.

**Cameras.** Three viewpoints (`gripper_cam`, `overview_cam`, `handover_cam`).
Hardware varied during the session and included Intel RealSense D405/D435/D435i
units and generic UVC devices. Fiducials are AprilTag `tag36h11`, 20 mm, drawn
from lab sticker stock with IDs 180-224, mapped to named entities
(`left_base`=180, `right_base`=181, `ot_base`=182, `rack_1`=183, `tipbox_1`=186,
`tube_1_cap`=224).

**Software.** Python 3.13, FastAPI backend, Vue frontend, OpenCV (contrib, for
`cv2.aruco`), librealsense 2.56.5 (pip wheel) and 2.58.3 (Homebrew). Host: macOS
(Darwin 25.5.0), arm64.

## What "measured" means

Claims in `FINDINGS.md` fall into four evidence classes, and each finding states
which applies.

**Direct instrument reply.** A literal captured response. Example: the `M119`
endstop poll returning `min_x:0 min_y:0 min_z:0 min_a:0 min_b:0` unchanged over
18 s while the Z limit switch was held closed by hand. These are quoted verbatim,
not paraphrased.

**Timed motion.** Every motion command records wall-clock duration against a
duration predicted from commanded distance and feedrate. Because the controller
acknowledges on *queue*, arrival is established by blocking on `M400`, which
returns only when the planner queue drains. Predicted duration is
`distance_mm * 60 / feedrate_mm_per_min`. Agreement within a few percent is the
normal case; the session's moves typically ran within 0.1-0.5 % of prediction
(e.g. 25.12 s against 25.00 s for a 250 mm leg at 600 mm/min).

**Critically, this class establishes only that a move ran.** With no endstop and
no current sensing, a stalled axis skips steps while the timing is
indistinguishable from a clean move. Timing is used in this work to detect moves
that did *not* run (finding 2, where 1.00 s against 3.00 s exposed a latched
halt), never to confirm that a move reached anywhere.

**Operator observation.** Where no software signal exists, a human watching the
machine is the measurement instrument, and the finding says so. This is the only
evidence class behind finding 3's resolution and the (unresolved) plunger
identification.

**Code inspection with a reproducing observation.** Used where a defect is visible
in source and was also observed in operation, e.g. the fusion arithmetic (finding
4), where the averaging rule was read from source and the specific channel values
producing exactly 0.600 were reproduced in tests.

## Instrumentation and controls

**Relative-only motion.** After the position counters were observed re-zeroing
(finding 3), all motion tooling was rewritten to issue relative moves exclusively,
so no command depends on an absolute datum. Closed out-and-back paths were used
throughout, which gives a weak internal consistency check: the counter must return
to its starting value.

That check is deliberately described as weak. Because the counter is open-loop, a
closed loop returns to 0.00 on paper *even if both legs stalled*. It detects
arithmetic errors in the tooling, not physical ones in the machine.

**Bounded steps with an operator in the loop.** Motion into unmeasured territory
was issued in bounded increments (typically 5 mm) with the operator observing,
following the pattern of the existing envelope-measurement tooling. Emergency stop
is fired before any exception unwinds, because closing the serial port does not
halt an in-flight move.

**Fault-state clearing.** `M999` is issued unconditionally on connect. A halted
board answers status queries normally while ignoring motion commands, so a
responsiveness check is not a control for this.

## Verification-layer testing

The verification agents were exercised against injected faults rather than only
happy paths. A fault-catch table drives a set of scenarios and exits non-zero if
any misses its expectation; under it, fused verification catches 6 of 6 injected
faults where a naive single-channel check catches 1 of 6. Results are sealed with
SHA-256 over the evidence, and the reported tier is computed from the channel
minimum so a caller cannot promote its own result.

Test suites: 174 tests across the motion/API/verification stack, and 249 across
the verification and receipt layer in the companion checkout. All green at the
time of writing.

## Threats to validity

**Single session, single cell, n=1.** The mechanisms are general; their frequency
in the wild is not established here, and no claim of prevalence is made.

**Hardware changed under us.** Cameras were physically connected and disconnected
during the session, and the controller reset at least twice, once with no operator
action. Findings that depend on a stable configuration are marked accordingly.

**The observer was part of the system.** The supervising agent both recorded the
findings and produced one of them (finding 3). That case is reported from the
transcript, including the incorrect assertions, rather than reconstructed
afterwards.

**Some fixes are unvalidated on hardware.** The datum-guarded teach point and the
disagreement-detection logic are covered by tests and were exercised in
simulation; the teach point was recorded on real hardware but never replayed,
because replay requires a homed datum that was never established. This is stated
as a limitation rather than smoothed over.

## Reproducibility

The timing measurements quoted throughout are reproducible with the motion scripts
named in `FINDINGS.md`. The endstop and halt observations require the same class of
controller and reproduce immediately. The fusion arithmetic reproduces from the
channel values alone and requires no hardware:

    torque = 0.9, vision = 0.9, depth = 0.0  ->  mean = 0.600  ->  PASS at 0.60
