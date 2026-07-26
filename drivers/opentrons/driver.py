"""Opentrons OT-One driver — G-code over USB serial to its Smoothieboard.

The OT-One predates the modern Opentrons HTTP API. Its controller is a Smoothieboard
(Uberclock, USB 0x1D50:0x6015) running Opentrons' own Smoothieware fork, which answers
G-code on a USB CDC serial port at 115200. Verified on the bench 2026-07-26:

    version -> {"version":v1.0.3_}
    M119    -> min_x:0 min_y:0 min_z:1 min_a:1 min_b:1
    M114    -> ok C: X:0.000 Y:0.000 Z:0.000 x:0.000 y:0.000 z:0.000

Five axes: X/Y/Z gantry plus **A and B, the two pipette plungers**.

Two hard-won rules are built into this driver rather than left to callers, because
violating either wedges the machine in a way only a power cycle recovers:

1. **One command at a time, and every command must answer.** Smoothieware services serial
   from the same main loop that executes motion, so a blocked move means nothing is read
   from the port. Commands sent into that silence are not rejected — they queue, and run
   later, in a state nobody predicted. :meth:`_send` therefore refuses to send anything
   after a timeout.
2. **Never move toward a triggered endstop.** Doing so latches a limit halt
   ("Limit switch min_z was hit - reset or M999 required") after which every command fails
   until the latch is cleared. :meth:`initialize` reads ``M119`` and picks directions from
   it rather than assuming.

Homing is deliberately NOT implemented — see :meth:`home`.
"""
from __future__ import annotations

import glob
import logging
import math
import threading
import time
from typing import Any

from ..base import ConnectionState, DeviceInfo, DriverError, InstrumentKind
from ..capabilities.liquid_handler import DeckLocation, LiquidHandlerDriver

log = logging.getLogger(__name__)

BAUD = 115200
GANTRY_AXES = ("X", "Y", "Z")
# How far each axis moves during initialize(). Small: this is a "does it move" check, not a
# calibration, and the machine is unhomed so nothing knows where the deck is.
INIT_STEP_MM = {"X": 3.0, "Y": 3.0, "Z": 2.0}
INIT_FEED = 600                        # mm/min, gentle

# Which SIGN moves an axis AWAY from its min endstop.
#
# Not the obvious +1 for every axis. Measured on this machine: Z was resting on min_z and a
# `G0 Z+2` drove *into* the switch and latched a halt — so on Z the positive direction is
# toward min, and the axis polarity is inverted relative to the endstop naming. X and Y were
# both clear of their switches, so their polarity is unverified; +1 is assumed for them and
# the assumption is CHECKED at runtime (initialize re-reads M119 and fails loudly if a
# release move did not release the switch), never trusted silently.
AWAY_FROM_MIN = {"X": +1.0, "Y": +1.0, "Z": -1.0}

# Which sign raises the pipette head. MEASURED with the handover camera, not assumed:
# `G0 Z+6` moved the head DOWN 14 px in frame and `G0 Z-6` moved it back UP 14 px (template
# match scores 0.80/0.81, net displacement 0 px at score 0.99). So Z-negative is up, and
# `min_z` sits at the BOTTOM of travel, near the deck — the head had been parked on it, and
# an early `G0 Z+2` was pushing it toward the deck until the switch stopped the move.
Z_UP_SIGN = -1.0

# Retracting up moves AWAY from min_z, so it cannot trip that switch. But `M119` reports
# only min_* endstops — there is NO max endstop at the top of travel, so nothing but this
# bound stops a retract from driving into the mechanical top. Hence "retract by a known
# distance" rather than "retract until something stops us".
DEFAULT_RETRACT_MM = 30.0
MAX_RETRACT_MM = 80.0          # refuse to be asked for more in one call
RETRACT_STEP_MM = 5.0          # checked against the endstops between steps

# Largest single commanded move per axis, always enforced (R-LH-3). The machine is unhomed,
# so there is no absolute envelope to check against by default; this bounds the damage a
# wrong number can do. An absolute `envelope` may additionally be configured.
MAX_STEP_MM = {"X": 50.0, "Y": 50.0, "Z": 30.0}

# How position is known, reported so no consumer mistakes it for a measurement (R-LH-4).
#
# The OT-One has no encoders: `M114`'s lower-case "actual" values are the controller's own
# step counts, which is dead reckoning, not feedback. And nothing has been homed, so even
# that count has no fixed origin — it is whatever the board booted with. The ONLY true
# position measurements on this machine are the endstop switches.
POSITION_PROVENANCE = {
    "source": "controller_step_counts",
    "encoders": False,
    "referenced": False,        # never homed: the origin is arbitrary
    "note": "dead-reckoned and unreferenced; only the endstops are measured",
}

# Replies that mean the firmware has latched a fault and will refuse everything until it is
# cleared. "!!" is Smoothieware's halt marker.
_FAULT_MARKERS = ("!!", "reset or m999 required", "limit switch")


class _SerialTransport:
    """Line conversation with the board. Owns the port and the read timing."""

    def __init__(self, port: str, baud: int = BAUD, timeout: float = 2.0) -> None:
        try:
            import serial
        except ImportError as e:                                  # pragma: no cover
            raise DriverError(
                "pyserial not installed — it is required to talk to the OT-One"
            ) from e
        try:
            self._ser = serial.Serial(port, baud, timeout=timeout)
        except Exception as e:
            raise DriverError(f"cannot open {port}: {e}") from e
        self.port = port
        # The board says nothing on open (no boot banner on an already-running board), so
        # a short drain only clears anything a previous session left behind.
        self._read(0.5)

    def _read(self, seconds: float) -> str:
        """Read until the board has been quiet for a moment, or the window expires.

        Replies are multi-line and carry no sentinel — `M114` answers with two lines and no
        trailing "ok" of its own — so "quiet for 400 ms" is the only reliable
        end-of-reply signal.
        """
        out, end = b"", time.time() + seconds
        while time.time() < end:
            waiting = self._ser.in_waiting
            if waiting:
                out += self._ser.read(waiting)
                end = time.time() + 0.4
            else:
                time.sleep(0.05)
        return out.decode("utf-8", "replace")

    def converse(self, line: str, wait: float) -> str:
        self._ser.reset_input_buffer()
        self._ser.write((line + "\n").encode())
        self._ser.flush()
        return self._read(wait).strip()

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:                                          # pragma: no cover
            pass


def open_transport(port: str, baud: int = BAUD) -> _SerialTransport:
    """Factory, so tests can substitute a fake without touching a serial port."""
    return _SerialTransport(port, baud)


def find_port() -> str | None:
    """Best guess at the board's port when none is configured.

    A convenience, not an identity: unlike the cameras (see ``core/cameras.py``) the tty
    name is all we get here, so this insists on exactly one candidate rather than picking
    one. `/dev/cu.*` is the right prefix on macOS — `/dev/tty.*` blocks on open waiting
    for carrier detect.
    """
    candidates = sorted(glob.glob("/dev/cu.usbmodem*")) or sorted(glob.glob("/dev/ttyACM*"))
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        log.warning("several possible OT-One ports (%s); set OT_SERIAL_PORT",
                    ", ".join(candidates))
    return None


class OpentronsDriver(LiquidHandlerDriver):
    """Config: {"transport": "serial", "port": "/dev/cu.usbmodem11301"}."""

    def __init__(self, device_id: str, config: dict[str, Any] | None = None) -> None:
        super().__init__(device_id, config)
        self._io: Any = None
        self._lock = threading.Lock()      # sync API handlers run in a threadpool
        self._firmware = ""
        self._endstops: dict[str, bool] = {}
        self._position: dict[str, float] = {}
        self._last_read = 0.0

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            id=self.device_id,
            name=self.config.get("name", "Opentrons (OT-One)"),
            kind=InstrumentKind.LIQUID_HANDLER,
            model="OT-One",
            vendor="Opentrons",
            meta={"firmware": self._firmware, "port": self.config.get("port"),
                  "axes": list(GANTRY_AXES) + ["A", "B"], "homing": False},
        )

    # --- lifecycle ---------------------------------------------------------
    def connect(self) -> None:
        self._state = ConnectionState.CONNECTING
        port = str(self.config.get("port") or "").strip() or find_port()
        if not port:
            self._state = ConnectionState.ERROR
            raise DriverError(
                f"{self.device_id}: no serial port configured or found. Set OT_SERIAL_PORT "
                "(on macOS it looks like /dev/cu.usbmodem11301, not /dev/ttyACM0)"
            )
        io = open_transport(port, int(self.config.get("baud") or BAUD))
        try:
            # `version` is the cheapest proof that the firmware's main loop is alive. A
            # board mid-blocked-move opens fine and answers nothing, so opening the port is
            # NOT evidence of a working machine.
            reply = io.converse("version", 3.0)
            if not reply:
                raise DriverError(
                    f"{self.device_id}: {port} opened but the board did not answer "
                    "`version`. Its main loop is probably blocked in a move; power-cycle "
                    "the OT-One"
                )
            self._firmware = reply
            self.config["port"] = port
            self._io = io
            self._refresh(force=True)
        except Exception:
            io.close()
            self._io = None
            self._state = ConnectionState.ERROR
            raise
        self._state = ConnectionState.CONNECTED

    def disconnect(self) -> None:
        if self._io is not None:
            self._io.close()
        self._io = None
        self._state = ConnectionState.DISCONNECTED

    def status(self) -> dict[str, Any]:
        # Cheap by contract (the UI polls it), so this serves the cached read and only goes
        # to the wire when it is stale.
        if self._io is not None:
            try:
                self._refresh()
            except DriverError:
                pass
        return {
            "state": self._state,
            "connected": self._io is not None,
            "firmware": self._firmware,
            "endstops": dict(self._endstops),
            "position": dict(self._position),
            "homed": False,          # nothing homes this machine; see home()
            # R-LH-4: state how position is known, right next to the numbers, so a consumer
            # cannot read them as measurements.
            "provenance": dict(POSITION_PROVENANCE),
        }

    # --- transport ---------------------------------------------------------
    def _send(self, command: str, wait: float = 2.5, allow_fault: bool = False) -> str:
        """Send one line and return its reply. Raises on silence or a latched fault.

        Silence is fatal rather than retried: see rule 1 in the module docstring. Sending
        anything else after a timeout is how the board ends up executing a queue of
        commands nobody is watching.
        """
        if self._io is None:
            raise DriverError(f"{self.device_id}: not connected")
        with self._lock:
            reply = self._io.converse(command, wait)
        if not reply:
            self._state = ConnectionState.ERROR
            raise DriverError(
                f"{self.device_id}: no reply to {command!r}. The firmware's main loop is "
                "blocked (a move that cannot finish); nothing more will be sent. "
                "Power-cycle the OT-One"
            )
        low = reply.lower()
        if not allow_fault and any(marker in low for marker in _FAULT_MARKERS):
            raise DriverError(f"{self.device_id}: firmware fault on {command!r}: {reply}")
        return reply

    def _refresh(self, force: bool = False, ttl: float = 0.5) -> None:
        if not force and time.time() - self._last_read < ttl:
            return
        self._endstops = self._read_endstops()
        self._position = self._read_position()
        self._last_read = time.time()

    def _read_endstops(self) -> dict[str, bool]:
        """`M119` -> {"min_x": False, ..., "min_b": True}."""
        out = {}
        for token in self._send("M119").split():
            if ":" in token:
                name, _, value = token.partition(":")
                if name.startswith("min_"):
                    out[name] = value.strip() == "1"
        return out

    def _read_position(self) -> dict[str, float]:
        """`M114` -> commanded and actual position.

        The reply carries BOTH: uppercase keys are the *commanded* position, lowercase the
        *actual* one. They diverge whenever a move was cut short — after a limit halt this
        machine reported `Z:2.000 ... z:0.006`, i.e. the planner had advanced 2 mm that the
        axis never travelled. Both are kept, because the difference is the diagnostic.
        """
        out: dict[str, float] = {}
        for token in self._send("M114").replace("C:", " ").split():
            name, _, value = token.partition(":")
            if not value or name == "ok":
                continue
            try:
                out[name] = float(value)
            except ValueError:
                continue
        return out

    def _clear_halt(self) -> None:
        """Clear a latched limit halt (`M999`).

        Used only to avoid *leaving* the machine latched after a failure, never to push
        through one: clearing a halt in order to keep moving is how a protective stop gets
        defeated. Callers raise immediately afterwards.
        """
        log.warning("%s: clearing a latched firmware halt (M999)", self.device_id)
        self._send("M999", allow_fault=True)

    # --- initialization ----------------------------------------------------
    def initialize(self, retract_z: bool | None = None) -> dict[str, Any]:
        """Prove each gantry axis moves, by jogging it a few mm and returning it (R-LH-2).

        This replaces homing (see :meth:`home`). It touches X/Y/Z only — never the A/B
        plungers — and every move is relative, so nothing depends on an absolute origin
        this unhomed machine does not have.

        Direction comes from `M119`, not from an assumption. An axis already resting on its
        min endstop is moved *away* first and the release is verified; an axis with room is
        jogged both ways and returned to where it started.

        R-LH-2 also asks for a Z retract at the end. It runs when `retract_z` is true, or
        when the config says `retract_z_on_init`; it defaults **off** because a retract has
        no endstop above it to stop against (see :meth:`retract_z`) and the safe distance
        depends on where the head happens to be parked, which nothing here knows.
        """
        if retract_z is None:
            retract_z = bool(self.config.get("retract_z_on_init", False))
        report: dict[str, Any] = {"firmware": self._firmware, "axes": {}}
        self._refresh(force=True)
        try:
            self._send("G91")                        # relative for every move below
            try:
                for axis in GANTRY_AXES:
                    report["axes"][axis] = self._init_axis(axis)
            finally:
                # Absolute mode restored even on failure: leaving the board in G91 would
                # silently reinterpret any later absolute move as a relative one.
                self._send("G90", allow_fault=True)
        except DriverError:
            # Do not leave the machine latched for the next caller to trip over.
            try:
                self._clear_halt()
                self._send("G90", allow_fault=True)
            except DriverError:
                pass
            raise
        if retract_z:
            report["retract"] = self.retract_z(
                self.config.get("retract_z_mm") or DEFAULT_RETRACT_MM)

        self._refresh(force=True)
        report["endstops"] = dict(self._endstops)
        report["position"] = dict(self._position)
        report["provenance"] = dict(POSITION_PROVENANCE)
        return report

    def _init_axis(self, axis: str) -> dict[str, Any]:
        step = float(INIT_STEP_MM[axis])
        away = AWAY_FROM_MIN[axis]
        switch = f"min_{axis.lower()}"
        on_switch = bool(self._endstops.get(switch))

        if on_switch:
            # Resting on the limit. Move off it, and CHECK that it released — if it did
            # not, the assumed polarity is wrong and continuing would drive further into
            # the switch, which is exactly the latched-halt failure.
            self._jog(axis, away * step)
            if self._read_endstops().get(switch):
                raise DriverError(
                    f"{self.device_id}: {axis} still on {switch} after moving "
                    f"{away * step:+g} mm — the assumed direction is wrong, or the axis is "
                    f"stuck. Not moving {axis} further"
                )
            # Come part way back, deliberately not all the way: parking on a triggered
            # limit switch is what makes the *next* move fail.
            self._jog(axis, -away * step * 0.5)
            return {"started_on_endstop": True, "moved_mm": step,
                    "net_mm": round(away * step * 0.5, 3), "released": True}

        # Clear of the switch: move AWAY first, then return. Both directions are exercised,
        # but the toward-the-switch leg only ever brings the axis back to where it started.
        #
        # Order matters, and not moving away first is a bug this driver had: the distance to
        # the switch is unknown, so a leading toward-the-switch move can hit it. Z is the
        # live example — after the earlier halt it was left ~1 mm below min_z, which is less
        # than its 2 mm step, so a `+` first move would have latched the halt again.
        self._jog(axis, away * step)
        self._jog(axis, -away * step)
        return {"started_on_endstop": False, "moved_mm": step, "net_mm": 0.0,
                "released": None}

    # --- relative motion (R-LH-1) ------------------------------------------
    def move_by(self, dx: float = 0.0, dy: float = 0.0, dz: float = 0.0) -> dict[str, Any]:
        """Move the pipette head by a relative offset in mm. The servo loop's one action.

        Relative *semantics*, and relative on the wire too (`G91`). The work breakdown
        suggests read -> clamp -> absolute command where a readback exists, and a readback
        does exist — but this machine's commanded and actual frames are offset from each
        other (see :meth:`_read_position`), and neither is referenced to anything, so an
        absolute target computed from one frame and issued in the other would be wrong by
        that offset. A relative command carries the same displacement without depending on
        either origin. The readback is still used, for the envelope check and to verify
        afterwards that the axis actually went where it was told.

        Refuses rather than clamping (R-LH-3), and refuses to move an axis toward an endstop
        that is already triggered.
        """
        requested = {"X": float(dx), "Y": float(dy), "Z": float(dz)}
        for axis, delta in requested.items():
            if not math.isfinite(delta):
                raise DriverError(f"{self.device_id}: {axis} delta is not a finite number")
            limit = MAX_STEP_MM[axis]
            if abs(delta) > limit:
                raise DriverError(
                    f"{self.device_id}: {axis}{delta:+g} mm exceeds the {limit:g} mm "
                    f"single-move limit — refusing rather than clamping"
                )

        moving = {a: d for a, d in requested.items() if d}
        if not moving:
            return {"requested": requested, "achieved": {}, "moved": False}

        self._refresh(force=True)
        before = dict(self._position)
        for axis, delta in moving.items():
            self._check_direction(axis, delta)
            self._check_envelope(axis, delta, before)

        for axis, delta in moving.items():
            self._send("G91")
            self._jog(axis, delta)
        self._send("G90", allow_fault=True)

        self._refresh(force=True)
        achieved = {a: round(self._position.get(a.lower(), 0.0) - before.get(a.lower(), 0.0), 3)
                    for a in moving}
        # A commanded move the axis did not make is the failure that matters here: it means
        # something is blocked, and every later offset would be computed against a lie.
        drift = {a: round(achieved[a] - moving[a], 3) for a in moving
                 if abs(achieved[a] - moving[a]) > 0.2}
        if drift:
            log.warning("%s: commanded %s but achieved %s (shortfall %s)",
                        self.device_id, moving, achieved, drift)
        return {"requested": requested, "achieved": achieved, "moved": True,
                "drift_mm": drift, "position": self.position()}

    def _check_direction(self, axis: str, delta: float) -> None:
        switch = f"min_{axis.lower()}"
        if not self._endstops.get(switch):
            return
        if (delta * AWAY_FROM_MIN[axis]) < 0:
            raise DriverError(
                f"{self.device_id}: {switch} is triggered and {axis}{delta:+g} moves toward "
                f"it — refusing (that latches a limit halt). Move "
                f"{axis}{AWAY_FROM_MIN[axis] * abs(delta):+g} to come off the switch"
            )

    def _check_envelope(self, axis: str, delta: float, before: dict[str, float]) -> None:
        """Optional absolute envelope, in the controller's own (unreferenced) frame.

        Off unless configured, and deliberately so: with nothing homed the frame's origin is
        whatever the board booted with, so a hardcoded envelope would be meaningless — or
        worse, wrong in a way that reads as a safety feature. `MAX_STEP_MM` is the guard that
        always applies.
        """
        envelope = (self.config.get("envelope") or {}).get(axis)
        if not envelope:
            return
        low, high = float(envelope[0]), float(envelope[1])
        target = before.get(axis.lower(), 0.0) + delta
        if not (low <= target <= high):
            raise DriverError(
                f"{self.device_id}: {axis} target {target:.3f} mm is outside the configured "
                f"envelope [{low:g}, {high:g}] — refusing rather than clamping"
            )

    def retract_z(self, distance_mm: float | None = None) -> dict[str, Any]:
        """Raise the pipette head by a bounded distance (R-LH-2, R-VIS-11).

        Deliberately NOT "up until something stops it". Up is away from `min_z` (measured —
        see :data:`Z_UP_SIGN`), and `M119` shows no max endstop, so there is no switch at the
        top of travel to stop against: an unbounded retract would drive into the mechanical
        top. A non-homing move that *does* reach an endstop latches a halt needing `M999`,
        so making that the normal path would also mean every retract logged a fault
        indistinguishable from a real one.

        Moves in steps, re-reading the endstops between them, so an unexpected switch stops
        it cleanly rather than by fault.
        """
        distance = DEFAULT_RETRACT_MM if distance_mm is None else float(distance_mm)
        if not math.isfinite(distance) or distance <= 0:
            raise DriverError(f"{self.device_id}: retract distance must be positive")
        if distance > MAX_RETRACT_MM:
            raise DriverError(
                f"{self.device_id}: retract of {distance:g} mm exceeds the {MAX_RETRACT_MM:g} "
                f"mm bound — there is no endstop at the top of travel to stop against"
            )

        self._refresh(force=True)
        start = self._position.get("z", 0.0)
        # Parked on min_z is the NORMAL starting state for this machine, and retracting is
        # precisely how to leave it — so a triggered min_z must not block the retract. It is
        # used as a check instead: moving up must RELEASE it, and if it does not then up is
        # not up and Z_UP_SIGN is wrong. (An earlier version aborted here instead, which
        # made retract_z a no-op in the one state it exists to fix.)
        started_on_min = bool(self._endstops.get("min_z"))
        released: bool | None = None
        moved = 0.0
        self._send("G91")
        try:
            while moved < distance - 1e-9:
                step = min(RETRACT_STEP_MM, distance - moved)
                self._jog("Z", Z_UP_SIGN * step)
                moved += step
                if started_on_min:
                    if self._read_endstops().get("min_z"):
                        raise DriverError(
                            f"{self.device_id}: min_z did not release after retracting "
                            f"{moved:g} mm — 'up' is not the direction this driver thinks "
                            f"it is (Z_UP_SIGN={Z_UP_SIGN:+g}), or Z is stuck"
                        )
                    started_on_min, released = False, True
        finally:
            self._send("G90", allow_fault=True)

        self._refresh(force=True)
        return {"requested_mm": distance, "retracted_mm": round(moved, 3),
                "z_travel_mm": round(self._position.get("z", 0.0) - start, 3),
                "released_min_z": released, "position": self.position()}

    def position(self) -> dict[str, Any]:
        """Position plus how it is known (R-LH-4).

        `commanded` and `actual` are reported separately because they diverge, and the
        difference is the diagnostic: after a limit halt this machine read `Z:2.000` against
        `z:0.006`, i.e. the planner had advanced 2 mm the axis never travelled.
        """
        commanded = {k: v for k, v in self._position.items() if k.isupper()}
        actual = {k.upper(): v for k, v in self._position.items() if k.islower()}
        return {
            "commanded": commanded,
            "actual": actual,
            "offset_mm": {a: round(commanded[a] - actual[a], 3)
                          for a in commanded if a in actual},
            "provenance": dict(POSITION_PROVENANCE),
            "endstops": dict(self._endstops),
        }

    def _jog(self, axis: str, delta: float) -> None:
        """One relative move, waited out to completion.

        `M400` blocks until the planner is empty, so its reply is proof the motion actually
        finished rather than merely being accepted — the distinction that matters when the
        next decision depends on where the axis ended up.
        """
        self._send(f"G0 {axis}{delta:+g} F{INIT_FEED}", wait=5.0)
        self._send("M400", wait=20.0)

    # --- liquid handling ---------------------------------------------------
    def home(self) -> None:
        """Not implemented, on purpose.

        `G28.2` — the homing code Opentrons' own software uses — was tried on this unit and
        left the firmware unresponsive to everything including `M112` and `Ctrl-X`,
        recoverable only by a power cycle. The board acknowledges the command with a bare
        `ok` and then blocks in its main loop, so there is no reply that distinguishes
        "homing" from "wedged".

        Until the correct sequence for this firmware is established (per-axis order, and
        the fact that Z parks on min_z with positive Z moving *toward* the switch),
        :meth:`initialize` is the safe substitute and this raises rather than guessing.
        """
        raise DriverError(
            f"{self.device_id}: homing is not implemented for this firmware — G28.2 wedges "
            "it (power cycle to recover). Use initialize() to verify the axes move"
        )

    def move_to(self, location: DeckLocation) -> None:
        raise DriverError(
            f"{self.device_id}: absolute moves need a homed machine and a deck coordinate "
            "frame; neither exists yet. initialize() only jogs relatively"
        )

    def pick_up_tip(self, location: DeckLocation) -> None:
        raise DriverError(f"{self.device_id}: tip handling not implemented")

    def drop_tip(self, location: DeckLocation | None = None) -> None:
        raise DriverError(f"{self.device_id}: tip handling not implemented")

    def aspirate(self, volume_ul: float, location: DeckLocation) -> None:
        raise DriverError(f"{self.device_id}: aspirate needs the A/B plunger axes, which "
                          "are out of scope until the plunger travel is characterised")

    def dispense(self, volume_ul: float, location: DeckLocation) -> None:
        raise DriverError(f"{self.device_id}: dispense needs the A/B plunger axes, which "
                          "are out of scope until the plunger travel is characterised")
