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

Two frames, and where they meet
-------------------------------
Everything below the ``_send`` line is in the **controller frame**: ``Z+`` is down, ``min_z`` is
at the top of travel. Everything the *capability* exposes — :meth:`move_relative`,
:class:`~drivers.capabilities.liquid_handler.RelativeMoveReport` — is in the **task frame**,
where ``+z is up``. :data:`Z_UP_SIGN` is the entire conversion and it is applied in exactly one
method. :meth:`position` and :meth:`status` deliberately stay in the controller frame, because
their numbers are read next to what the board itself reports and flipping them would make the
driver disagree with ``M114`` on a bench.

The wire itself lives in :mod:`drivers.opentrons.transport` (D6/R-LH-5), which also provides a
board simulator recording the exact lines emitted — the only way to catch a driver that reports
success while doing nothing, without an instrument.
"""
from __future__ import annotations

import glob
import logging
import math
import threading
import time
from typing import Any

from ..base import ConnectionState, DeviceInfo, DriverError, InstrumentKind
from ..capabilities.liquid_handler import (DeckLocation, LiquidHandlerDriver, MoveLimits,
                                           RelativeMoveReport)
from .transport import BAUD, GANTRY_AXES, SerialTransport
from .transport import open_transport as build_transport

log = logging.getLogger(__name__)

#: Kept as an alias so callers and tests that reached for the old private name still work. The
#: class itself now lives in `transport.py`, with the loopback and null transports beside it.
_SerialTransport = SerialTransport

# How far each axis moves during initialize(). Small: this is a "does it move" check, not a
# calibration, and the machine is unhomed so nothing knows where the deck is.
INIT_STEP_MM = {"X": 3.0, "Y": 3.0, "Z": 2.0}
INIT_FEED = 600                        # mm/min, gentle

# Z geometry, measured on the bench 2026-07-26 by two independent means that agree:
#
#   * Camera (handover_cam, which sees the OT deck): `G0 Z+6` moved the pipette head DOWN
#     in frame, `G0 Z-6` moved it back UP, net displacement zero. Confirmed by eye on the
#     cropped before/after pair, not just by template score.
#   * Endstops: with `min_z` triggered, `G0 Z+5` RELEASED it — so min_z lies in the -Z
#     direction, i.e. at the TOP of travel, and the head parks up there.
#
# Both together: **-Z is up and toward min_z; +Z is down and away from it.** Getting this
# backwards is not a cosmetic error — "retract" would drive the pipette into the deck.
Z_UP_SIGN = -1.0

# Which SIGN moves an axis AWAY from its min endstop.
#
# Z is +1 (down) because its switch is at the top, per the measurements above. X and Y were
# never observed near their switches, so +1 is an assumption for them — and it is CHECKED at
# runtime rather than trusted: a release move that does not release the switch aborts
# (see :meth:`_init_axis`).
AWAY_FROM_MIN = {"X": +1.0, "Y": +1.0, "Z": +1.0}

# Retracting up moves TOWARD min_z, so that switch is the natural "fully up" stop and the
# retract checks it between steps. Steps are small so the switch is noticed at a step
# boundary rather than mid-move (a non-homing move that reaches an endstop latches a halt).
# The distance bound remains, as a backstop for a switch that never reports.
DEFAULT_RETRACT_MM = 30.0
MAX_RETRACT_MM = 80.0          # refuse to be asked for more in one call
RETRACT_STEP_MM = 2.0          # checked against the endstops between steps

# Largest single commanded move per axis, always enforced (R-LH-3). The machine is unhomed,
# so there is no absolute envelope to check against by default; this bounds the damage a
# wrong number can do. An absolute `envelope` may additionally be configured.
MAX_STEP_MM = {"X": 50.0, "Y": 50.0, "Z": 30.0}

# Difference between commanded and achieved worth reporting, mm. Below this it is rounding in the
# controller's step maths; above it, an axis did not follow — and every later offset would then be
# computed against a position that was never reached, which is the failure that matters most here.
DRIFT_TOLERANCE_MM = 0.2

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


def open_transport(port: str, baud: int = BAUD) -> Any:
    """Open the **serial** transport. The seam a test replaces to avoid a real port.

    Kept at this exact two-argument signature on purpose: it is the monkeypatch point every
    driver test uses, and widening it would break them silently. Choosing a *different* transport
    is `connect`'s job, from ``config["transport"]``, so this function never has to decide.
    """
    return SerialTransport(port, baud)


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
        kind = str(self.config.get("transport") or "serial").strip().lower()
        port = str(self.config.get("port") or "").strip()
        baud = int(self.config.get("baud") or BAUD)

        if kind == "serial":
            port = port or find_port() or ""
            if not port:
                self._state = ConnectionState.ERROR
                raise DriverError(
                    f"{self.device_id}: no serial port configured or found. Set OT_SERIAL_PORT "
                    "(on macOS it looks like /dev/cu.usbmodem11301, not /dev/ttyACM0)"
                )
            # Through the module-level factory rather than the class, so this stays the one seam
            # a test replaces to keep every code path below hardware-free (R-SIM-7).
            io = open_transport(port, baud)
        else:
            # A board simulator or a deliberately mute transport (D6). No port is required, and
            # none is invented: a loopback that quietly claimed a tty would be a lie about what
            # the machine is.
            io = build_transport(port, baud, kind)
            port = io.port
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

        R-LH-2 also asks for a Z retract at the end, and it now runs **by default**. It was off
        for a while, on the reasoning that a retract had no endstop above it to stop against — and
        that reasoning was wrong, because it rested on the inverted Z convention this driver has
        since settled. ``min_z`` is at the *top* of travel (measured: with it triggered, `G0 Z+5`
        released it), so a retract moves *toward* a real switch and :meth:`retract_z` stops on it.
        With a hard stop to stop against, "leave the head clear of the deck" is the safe way to
        finish initializing, and R-LH-2 asks for it. Pass ``retract_z=False``, or set
        ``retract_z_on_init: false`` in the fleet entry, to opt out.
        """
        if retract_z is None:
            retract_z = bool(self.config.get("retract_z_on_init", True))
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

    # --- relative motion, task frame (R-LH-1) ------------------------------
    def move_relative(self, dx: float = 0.0, dy: float = 0.0,
                      dz: float = 0.0) -> RelativeMoveReport:
        """The capability's relative move: **task frame, +z is up** (R-LH-1).

        This method is the entire boundary between the two frames. It converts ``dz`` with
        :data:`Z_UP_SIGN`, hands the controller-frame displacement to :meth:`move_by`, and
        converts the achieved motion and the positions back. Nothing above it needs to know that
        this machine's ``Z+`` points at the deck, and nothing below it may assume otherwise.

        A positive ``dz`` therefore **raises** the head. That is the one sentence worth checking
        against a test before touching this file: reversed, "retract" drives the pipette into the
        deck, and the driver would report success while doing it.

        ``provenance`` is always ``dead_reckoned``. The OT-One has no encoders and has never been
        homed, so both the numbers and their origin are the controller's own step counts
        (:data:`POSITION_PROVENANCE`). Reporting anything else would let the servo loop treat an
        assumption as a measurement, which is exactly what R-LH-4 forbids.
        """
        raw = self.move_by(dx=dx, dy=dy, dz=Z_UP_SIGN * float(dz))
        requested = {"x": float(dx), "y": float(dy), "z": float(dz)}
        applied = {a.lower(): self._to_task(a, v)
                   for a, v in (raw.get("achieved") or {}).items()}
        drift = {a: round(applied[a] - requested[a], 3) for a in applied
                 if abs(applied[a] - requested[a]) > DRIFT_TOLERANCE_MM}
        position = raw.get("position") or self.position()
        return RelativeMoveReport(
            requested_mm=requested,
            applied_mm=applied,
            position_before=self._task_position(position.get("actual"), applied, before=True),
            position_after=self._task_position(position.get("actual"), applied),
            provenance="dead_reckoned",
            drift_mm=drift,
            moved=bool(raw.get("moved")),
            detail={"controller_frame": position, "endstops": dict(self._endstops)},
        )

    def move_limits(self) -> MoveLimits:
        """The bounds :meth:`move_relative` enforces (R-LH-3).

        Only ``max_step_mm``, which is frame-agnostic: it bounds a *magnitude*, so it means the
        same thing in either frame and can be checked by a caller without knowing which frame it
        holds.

        ``envelope_mm`` is deliberately left empty even when one is configured. A configured
        envelope is in the **controller's** frame, and Z's bounds do not merely negate when
        converted — the interval flips end for end — so publishing a converted pair would produce
        a bound nobody can check against the board by eye, in the one place where being wrong is
        expensive. The absolute envelope is therefore enforced inside :meth:`_check_envelope`, in
        its own frame, where the numbers match what ``M114`` prints.
        """
        return MoveLimits(max_step_mm={a.lower(): float(v) for a, v in MAX_STEP_MM.items()})

    @staticmethod
    def _to_task(axis: str, value: float) -> float:
        """One controller-frame number in the task frame. Z flips; X and Y do not."""
        return round(Z_UP_SIGN * float(value), 3) if axis.upper() == "Z" else round(float(value), 3)

    def _task_position(self, actual: Any, applied: dict[str, float],
                       before: bool = False) -> dict[str, float]:
        """Task-frame position, after the move, or reconstructed for before it.

        ``M114`` is only read *after* a move, so "before" is the readback minus what was applied.
        Reconstructed rather than captured separately because an extra ``M114`` round trip before
        every nudge costs a wire conversation per iteration for a number the arithmetic already
        has — and on a machine whose position is dead reckoning either way, the subtraction is
        exactly as trustworthy as the read.
        """
        if not isinstance(actual, dict):
            return {}
        out: dict[str, float] = {}
        for axis, value in actual.items():
            try:
                number = self._to_task(axis, float(value))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(number):
                continue
            key = axis.lower()
            out[key] = round(number - applied.get(key, 0.0), 3) if before else number
        return out

    def move_by(self, dx: float = 0.0, dy: float = 0.0, dz: float = 0.0) -> dict[str, Any]:
        """Move by a relative offset in mm, **in the controller's frame** — so ``dz`` is *down*.

        The wire-level half of :meth:`move_relative`, and the reason the two exist separately:
        this one speaks the frame ``M114`` and ``G0`` speak, which is the frame to think in when
        reading a board's replies, and the capability's method speaks the task frame, which is the
        frame to think in when reading a plan. Callers above the driver should use
        :meth:`move_relative`; this stays public because the bench scripts and the driver's own
        tests reason in board coordinates.

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
                 if abs(achieved[a] - moving[a]) > DRIFT_TOLERANCE_MM}
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
        """Raise the pipette head, stopping at the top switch or the distance bound.

        Up is `-Z`, toward `min_z` at the top of travel (:data:`Z_UP_SIGN`). So `min_z` is
        the real "fully up" stop, and this steps toward it in small increments, re-reading
        the endstops between steps so the switch is seen at a step boundary rather than
        mid-move — a non-homing move that reaches an endstop latches a halt needing `M999`.

        **`retracted_mm` is what was commanded, not what was travelled.** The OT-One has no
        encoders (:data:`POSITION_PROVENANCE`), so a stalled axis is counted as if it moved:
        measured on this bench, a 10 mm retract from a head already near the top advanced the
        step counter by 10 mm while the camera showed ~2 mm of travel and the motor skipped
        the rest. `at_top` (from the switch) is the only trustworthy statement about where
        the head actually is.
        """
        distance = DEFAULT_RETRACT_MM if distance_mm is None else float(distance_mm)
        if not math.isfinite(distance) or distance <= 0:
            raise DriverError(f"{self.device_id}: retract distance must be positive")
        if distance > MAX_RETRACT_MM:
            raise DriverError(
                f"{self.device_id}: retract of {distance:g} mm exceeds the {MAX_RETRACT_MM:g} "
                f"mm bound — a retract should stop at min_z well before this, so a larger "
                f"request means something is wrong rather than something is far away"
            )

        self._refresh(force=True)
        start = self._position.get("z", 0.0)
        # Already on the top switch: fully retracted by definition, and pushing further is
        # what stalls the motor against the mechanical top. This is the head's parked state,
        # so it is the common case, not an edge case.
        if self._endstops.get("min_z"):
            return {"requested_mm": distance, "retracted_mm": 0.0, "z_travel_mm": 0.0,
                    "at_top": True, "already_there": True, "position": self.position()}

        moved, at_top = 0.0, False
        self._send("G91")
        try:
            while moved < distance - 1e-9:
                step = min(RETRACT_STEP_MM, distance - moved)
                try:
                    self._jog("Z", Z_UP_SIGN * step)
                except DriverError as e:
                    # Reaching the top switch mid-step latches a halt. For a *retract* that
                    # is success, not failure — the goal was "as far up as it goes" — so it
                    # is cleared and reported rather than raised.
                    if "limit switch" not in str(e).lower():
                        raise
                    log.info("%s: retract reached the top switch mid-step", self.device_id)
                    self._clear_halt()
                    at_top = True
                    break
                moved += step
                if self._read_endstops().get("min_z"):
                    at_top = True
                    break
        finally:
            self._send("G90", allow_fault=True)

        self._refresh(force=True)
        return {"requested_mm": distance, "retracted_mm": round(moved, 3),
                "z_travel_mm": round(self._position.get("z", 0.0) - start, 3),
                "at_top": at_top, "already_there": False, "position": self.position()}

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
