"""How the OT-One driver talks to a board — and how it is tested without one (D6, R-LH-5).

Why this is a separate module
-----------------------------
The driver used to own its port directly, and the failure that produced was the worst shape a
lab driver can have: ``connect()`` assigned a dummy object and ``_send()`` returned ``None``, so
**every liquid-handling call reported success while doing nothing**. Nothing raised, nothing
logged, and the plan's record said the pipette moved. That defect is invisible to any test that
only checks return values, and invisible on the bench too until something is in the way.

Splitting the wire out fixes it structurally in two ways:

* the driver can no longer *have* a transport that silently swallows commands — the only
  transports are a real serial port, a board simulator, and one that refuses everything loudly;
* :class:`LoopbackTransport` records **every line written, in order**, so a test can assert the
  exact wire conversation. That is what catches a no-op, a wrong sign, a missing ``G91``, or a
  move issued while the board is in the wrong mode, none of which a return value reveals.

Three transports
----------------
:class:`SerialTransport`
    The real thing: a USB CDC line conversation at 115200. Owns the port and the read timing.
:class:`LoopbackTransport`
    A simulated Smoothieboard. Answers ``version`` / ``M119`` / ``M114`` / ``G0`` / ``M400`` /
    ``M999`` the way the real firmware does, tracks per-axis position **and endstop position**,
    and latches a limit halt when a move reaches a switch — because that is the failure mode the
    driver's rules exist for, and a simulator that cannot reproduce it cannot test them.
:class:`NullTransport`
    Answers nothing at all. Every command therefore raises "no reply" in the driver. This exists
    to make "connected to nothing" a loud failure rather than a silent success, i.e. to make the
    original defect unrepresentable.

Everything on this wire is in the **controller's frame**, where ``Z+`` is down and ``min_z`` is
at the top of travel (measured — see `drivers.opentrons.driver.Z_UP_SIGN`). The task frame's
``+z is up`` conversion happens in the driver, above this module, and must never be applied here.
"""
from __future__ import annotations

import time
from typing import Iterable, Protocol, Sequence, runtime_checkable

from ..base import DriverError

BAUD = 115200

#: The axes a `G0` may name in these tests and on this machine. A and B are the pipette
#: plungers and are deliberately absent from every code path — driving a plunger into its stop
#: is the failure that produced this driver's rules.
GANTRY_AXES = ("X", "Y", "Z")


@runtime_checkable
class Transport(Protocol):
    """One line out, its reply back. Deliberately the whole interface.

    ``converse`` is a *request/response* rather than separate write and read calls, because
    Smoothieware services serial from the same main loop that executes motion: a blocked move
    means nothing is read from the port, and commands sent into that silence are not rejected —
    they queue, and run later, in a state nobody predicted. Pairing every write with its read is
    what lets the driver refuse to send anything after a timeout.
    """

    port: str

    def converse(self, line: str, wait: float) -> str:
        """Send one line; return whatever the board said, stripped. ``""`` means silence."""
        ...

    def close(self) -> None:
        ...


class SerialTransport:
    """Line conversation with a real board. Owns the port and the read timing."""

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
        # The board says nothing on open (no boot banner on an already-running board), so a
        # short drain only clears anything a previous session left behind.
        self._read(0.5)

    def _read(self, seconds: float) -> str:
        """Read until the board has been quiet for a moment, or the window expires.

        Replies are multi-line and carry no sentinel — `M114` answers with two lines and no
        trailing "ok" of its own — so "quiet for 400 ms" is the only reliable end-of-reply
        signal.
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


class NullTransport:
    """A transport that answers nothing, so nothing can appear to succeed.

    The point of the class. The driver's ``_send`` treats silence as fatal, so every command
    through this raises with the reason. It is what ``connect()`` used to do *accidentally* —
    assign a dummy object whose calls returned ``None`` — except that the accident reported
    success. Configure ``{"transport": "null"}`` to have a liquid handler that is present in the
    fleet and provably incapable of pretending to move.
    """

    def __init__(self, port: str = "", baud: int = BAUD) -> None:
        self.port = port or "<null>"
        self.lines: list[str] = []
        self.closed = False

    def converse(self, line: str, wait: float) -> str:
        self.lines.append(line)
        return ""

    def close(self) -> None:
        self.closed = True


class LoopbackTransport:
    """A simulated Smoothieboard that records the exact wire lines it was sent.

    Faithful in the four ways the driver's safety rules depend on, and no further:

    * **Endstop *position*, not just a boolean.** ``switch_at`` says at which coordinate a min
      switch engages, so "clear of the switch, but only just" is representable — the state the
      real Z was left in after a halt (~1 mm from ``min_z`` with a 2 mm init step), and the state
      that catches a leading toward-the-switch move.
    * **Latching.** A move that reaches or is issued into a triggered switch latches a halt and
      then answers ``!!`` to everything until ``M999``, exactly as the firmware does. Without
      this the driver's whole "never move toward a triggered endstop" rule is untestable.
    * **Silence.** ``silent_after`` makes the board acknowledge one command and then stop
      speaking, which is what a blocked main loop looks like.
    * **Modal state.** ``G90``/``G91`` is remembered, so a test can assert the board was left in
      absolute mode — leaving it in ``G91`` would silently reinterpret a later absolute move as a
      relative one.

    ``lines`` is every line written, in order, and :meth:`assert_wire` compares it to an expected
    sequence. That is the assertion R-LH-5 asks for, and the one that catches a no-op.
    """

    def __init__(self, *, triggered: Iterable[str] = (), position: dict[str, float] | None = None,
                 switch_at: dict[str, float] | None = None, away: dict[str, float] | None = None,
                 silent_after: str | None = None, latch_on_touch: bool = True,
                 stalled_axes: Iterable[str] = (), port: str = "<loopback>",
                 baud: int = BAUD) -> None:
        self.port = port
        self.lines: list[str] = []
        self.position = dict(position or {a: 0.0 for a in GANTRY_AXES})
        self.triggered = set(triggered)
        self.switch_at = dict(switch_at or {})
        # Which sign moves an axis AWAY from its min switch, in this simulated machine's wiring.
        # Mirrors the measured bench geometry: +1 on all three, which on Z means min_z is at the
        # TOP of travel and "up" is therefore -Z, toward it.
        self.away = dict(away or {a: +1.0 for a in GANTRY_AXES})
        self.silent_after = silent_after
        self.latch_on_touch = latch_on_touch
        #: Axes that accept a move and do not travel — the stalled-motor case a step counter
        #: cannot see. The whole reason `RelativeMoveReport.drift_mm` exists.
        self.stalled = set(stalled_axes)
        self.silent = False
        self.halted = False
        self.relative = False
        self.closed = False
        self.firmware = '{"version":v1.0.3_}'

    # --- the transport contract ---------------------------------------------
    def converse(self, line: str, wait: float) -> str:
        self.lines.append(line)
        if self.silent:
            return ""
        if self.silent_after is not None and line == self.silent_after:
            self.silent = True
            return "ok"                       # acknowledged, then never speaks again
        if self.halted and line != "M999":
            return "!!"
        if line == "version":
            return self.firmware
        if line == "M999":
            self.halted = False
            return "ok"
        if line == "M119":
            return " ".join(f"min_{a.lower()}:{1 if f'min_{a.lower()}' in self.triggered else 0}"
                            for a in ("X", "Y", "Z", "A", "B")) + " \nok"
        if line == "M114":
            upper = " ".join(f"{a}:{self.position[a]:.3f}" for a in GANTRY_AXES)
            lower = " ".join(f"{a.lower()}:{self.position[a]:.3f}" for a in GANTRY_AXES)
            return f"ok C: {upper} {lower}"
        if line in ("G90", "G91"):
            self.relative = line == "G91"
            return "ok"
        if line == "M400":
            return "ok"
        if line.startswith("G0 "):
            return self._move(line)
        return "ok"

    def close(self) -> None:
        self.closed = True

    # --- board behaviour ----------------------------------------------------
    def _move(self, line: str) -> str:
        body = line.split()[1]
        axis, delta = body[0], float(body[1:])
        switch = f"min_{axis.lower()}"
        toward_switch = (delta * self.away.get(axis, +1.0)) < 0

        if switch in self.triggered and toward_switch and self.latch_on_touch:
            self.halted = True
            return f"Limit switch {switch} was hit - reset or M999 required"

        if axis in self.stalled:
            return "ok"                       # accepted, travelled nothing

        target = self.position[axis] + delta
        limit = self.switch_at.get(axis)
        if limit is not None and toward_switch and self.latch_on_touch:
            # `away` gives the sign of "safe", so the switch lies on the -away side and is
            # reached when the target passes it.
            reached = target <= limit if self.away.get(axis, +1.0) > 0 else target >= limit
            if reached:
                self.position[axis] = limit
                self.triggered.add(switch)
                self.halted = True
                return f"Limit switch {switch} was hit - reset or M999 required"

        self.position[axis] = target
        if switch in self.triggered and not toward_switch:
            self.triggered.discard(switch)
        return "ok"

    # --- what a test asserts against ----------------------------------------
    def moves(self) -> list[str]:
        """Only the motion lines, in order. The subset most assertions care about."""
        return [line for line in self.lines if line.startswith("G0 ")]

    def assert_wire(self, expected: Sequence[str], *, only_moves: bool = False) -> None:
        """Assert the exact lines written, in order (R-LH-5).

        ``only_moves`` narrows it to `G0` lines, for a test about *where* rather than about the
        modal framing. A failure prints both sequences, because "which line is wrong" is the only
        useful form of this failure.
        """
        actual = self.moves() if only_moves else list(self.lines)
        if actual != list(expected):
            raise AssertionError(
                "wire mismatch\n  expected: " + " | ".join(expected)
                + "\n  actual:   " + " | ".join(actual))


def open_transport(port: str, baud: int = BAUD, kind: str = "serial") -> Transport:
    """Build a transport by name. ``serial`` is the only one that touches hardware.

    A factory so a test can substitute a board simulator without a serial port, and so
    ``{"transport": "loopback"}`` in a fleet entry is enough to bring the driver up with no
    instrument attached (R-SIM-7).
    """
    kind = (kind or "serial").strip().lower()
    if kind in ("loopback", "sim", "fake"):
        return LoopbackTransport(port=port or "<loopback>", baud=baud)
    if kind in ("null", "none", "offline"):
        return NullTransport(port=port, baud=baud)
    if kind == "serial":
        return SerialTransport(port, baud)
    raise DriverError(
        f"unknown liquid-handler transport {kind!r} — expected serial, loopback or null")


__all__ = ["BAUD", "GANTRY_AXES", "LoopbackTransport", "NullTransport", "SerialTransport",
           "Transport", "open_transport"]
