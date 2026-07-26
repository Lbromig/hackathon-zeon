"""Opentrons driver (the original OT-One unit).

The OT-One predates the modern Opentrons HTTP API, so this driver talks G-code
over USB serial to the unit's Smoothieboard. That path is verified on the bench
unit; ``docs/OT_ONE_HARDWARE.md`` holds the measurements behind every constant
here.

Three hardware facts shape this implementation, and each removes an option that
would otherwise look natural:

1. **No endstop on any axis registers with the board.** ``M119`` was polled for
   18 s while the Z limit switch was pressed by hand and no bit ever changed,
   and the firmware config holds no axis limit entries. So ``G28.2`` never
   terminates on a limit: it drives its full search distance into the mechanical
   stop and zeroes the counter there.

2. **Therefore Z=0 is not a physical datum**, and absolute coordinates reference
   a position that was never established. Only *relative* (``G91``) motion is
   trustworthy, so that is the primitive everything here builds on.

3. **G0 and G28.2 acknowledge when a move is QUEUED, not when it arrives.** Their
   ack proves nothing. ``M400`` blocks until the planner queue drains, so its ack
   is the real "finished" signal and the only thing worth timing out on.

What is not implemented raises ``DriverError`` rather than being faked. XY
positioning, the plungers, and tip ejection have never been exercised on this
unit, and a stub that silently no-ops would be read as working.
"""
from __future__ import annotations

import time
from typing import Any

from ..base import ConnectionState, DeviceInfo, DriverError, InstrumentKind
from ..capabilities.liquid_handler import DeckLocation, LiquidHandlerDriver

# --- G-code vocabulary, confirmed against firmware v1.0.3 on the bench unit ---
G_HOME = "G28.2"          # Smoothie homing (NOT plain G28)
G_MOVE = "G0"
G_ABSOLUTE = "G90"
G_RELATIVE = "G91"
G_WAIT_MOVES = "M400"     # blocks until the queue drains: the real "arrived"
G_ENDSTOPS = "M119"
G_ESTOP = "M112"          # latches HALT; needs M999 to clear
G_CLEAR_HALT = "M999"
G_STEPPERS_ON = "M17"
G_STEPPERS_OFF = "M18"
SMOOTHIE_RESET = b"\x18"  # Ctrl-X: handled at the serial layer, so it interrupts
                          # a move already executing. M112 alone can sit in the
                          # queue behind that very move.

BAUD = 115200
ACK_TIMEOUT = 2.5         # non-motion commands ack almost instantly
MOTION_TIMEOUT = 20.0     # bounded, but a homing search legitimately takes 5-7 s
READ_POLL = 0.25          # short, so the deadline loop can actually run
WRITE_TIMEOUT = 0.5       # without this a write to a wedged board blocks forever

# Measured tip-pickup geometry. See docs/OT_ONE_HARDWARE.md.
TIP_ENGAGE_DEPTH_MM = 53.0
MAX_JOG_MM = 15.0         # refuse larger single relative steps
APPROACH_FEED = 300.0     # mm/min (5 mm/s)
ENGAGE_FEED = 180.0       # mm/min (3 mm/s), gentler for seating the tip

# --- plungers -----------------------------------------------------------------
# B and C are the two pipette plungers. They are NOT gantry axes: travel is short
# and a plunger driven past its seal jams, so they get a much tighter per-jog cap.
PLUNGER_AXES = ("B", "C")
MAX_PLUNGER_JOG_MM = 3.0
PLUNGER_FEED = 120.0      # mm/min (2 mm/s): plungers are geared, go gently

# Volume calibration is a MEASUREMENT, not a constant. It has never been taken on
# this unit, so it lives in config and aspirate/dispense refuse without it rather
# than guessing. `M119` reports `min_b` but no `min_c`, so the plungers are not
# symmetric and the mounted side must be identified empirically too.
# Populate both via scripts/calibrate_plunger.py:
#     {"plunger_axis": "B", "plunger_ul_per_mm": <measured>}


class OpentronsDriver(LiquidHandlerDriver):
    """Config: {"transport": "serial", "port": "/dev/cu.usbmodem11201"}."""

    def __init__(self, device_id: str, config: dict[str, Any] | None = None) -> None:
        super().__init__(device_id, config)
        self._conn: Any = None
        self._z_offset_mm = 0.0    # relative depth below the homed datum
        self._homed = False
        self._reference_lost = False

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            id=self.device_id,
            name=self.config.get("name", "Opentrons (OT-One)"),
            kind=InstrumentKind.LIQUID_HANDLER,
            model="OT-One",
            vendor="Opentrons",
            meta={
                "firmware": "smoothie v1.0.3",
                "control_path": "serial g-code",
                "endstops_functional": False,
                "verified": ["z_home", "relative_z_jog", "pick_up_tip"],
            },
        )

    # --- lifecycle ---------------------------------------------------------
    def connect(self) -> None:
        self._state = ConnectionState.CONNECTING
        port = self.config.get("port")
        if not port:
            self._state = ConnectionState.ERROR
            raise DriverError("Opentrons config needs a 'port'")
        try:
            import serial
        except ImportError as e:
            self._state = ConnectionState.ERROR
            raise DriverError("pyserial is required for the OT-One serial path") from e
        try:
            self._conn = serial.Serial(
                port, BAUD, timeout=READ_POLL, write_timeout=WRITE_TIMEOUT
            )
        except Exception as e:
            self._state = ConnectionState.ERROR
            raise DriverError(f"Opentrons connect failed on {port}: {e}") from e

        # Opening the port toggles DTR, which resets the board and discards any
        # homing reference it held.
        time.sleep(2.0)
        self._homed = False
        self._reference_lost = False
        self._z_offset_mm = 0.0
        try:
            self._conn.reset_input_buffer()
        except Exception:
            pass

        if not self._responsive():
            # A previous M112 latches HALT, where the board ignores everything.
            try:
                self._write(G_CLEAR_HALT)
            except Exception:
                pass
            time.sleep(0.6)
            if not self._responsive():
                self._state = ConnectionState.ERROR
                self.disconnect()
                raise DriverError(
                    "board is enumerated but not answering, even after M999. It "
                    "needs a full power cycle: power off, unplug USB, wait 5 s."
                )
        self._send(G_ABSOLUTE)
        self._state = ConnectionState.CONNECTED

    def disconnect(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
        self._state = ConnectionState.DISCONNECTED

    def status(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "connected": self._conn is not None,
            "homed": self._homed,
            "reference_lost": self._reference_lost,
            "z_below_datum_mm": self._z_offset_mm,
            "endstops_functional": False,
        }

    # --- transport ---------------------------------------------------------
    def _write(self, command: str) -> None:
        if self._conn is None:
            raise DriverError("Opentrons not connected")
        self._conn.write((command + "\r\n").encode())
        self._conn.flush()

    def _responsive(self) -> bool:
        """True if the board answers a harmless query. Commands no motion."""
        try:
            self._write("version")
        except Exception:
            return False
        deadline = time.time() + ACK_TIMEOUT
        while time.time() < deadline:
            try:
                if self._conn.read(256):
                    return True
            except Exception:
                return False
        return False

    def _send(self, command: str, motion: bool = False,
              timeout: float | None = None) -> str:
        if self._conn is None:
            raise DriverError("Opentrons not connected")
        # An explicit timeout is needed for a queued path: M400 there waits for
        # the WHOLE path to finish, which legitimately exceeds MOTION_TIMEOUT.
        budget = timeout if timeout is not None else (
            MOTION_TIMEOUT if motion else ACK_TIMEOUT)
        # Drop anything left over. One orphaned "ok" would otherwise satisfy this
        # command instantly and silently disable the stall detection below.
        try:
            self._conn.reset_input_buffer()
        except Exception:
            pass
        self._write(command)

        deadline = time.time() + budget
        buf = ""
        while time.time() < deadline:
            chunk = self._conn.readline().decode(errors="replace")
            if not chunk:
                continue
            buf += chunk
            low = chunk.lower()
            if "error" in low or "alarm" in low or "halt" in low:
                if motion:
                    self.estop()
                raise DriverError(f"firmware rejected {command!r}: {chunk.strip()}")
            if "ok" in low:
                return buf
        if motion:
            # No ack inside the budget means the axis is very likely driving
            # against a stop. Cut motion BEFORE raising: closing the port does
            # not stop an in-flight Smoothieware move.
            self.estop()
            raise DriverError(
                f"no ack for {command!r} within {budget:.0f}s, emergency stop sent. "
                f"The axis did not arrive (blocked, or driving into a hard stop)."
            )
        raise DriverError(f"timed out waiting for ack to {command!r}")

    def _motion(self, command: str) -> None:
        """Issue motion and wait for it to actually finish, not merely queue."""
        self._send(command, motion=True)
        self._send(G_WAIT_MOVES, motion=True)

    def estop(self) -> bool:
        """Cut motion now. Best effort, never raises.

        Returns True only if bytes were actually written. Callers must report
        that honestly: a stop that claims success while writing nothing is worse
        than no stop at all.
        """
        self._reference_lost = True
        self._homed = False
        if self._conn is None:
            return False
        wrote = False
        for payload in (SMOOTHIE_RESET,
                        (G_ESTOP + "\r\n").encode(),
                        (G_STEPPERS_OFF + "\r\n").encode()):
            try:
                # Deliberately no flush(): on POSIX that is tcdrain, which is not
                # bounded by write_timeout and can block forever against exactly
                # the wedged board this exists to rescue.
                self._conn.write(payload)
                wrote = True
                time.sleep(0.2)
            except Exception:
                pass
        return wrote

    # --- queries -----------------------------------------------------------
    def endstops(self) -> dict[str, int]:
        """Read endstop states. Commands no motion.

        On this unit every bit reads 0 permanently, including while a switch is
        physically pressed. Kept because it is the diagnostic that proves it.
        """
        raw = self._send(G_ENDSTOPS)
        out: dict[str, int] = {}
        for token in raw.replace(",", " ").split():
            if ":" in token and token.lower().startswith("min"):
                key, _, value = token.partition(":")
                try:
                    out[key.strip().lower()] = int(value)
                except ValueError:
                    pass
        return out

    # --- motion primitives -------------------------------------------------
    def home(self) -> None:
        """Home the Z lift only.

        Z ONLY, deliberately. Y does not home on this unit: ``G28.2 Y`` drives
        looking for a switch that never reports and grinds against a hard stop.
        X and the A mount do ack, but nothing verifies where they stopped.

        Because no endstop registers, this drives the full search distance into
        the mechanical top stop and zeroes there. That is repeatable, so it
        serves as a datum, but every home stresses the mechanism. Repairing the
        endstop wiring is the real fix.
        """
        self._send(G_STEPPERS_ON)
        self._motion(f"{G_HOME} Z")
        self._homed = True
        self._reference_lost = False
        self._z_offset_mm = 0.0

    def jog(self, axis: str, delta_mm: float,
            feedrate: float = APPROACH_FEED) -> None:
        """Move one axis by a relative amount.

        Relative, because this unit has no trustworthy datum on any axis. The
        caller is responsible for knowing there is room: with no endstops and no
        current sensing, an overrun is invisible to software.

        Y IS allowed here, unlike in home(). The Y fault is specific to homing:
        `G28.2 Y` drives a long search looking for an endstop that never reports
        and grinds against a hard stop. A bounded relative move does no search,
        so it is a different operation entirely. Verified on hardware
        2026-07-25: 10 mm of Y in 2 mm steps, every step within 0.01 s of the
        predicted duration, no resistance.

        Sign conventions verified on this unit:
          Z: positive is DOWN.
          X, Y: axis directions confirmed to move; which way an operator calls
                "left" or "back" depends on where they are standing, so the UI
                labels them by axis rather than by direction.
        """
        axis = axis.upper()
        if axis not in ("X", "Y", "Z", "A", *PLUNGER_AXES):
            raise DriverError(f"unknown axis {axis!r}")
        if axis in PLUNGER_AXES:
            # Plungers get their own, much smaller cap: short travel, and one
            # driven past its seal jams.
            if abs(delta_mm) > MAX_PLUNGER_JOG_MM:
                raise DriverError(
                    f"refusing a {delta_mm} mm jog on plunger {axis}: the cap is "
                    f"{MAX_PLUNGER_JOG_MM} mm, because plunger travel is short and "
                    f"overrunning the seal jams it."
                )
        elif abs(delta_mm) > MAX_JOG_MM:
            raise DriverError(
                f"refusing a {delta_mm} mm jog: the cap is {MAX_JOG_MM} mm per "
                f"step, with no endstops to catch an overrun."
            )
        if self._reference_lost:
            raise DriverError("reference lost after an emergency stop; home() again")
        self._send(G_STEPPERS_ON)
        try:
            self._send(G_RELATIVE)
            self._motion(f"{G_MOVE} {axis}{delta_mm:.2f} F{feedrate:.0f}")
        finally:
            # Always restore absolute mode, even on failure, so the board is
            # never left in a mode the next caller does not expect.
            try:
                self._send(G_ABSOLUTE)
            except Exception:
                pass

    def jog_path(self, steps: list[tuple[str, float]],
                 feedrate: float = APPROACH_FEED,
                 max_total_mm: float = 500.0) -> None:
        """Queue several relative moves back-to-back for CONTINUOUS motion.

        Use this for scripted paths. `jog()` drains the planner with M400 after
        every single step, which brings the machine to a dead stop between steps
        — so a path built from repeated jog() calls visibly jitters, one
        start-stop per step. Here the moves are queued without draining, letting
        Smoothieware's look-ahead blend them into continuous motion, and the
        queue is drained once at the end.

        The trade-off is deliberate: a stall is only detected when the whole
        path finishes rather than per step, so the guard becomes a cap on total
        distance instead of per-step distance. Keep paths short enough that an
        operator can still react.
        """
        if not steps:
            return
        total = sum(abs(d) for _, d in steps)
        if total > max_total_mm:
            raise DriverError(
                f"refusing a {total:.0f} mm path: the cap is {max_total_mm:.0f} mm "
                f"total, because a stall is only detected once the path completes."
            )
        for axis, _ in steps:
            a = axis.upper()
            if a not in ("X", "Y", "Z", "A"):
                raise DriverError(f"unknown axis {axis!r}")
        if self._reference_lost:
            raise DriverError("reference lost after an emergency stop; home() again")

        self._send(G_STEPPERS_ON)
        try:
            self._send(G_RELATIVE)
            # Queue every move first, WITHOUT waiting. This is what makes the
            # motion continuous rather than a series of separate moves.
            for axis, delta in steps:
                self._send(f"{G_MOVE} {axis.upper()}{delta:.2f} F{feedrate:.0f}",
                           motion=True)
            # One drain for the whole path. Its ack is the real "arrived".
            self._send(G_WAIT_MOVES, motion=True,
                       timeout=max(MOTION_TIMEOUT, total * 60.0 / feedrate + 10.0))
        finally:
            try:
                self._send(G_ABSOLUTE)
            except Exception:
                pass
        for axis, delta in steps:
            if axis.upper() == "Z":
                self._z_offset_mm += delta

    def jog_z(self, delta_mm: float, feedrate: float = APPROACH_FEED) -> float:
        """Move Z by a relative amount. Positive is DOWN on this unit.

        Returns the new depth below the homed position.
        """
        self.jog("Z", delta_mm, feedrate)
        self._z_offset_mm += delta_mm
        return self._z_offset_mm

    # --- liquid handling ---------------------------------------------------
    def pick_up_tip(self, location: DeckLocation) -> None:
        """Lower onto a tip already sitting under the nozzle, and seat it.

        Verified on the bench unit: engagement is ``TIP_ENGAGE_DEPTH_MM`` below
        the homed top position. This does NOT travel in XY, because XY cannot be
        positioned on this unit (see ``move_to``), so the rack must already be
        under the nozzle.

        A crash is invisible to software here: with no endstops and no current
        sensing, a stalled stepper skips steps and the timing is identical to a
        clean move. Motion duration proves a move ran, never that it was clear.
        """
        if location.x is not None or location.y is not None:
            raise DriverError(
                "pick_up_tip cannot travel in XY on the OT-One: Y does not home "
                "and no datum exists. Position the rack under the nozzle first, "
                "then call with an XY-free DeckLocation."
            )
        if not self._homed:
            self.home()

        remaining = TIP_ENGAGE_DEPTH_MM - self._z_offset_mm
        if remaining <= 0:
            raise DriverError(
                f"already {self._z_offset_mm:.1f} mm down, at or past the "
                f"{TIP_ENGAGE_DEPTH_MM:.1f} mm engagement depth"
            )

        # Descend in bounded steps, easing off for the final engagement so a
        # contact cannot overshoot.
        while remaining > 0:
            if remaining <= 4.0:
                step, feed = min(2.0, remaining), ENGAGE_FEED
            else:
                step, feed = min(2.0, remaining), APPROACH_FEED
            self.jog_z(step, feedrate=feed)
            remaining = TIP_ENGAGE_DEPTH_MM - self._z_offset_mm

    def drop_tip(self, location: DeckLocation | None = None) -> None:
        raise DriverError(
            "drop_tip is not implemented on the OT-One: tip ejection uses the "
            "plunger axes (B/C), which have never been exercised on this unit. "
            "Remove the tip by hand rather than trusting an untested eject."
        )

    def move_to(self, location: DeckLocation) -> None:
        raise DriverError(
            "move_to is not available on the OT-One. Absolute coordinates need a "
            "datum, and this unit has no working endstops, so G28.2 zeroes the "
            "counter at a mechanical stop rather than a known reference. Y does "
            "not home at all. Use jog_z() for relative Z motion."
        )

    # --- plunger ------------------------------------------------------------
    def _plunger_config(self) -> tuple[str, float]:
        """The measured plunger axis and µL-per-mm, or a refusal explaining how.

        Deliberately not defaulted. Guessing either value would move a plunger
        the wrong way or by the wrong amount, and neither is recoverable from a
        firmware read: this board's config has no plunger entries, exactly as it
        has none for the axis limits.
        """
        axis = self.config.get("plunger_axis")
        ul_per_mm = self.config.get("plunger_ul_per_mm")
        if not axis or not ul_per_mm:
            raise DriverError(
                "plunger is not calibrated, so volumes cannot be commanded. "
                "Two values are needed in the fleet config and both are "
                "measurements, not constants: 'plunger_axis' (B or C — M119 "
                "reports min_b but no min_c, so the mounted side is not "
                "symmetric and must be found empirically) and "
                "'plunger_ul_per_mm'. Run scripts/calibrate_plunger.py."
            )
        axis = str(axis).upper()
        if axis not in PLUNGER_AXES:
            raise DriverError(f"plunger_axis must be one of {PLUNGER_AXES}, got {axis!r}")
        try:
            per_mm = float(ul_per_mm)
        except (TypeError, ValueError):
            raise DriverError(f"plunger_ul_per_mm must be a number, got {ul_per_mm!r}")
        if per_mm <= 0:
            raise DriverError("plunger_ul_per_mm must be positive")
        return axis, per_mm

    def _move_plunger(self, volume_ul: float, direction: int) -> None:
        """Move the plunger by a volume. direction -1 draws up, +1 pushes out.

        The sign convention is part of the calibration: which way the plunger
        travels to draw liquid is recorded by calibrate_plunger.py as the sign of
        plunger_ul_per_mm's axis motion, so it is not assumed here.
        """
        axis, per_mm = self._plunger_config()
        if volume_ul <= 0:
            raise DriverError("volume must be positive")
        mm = volume_ul / per_mm
        if mm > MAX_PLUNGER_JOG_MM:
            # Split it, but keep each step inside the plunger cap.
            steps = []
            remaining = mm
            while remaining > 1e-9:
                s = min(MAX_PLUNGER_JOG_MM, remaining)
                steps.append((axis, direction * s))
                remaining -= s
            self.jog_path(steps, feedrate=PLUNGER_FEED,
                          max_total_mm=MAX_PLUNGER_JOG_MM * 20)
        else:
            self.jog(axis, direction * mm, feedrate=PLUNGER_FEED)

    def aspirate(self, volume_ul: float, location: DeckLocation) -> None:
        """Draw `volume_ul` with the calibrated plunger.

        Does NOT travel to `location`: this unit has no datum, so there is no
        absolute move (see move_to). Position the tip over the liquid first with
        relative jogs, then call this.
        """
        self._move_plunger(volume_ul, direction=-1)

    def dispense(self, volume_ul: float, location: DeckLocation) -> None:
        """Push `volume_ul` back out with the calibrated plunger.

        Like aspirate, this does not travel — position first.
        """
        self._move_plunger(volume_ul, direction=+1)
