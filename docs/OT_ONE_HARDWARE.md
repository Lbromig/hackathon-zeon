# OT-One hardware notes

Measured on the bench unit 2026-07-25. Everything here was read off the
machine, not assumed. `drivers/opentrons/driver.py` sources its constants
from this file.

it before writing any positioning code against this robot.

## The machine has no working endstops

Polled `M119` for 18 seconds while the Z limit switch was pressed by hand. **Not
one endstop bit ever changed.** Baseline and final state were identical:

    min_x:0 min_y:0 min_z:0 min_a:0 min_b:0

The firmware config has no axis limit entries either:

    config-get sd gamma_min             -> sd: gamma_min is not in config
    config-get sd gamma_max_travel      -> sd: gamma_max_travel is not in config
    config-get sd gamma_homing_direction-> sd: gamma_homing_direction is not in config

### What follows from that

`G28.2` does **not** home to a limit on this machine. It drives a fixed search
distance, gives up, and zeroes the position counter. Evidence: homing Z twice in
a row took 5.44 s then 6.56 s. A real second home starts already on the switch
and finishes in a fraction of a second. Both runs burned the full search.

So **`Z=0` after homing is not a physical datum.** Any absolute coordinate is
referenced to a position that was never established. Do not use absolute moves
for positioning on this robot.

This is also the true root cause of the original Y stall. Y was not specially
broken: `G28.2 Y` drove looking for a switch that never reports, ran into a hard
stop, and ground. Z has been doing the same thing all along, just without an
obstruction in the way.

## Use relative jogging instead

`G91` relative mode needs no datum, so it is the only trustworthy way to position
this machine as currently wired. `scripts/jog_z.py` does one bounded step:

    python3 scripts/jog_z.py 2      # 2 mm DOWN
    python3 scripts/jog_z.py -2     # 2 mm UP

It caps a single step at 15 mm, runs at 3 to 5 mm/s so it can be stopped by hand,
and restores `G90` afterwards.

## Verified geometry

- **Down is +Z.** Established by observation: after homing the pipette sits at
  the top of its travel and Z reads 0, so increasing Z descends.
- A controlled descent of **53 mm** from the raised position brought the nozzle
  to the tip in the rack, done in 1 and 2 mm increments.
- A **20 mm** raise from there ran in 4.00 s against 4.00 s expected.

## Motion duration is the only feedback

With no endstops and no current sensing, the sole confirmation that a move
happened is how long `M400` blocks. `G0` and `G28.2` acknowledge when a move is
**queued**, not when it arrives, so their ack proves nothing. `M400` blocks until
the planner queue drains and is the real signal.

Measured, at F600 (10 mm/s), a 40 mm move: `G0` acked in 85 ms, `M400` blocked
4.60 s against a predicted 4.00 s. At F300, 2 mm steps consistently measured
0.39 to 0.45 s against 0.40 s predicted.

**A crash is invisible.** A stalled stepper skips steps and the timing looks
identical to a clean move. The operator watching is the only protection.

## The board wedges, and USB hides it

Twice the board stopped accepting writes mid-session, surfacing as
`SerialTimeoutException: Write timeout`. It stays enumerated on USB and can still
look healthy, so this is easy to misread as working.

Two traps that follow:

- The Smoothieboard logic is powered over **USB**, while the motor rail is
  separate. With the motor supply off, the board answers normally, accepts moves,
  updates its position registers, and reports correct durations, while **nothing
  physically moves.** Only the untriggered endstops hint at it.
- A software reset does not clear it. Recovery needs a real power cycle: power
  off, **unplug USB too**, wait about 5 s, reconnect.

`write_timeout` on the port is therefore mandatory. Without it a write to a
wedged board blocks forever, including the write inside the emergency stop.

## Emergency stop notes

Order matters: `Ctrl-X` (0x18) first, then `M112`, then `M18`. Ctrl-X is handled
at the serial layer so it interrupts a move already executing; `M112` alone can
sit in the queue behind that very move. Never call `flush()` in the stop path: on
POSIX that is `tcdrain`, which is not bounded by `write_timeout` and can block
forever against exactly the wedged board the stop exists to rescue.

`M112` latches the board in HALT, where it ignores everything until `M999`.

`M18` de-energizes the steppers, so send `M17` before expecting motion again
after any stop.

---


## The datum, and its caveat

This machine has no working endstops (see `HARDWARE-FINDINGS.md`), so `G28.2 Z`
does not stop on a switch. It drives its full search distance, reaches the
mechanical top stop, and zeroes the counter there.

That does give a repeatable reference: the top of travel. But it is reached by
driving into a hard stop rather than tripping a switch, so **every home stresses
the mechanism**. Treat it as a working datum, not a good one. Fixing the endstop
wiring or firmware config is the real repair.

Homing `Z` takes 5.4 to 6.6 s because it always runs the whole search.

## Engagement depth

    tip engagement:  53 mm below the post-home top position
    verified lift:   20 mm raised, tip retained

Approach was done in decreasing increments, which is what kept it safe:

| Phase             | Step size | Feedrate           | Cumulative depth |
|-------------------|-----------|--------------------|------------------|
| initial descent   | 2 mm      | 300 mm/min (5 mm/s)| 0 to 49 mm       |
| final approach    | 1 mm      | 240 mm/min (4 mm/s)| 26 to 33 mm      |
| tip engagement    | 2 mm      | 180 mm/min (3 mm/s)| 49 to 53 mm      |

No step ever showed elevated duration, so no mechanical load was detected on the
way down. Contact was confirmed visually by the operator, not by the software.

## Reproducing it

Relative jogging only. Absolute coordinates reference a datum that is not
physically established, so do not use them here.

    # 1. establish the datum (drives to the top stop, ~6 s)
    python3 ot_driver.py home --transport serial --port /dev/cu.usbmodem11201 \
        --axes Z --go

    # 2. descend to ~49 mm in 2 mm steps, watching
    python3 scripts/jog_z.py 2        # repeat

    # 3. last few mm in 1 mm steps
    python3 scripts/jog_z.py 1        # repeat

    # 4. raise and confirm the tip stayed on
    python3 scripts/jog_z.py -2       # repeat

`jog_z.py` caps one step at 15 mm and restores `G90` afterwards.

## What the timings do and do not prove

`M400` blocks until the planner queue drains, so its duration confirms a move
*ran*. Measured against prediction it was accurate throughout: 2 mm at 5 mm/s
came in at 0.39 to 0.45 s against 0.40 s predicted; the 20 mm raise took 4.00 s
against 4.00 s predicted.

It does **not** prove the nozzle was clear. With no endstops and no current
sensing, a stalled stepper skips steps and the timing is indistinguishable from a
clean move. **A crash is invisible to the software.** The operator watching is the
only protection, and that was true for every number on this page.

## Known interruption

Serial writes to the board timed out twice mid-session
(`SerialTimeoutException: Write timeout`), which aborted one 2 mm step before it
was issued. The board stays enumerated on USB while wedged. Recovery is a full
power cycle: power off, unplug USB, wait about 5 s, reconnect.

`write_timeout` on the port is what turns this into a clean abort instead of a
hang. Do not remove it.

---

# Runbook: next bench session

Everything below is prepared and offline-verified. The order matters: each step
gates the next, and the cheap checks come first so a dead cable is found in
seconds rather than after a failed calibration.

## 0. Reconnect and confirm the machine is really there

The board's logic runs off **USB** while the motors sit on a **separate rail**, so
it can answer perfectly with dead motors, or vanish entirely. Both have happened.

    python3 ot_driver.py detect

Expect a `Smoothieboard` candidate on `/dev/cu.usbmodem*`. If the bus is empty the
cable is not making a data connection — reconnect it. If it enumerates but will not
answer, it needs a real power cycle: power off, **unplug USB too**, wait ~5 s.

Note the port name; macOS does not always reissue the same `usbmodem` number.

## 1. Confirm motors actually move (30 s, no risk)

The trap: with the motor rail off, moves are accepted, position registers update,
and durations come back correct while **nothing physically moves**.

    python3 ot_driver.py home --transport serial --port <PORT> --axes Z --go

A real Z home takes 5-7 s. If it returns instantly, or nothing visibly moves, the
motor supply is off.

## 2. Re-verify continuous motion (unverified — do this before trusting it)

`jog_path()` was written to fix visible jitter: driving a path with one `jog()` per
step drains the planner with `M400` after every step, stopping the machine dead
between steps. It queues the moves and drains once instead. **This has never run on
hardware** — the board dropped off before it could.

    python3 scripts/tour_search_space.py --x 60 --y 40 --laps 2 --feed 600

Watch for one continuous sweep per lap rather than a series of twitches. The loop
is closed, so net displacement should print `0.00 / 0.00`.

## 3. Identify the mounted plunger (the actual blocker: Q-OT-PLUNGER-1)

Cheapest informative step, and it can also tell you the plunger motor is not
connected at all — which would be a hardware finding, not a calibration result.

    python3 scripts/calibrate_plunger.py --identify

It nudges B, returns it, then C, returns it. Whichever visibly moves is the mounted
side. `M119` reports `min_b` but no `min_c`, so do not assume it is B.

## 4. Measure µL per mm

Needs a tip fitted and primed, its end submerged in water, and the plunger at a
repeatable start point.

    python3 scripts/calibrate_plunger.py --measure --axis <B|C> --mm 2.0

Dispense into a tared container (or read the tip graduation), then:

    plunger_ul_per_mm = volume_uL / mm_travelled

Take it **at least twice**. A single reading cannot show whether the plunger is
repeatable, which is the property that actually matters.

## 5. Record it and aspirate becomes real

On the `opentrons` entry in `core/config.py` `DEFAULT_FLEET`:

    "plunger_axis": "B",            # whichever moved
    "plunger_ul_per_mm": <measured>

That closes Q-OT-PLUNGER-1 and hands over the OT half of Q-EXEC-1 (the floor
rung's aspirate step). Until both values exist, `aspirate`/`dispense` refuse by
design rather than guessing.

## Throughout: what the software cannot tell you

**A crash is invisible.** No endstops register on any axis and there is no current
sensing, so a stalled stepper skips steps and a blocked move returns exactly like a
clean one. Every duration in every log above proves a move *ran*, never that the
path was *clear*. The operator watching is the only feedback channel this machine
has until vision supplies one.

If anything grinds, cut power at the switch. Do not rely on software.

    python3 ot_driver.py estop --port <PORT>     # writes Ctrl-X, M112, M18


---

# Motion profile (read from the board, 2026-07-26)

    config-get sd acceleration          -> 250        (mm/s^2)
    config-get sd default_seek_rate     -> 2500       (mm/min, homing search)
    config-get sd junction_deviation    -> not in config
    config-get sd x_axis_max_speed      -> not in config
    config-get sd default_feed_rate     -> not in config

`junction_deviation` being absent means corner blending runs on Smoothieware's
built-in default rather than a tuned value. With acceleration at 250 mm/s^2 a short
leg may never reach cruise before it has to decelerate for the next vertex, so leg
length and feedrate interact: more `--sides` gives gentler corners but shorter legs.

Measured smooth at **100 x 70 mm, 16 sides, F700, 12 mm Z dip**: 806 mm over 48
legs in 69.37s against 69.07s predicted (+6 ms/leg), closing to 0.00 on all three
axes. Confirmed smooth by eye at that setting, so the profile was left untouched —
`M204`/`M205` both answer `ok`, so acceleration and junction deviation *can* be
raised at runtime if a future path needs it, but that was not necessary and raising
acceleration on a machine with no closed loop risks skipped steps.

# Deck state to check before any Z motion

**A tip may be fitted.** As of 2026-07-26 one is. A fitted tip extends the nozzle,
so the usable clearance below the datum is less than the bare-nozzle figure, and the
53 mm tip-engagement depth assumes an *empty* nozzle descending onto a tip in the
rack — do not reuse it as a descent target with a tip already on.
