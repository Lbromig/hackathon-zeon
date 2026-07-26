"""OT-One driver: the two rules that wedge the real machine, enforced without hardware.

Both failures behind these tests happened on the bench:

* A homing command (`G28.2`) blocked the firmware's main loop. Because Smoothieware reads
  serial from that same loop, every later command went unanswered — and was *queued*, not
  rejected, so a batch of moves ran later unsupervised. Hence: silence is fatal, and
  nothing may be sent after it.
* A move toward an already-triggered endstop latched a limit halt, after which the board
  refused everything. Hence: direction comes from `M119`, never from an assumption.

A fake transport stands in for the serial port, so no test can reach real hardware
(R-SIM-7) and the wedged states can be reproduced deliberately.
"""
from __future__ import annotations

import pytest

from drivers.base import ConnectionState, DriverError
from drivers.opentrons import driver as ot


class FakeBoard:
    """Scriptable stand-in for the Smoothieboard.

    Tracks per-axis position and endstop state so that a move toward a triggered switch can
    latch a halt exactly as the firmware does.
    """

    def __init__(self, *, triggered=(), silent_after=None, latch_on_touch=True,
                 pos=None, switch_at=None):
        self.log: list[str] = []
        self.pos = dict(pos or {"X": 0.0, "Y": 0.0, "Z": 0.0})
        self.triggered = set(triggered)      # e.g. {"min_z"}
        self.silent_after = silent_after     # command that makes the board go silent
        self.latch_on_touch = latch_on_touch
        self.silent = False
        self.halted = False
        self.relative = False
        self.closed = False
        # Which direction moves an axis away from its min switch, in the FAKE machine's
        # wiring. Mirrors the measured bench geometry: every min switch is reached by moving
        # NEGATIVE, and on Z that means min_z is at the TOP (so up is -Z, toward it).
        self.away = {"X": +1.0, "Y": +1.0, "Z": +1.0}
        # Coordinate at which each min switch engages. Modelling the switch POSITION (not
        # just a boolean) is what makes "clear of the switch, but only just" representable —
        # the state the real Z was left in, and the one that catches a leading
        # toward-the-switch move.
        self.switch_at = dict(switch_at or {})

    def converse(self, line: str, wait: float) -> str:
        self.log.append(line)
        if self.silent:
            return ""
        if self.silent_after is not None and line == self.silent_after:
            self.silent = True
            return "ok"                       # acknowledged, then never speaks again
        if self.halted and line != "M999":
            return "!!"
        if line == "version":
            return '{"version":v1.0.3_}'
        if line == "M999":
            self.halted = False
            return "ok"
        if line == "M119":
            return " ".join(f"min_{a.lower()}:{1 if f'min_{a.lower()}' in self.triggered else 0}"
                            for a in ("X", "Y", "Z", "A", "B")) + " \nok"
        if line == "M114":
            upper = " ".join(f"{a}:{self.pos[a]:.3f}" for a in ("X", "Y", "Z"))
            lower = " ".join(f"{a.lower()}:{self.pos[a]:.3f}" for a in ("X", "Y", "Z"))
            return f"ok C: {upper} {lower}"
        if line in ("G90", "G91"):
            self.relative = line == "G91"
            return "ok"
        if line == "M400":
            return "ok"
        if line.startswith("G0 "):
            return self._move(line)
        return "ok"

    def _move(self, line: str) -> str:
        body = line.split()[1]
        axis, delta = body[0], float(body[1:])
        switch = f"min_{axis.lower()}"
        moving_toward_switch = (delta * self.away[axis]) < 0

        if switch in self.triggered and moving_toward_switch and self.latch_on_touch:
            self.halted = True
            return f"Limit switch {switch} was hit - reset or M999 required"

        target = self.pos[axis] + delta
        # Would this move reach the switch's position? `away` gives the sign of "safe", so
        # the switch is on the -away side and is reached when the target passes it.
        limit = self.switch_at.get(axis)
        if limit is not None and moving_toward_switch and self.latch_on_touch:
            reached = target <= limit if self.away[axis] > 0 else target >= limit
            if reached:
                self.pos[axis] = limit
                self.triggered.add(switch)
                self.halted = True
                return f"Limit switch {switch} was hit - reset or M999 required"

        self.pos[axis] = target
        if switch in self.triggered and not moving_toward_switch:
            self.triggered.discard(switch)
        return "ok"

    def close(self) -> None:
        self.closed = True


def _driver(fake, monkeypatch):
    """A connected driver talking to `fake` instead of a serial port."""
    monkeypatch.setattr(ot, "open_transport", lambda port, baud=ot.BAUD: fake)
    d = ot.OpentronsDriver("ot", {"port": "/dev/fake"})
    d.connect()
    return d


def test_connect_reads_firmware_and_state(monkeypatch):
    fake = FakeBoard()
    d = _driver(fake, monkeypatch)
    assert d.state is ConnectionState.CONNECTED
    status = d.status()
    assert "v1.0.3" in status["firmware"]
    assert status["endstops"] == {f"min_{a}": False for a in ("x", "y", "z", "a", "b")}
    assert status["homed"] is False


def test_connect_fails_when_board_opens_but_never_answers(monkeypatch):
    """Opening the port is not evidence of a working machine — this is what wedged looks like."""
    fake = FakeBoard()
    fake.silent = True
    monkeypatch.setattr(ot, "open_transport", lambda port, baud=ot.BAUD: fake)
    d = ot.OpentronsDriver("ot", {"port": "/dev/fake"})
    with pytest.raises(DriverError, match="did not answer"):
        d.connect()
    assert d.state is ConnectionState.ERROR
    assert fake.closed, "a failed connect must not leak the open port"


def test_silence_aborts_and_sends_nothing_further(monkeypatch):
    """Rule 1: after a timeout, no further command may be written.

    The real damage was not the unanswered command, it was the ones sent afterwards, which
    queued behind the blocked move and executed later.
    """
    fake = FakeBoard(silent_after="G0 X+3 F600")
    d = _driver(fake, monkeypatch)
    with pytest.raises(DriverError, match="no reply"):
        d.initialize()
    after_silence = fake.log[fake.log.index("G0 X+3 F600") + 1:]
    # Only the recovery attempt may follow, and it must not be a move.
    assert not [c for c in after_silence if c.startswith("G0 ")], (
        f"commands were sent into the silence: {after_silence}")


def test_initialize_moves_each_axis_both_ways_and_returns(monkeypatch):
    fake = FakeBoard()
    d = _driver(fake, monkeypatch)
    report = d.initialize()
    for axis in ("X", "Y", "Z"):
        assert report["axes"][axis]["net_mm"] == 0.0
        assert pytest.approx(fake.pos[axis], abs=1e-6) == 0.0
        moves = [c for c in fake.log if c.startswith(f"G0 {axis}")]
        assert any("+" in m for m in moves), f"{axis} never moved positive"
        assert any("-" in m for m in moves), f"{axis} never moved negative"


def test_axis_clear_of_switch_but_close_to_it_moves_away_first(monkeypatch):
    """The exact state the bench was left in: Z open, but only ~1 mm from min_z.

    The step is 2 mm, so a leading toward-the-switch move re-latches the halt. This is a
    regression test for that bug, which this driver had until the move order was flipped.
    """
    fake = FakeBoard(pos={"X": 0.0, "Y": 0.0, "Z": 1.0}, switch_at={"Z": 0.0})
    d = _driver(fake, monkeypatch)
    report = d.initialize()

    first_z = next(c for c in fake.log if c.startswith("G0 Z"))
    assert first_z.startswith("G0 Z+"), (
        f"must move away from a nearby switch first, sent {first_z}")
    assert not fake.halted, "drove into min_z despite it being only 1 mm away"
    assert report["axes"]["Z"]["net_mm"] == 0.0
    assert pytest.approx(fake.pos["Z"], abs=1e-6) == 1.0, "must end where it started"


def test_initialize_never_touches_the_plungers(monkeypatch):
    """A and B are out of scope: driving a plunger into its stop is what started all this."""
    fake = FakeBoard(triggered={"min_z"})
    d = _driver(fake, monkeypatch)
    d.initialize()
    assert not [c for c in fake.log if c.startswith("G0 A") or c.startswith("G0 B")]


def test_axis_on_endstop_moves_away_not_into_it(monkeypatch):
    """Rule 2, the exact bench failure: Z parked on min_z, and + moves toward it."""
    fake = FakeBoard(triggered={"min_z"})
    d = _driver(fake, monkeypatch)
    report = d.initialize()

    first_z = next(c for c in fake.log if c.startswith("G0 Z"))
    assert first_z.startswith("G0 Z+"), f"first Z move must be away from min_z, got {first_z}"
    assert report["axes"]["Z"]["started_on_endstop"] is True
    assert report["axes"]["Z"]["released"] is True
    assert not fake.halted, "must not latch a limit halt"
    # Left off the switch, not parked back on it: resting on a triggered limit is what
    # makes the NEXT move fail.
    assert report["endstops"]["min_z"] is False
    assert fake.pos["Z"] > 0, "released downward, away from the top switch"


def test_wrong_assumed_direction_is_detected_not_repeated(monkeypatch):
    """If a release move does not release the switch, stop — do not push further.

    Simulates an axis whose polarity is opposite to AWAY_FROM_MIN by refusing to release.
    """
    fake = FakeBoard(triggered={"min_x"}, latch_on_touch=False)
    fake.away["X"] = -1.0          # driver assumes +1 for X; fake says otherwise

    def stuck_move(line):
        body = line.split()[1]
        axis, delta = body[0], float(body[1:])
        fake.pos[axis] += delta
        return "ok"                # switch stays triggered

    fake._move = stuck_move        # type: ignore[method-assign]
    d = _driver(fake, monkeypatch)
    with pytest.raises(DriverError, match="still on min_x"):
        d.initialize()
    x_moves = [c for c in fake.log if c.startswith("G0 X")]
    assert len(x_moves) == 1, f"must stop after the first failed release, sent {x_moves}"


def test_limit_halt_is_reported_and_not_left_latched(monkeypatch):
    """A latched halt must surface as an error, and be cleared so the next caller is not stuck.

    Clearing is for hygiene only — the error still propagates. Pushing through a protective
    halt is precisely what must not happen.
    """
    fake = FakeBoard()

    def halting_move(line):
        fake.halted = True
        return "Limit switch min_y was hit - reset or M999 required"

    fake._move = halting_move      # type: ignore[method-assign]
    d = _driver(fake, monkeypatch)
    with pytest.raises(DriverError, match="firmware fault"):
        d.initialize()
    assert "M999" in fake.log, "must not leave the board latched"
    assert fake.halted is False


def test_relative_mode_is_always_restored(monkeypatch):
    """Leaving the board in G91 would reinterpret a later absolute move as relative."""
    fake = FakeBoard()
    d = _driver(fake, monkeypatch)
    d.initialize()
    assert fake.log.index("G91") < fake.log.index("G90")
    assert fake.relative is False


def test_homing_refuses_rather_than_wedging(monkeypatch):
    fake = FakeBoard()
    d = _driver(fake, monkeypatch)
    with pytest.raises(DriverError, match="homing is not implemented"):
        d.home()
    assert not [c for c in fake.log if "G28" in c], "no homing code may reach the board"


def test_unimplemented_operations_fail_loudly(monkeypatch):
    """A stub that silently succeeds is worse than one that raises — the original bug here."""
    fake = FakeBoard()
    d = _driver(fake, monkeypatch)
    loc = ot.DeckLocation(slot="1", well="A1")
    for call in (lambda: d.move_to(loc), lambda: d.pick_up_tip(loc), lambda: d.drop_tip(),
                 lambda: d.aspirate(100, loc), lambda: d.dispense(100, loc)):
        with pytest.raises(DriverError):
            call()


# ---------------------------------------------------------------------------
# R-LH-1: relative move — the action the servo loop drives
# ---------------------------------------------------------------------------


def test_move_by_emits_relative_moves_and_reports_achieved(monkeypatch):
    fake = FakeBoard()
    d = _driver(fake, monkeypatch)
    result = d.move_by(dx=2.0, dz=-1.5)

    assert result["moved"] is True
    assert result["achieved"] == {"X": 2.0, "Z": -1.5}
    assert not result["drift_mm"]
    moves = [c for c in fake.log if c.startswith("G0 ")]
    assert moves == ["G0 X+2 F600", "G0 Z-1.5 F600"]
    assert "G91" in fake.log and fake.relative is False, "must end back in absolute mode"


def test_move_by_zero_is_a_no_op(monkeypatch):
    fake = FakeBoard()
    d = _driver(fake, monkeypatch)
    assert d.move_by()["moved"] is False
    assert not [c for c in fake.log if c.startswith("G0 ")]


def test_move_by_reports_shortfall_when_an_axis_does_not_follow(monkeypatch):
    """A commanded move the axis never made would make every later offset a lie."""
    fake = FakeBoard()
    d = _driver(fake, monkeypatch)
    fake._move = lambda line: "ok"          # accepts, moves nothing
    result = d.move_by(dx=5.0)
    assert result["drift_mm"] == {"X": -5.0}


# ---------------------------------------------------------------------------
# R-LH-3: refuse, never silently clamp
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kwargs", [{"dx": 999.0}, {"dy": -80.0}, {"dz": 31.0}])
def test_move_by_refuses_beyond_the_single_move_limit(monkeypatch, kwargs):
    fake = FakeBoard()
    d = _driver(fake, monkeypatch)
    with pytest.raises(DriverError, match="refusing rather than clamping"):
        d.move_by(**kwargs)
    assert not [c for c in fake.log if c.startswith("G0 ")], "must not move at all"


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_move_by_refuses_non_finite(monkeypatch, bad):
    fake = FakeBoard()
    d = _driver(fake, monkeypatch)
    with pytest.raises(DriverError, match="finite"):
        d.move_by(dx=bad)


def test_move_by_refuses_outside_a_configured_envelope(monkeypatch):
    fake = FakeBoard(pos={"X": 8.0, "Y": 0.0, "Z": 0.0})
    monkeypatch.setattr(ot, "open_transport", lambda port, baud=ot.BAUD: fake)
    d = ot.OpentronsDriver("ot", {"port": "/dev/fake", "envelope": {"X": [-10.0, 10.0]}})
    d.connect()
    with pytest.raises(DriverError, match="outside the configured envelope"):
        d.move_by(dx=5.0)                    # 8 + 5 = 13, past the +10 bound
    assert not [c for c in fake.log if c.startswith("G0 ")]
    d.move_by(dx=1.0)                        # 9 is inside, still allowed


def test_move_by_refuses_to_drive_into_a_triggered_endstop(monkeypatch):
    """The bench failure, now blocked at the interface instead of by the firmware."""
    fake = FakeBoard(triggered={"min_z"})
    d = _driver(fake, monkeypatch)
    with pytest.raises(DriverError, match="moves toward it"):
        d.move_by(dz=-2.0)                   # -Z is up, toward the top switch
    assert not fake.halted
    d.move_by(dz=+2.0)                       # down, away from it, is fine


# ---------------------------------------------------------------------------
# R-LH-2 / R-VIS-11: bounded retract
# ---------------------------------------------------------------------------


def test_retract_z_moves_up_in_bounded_steps(monkeypatch):
    fake = FakeBoard()
    d = _driver(fake, monkeypatch)
    result = d.retract_z(12.0)

    assert result["retracted_mm"] == 12.0
    # Up is -Z: measured by camera (Z+ lowers the head) and by endstop (Z+ releases min_z).
    z_moves = [c for c in fake.log if c.startswith("G0 Z")]
    assert all("Z-" in m for m in z_moves), f"retract must move up, sent {z_moves}"
    assert len(z_moves) == 6, "12 mm in 2 mm steps"
    assert pytest.approx(fake.pos["Z"], abs=1e-6) == -12.0
    assert result["at_top"] is False, "never reached the switch, so cannot claim to be up"


def test_retract_z_refuses_an_unbounded_request(monkeypatch):
    """The bound is the backstop for a switch that never reports."""
    fake = FakeBoard()
    d = _driver(fake, monkeypatch)
    with pytest.raises(DriverError, match="exceeds"):
        d.retract_z(500.0)
    for bad in (0.0, -5.0, float("nan")):
        with pytest.raises(DriverError, match="must be positive"):
            d.retract_z(bad)


def test_retract_z_is_a_no_op_when_already_on_the_top_switch(monkeypatch):
    """min_z triggered means fully up already — pushing further only stalls the motor.

    This is the head's PARKED state, so it is the common case. Measured on the bench: a
    10 mm retract from a head already at the top advanced the step counter 10 mm while the
    camera showed ~2 mm, the motor skipping the rest.
    """
    fake = FakeBoard(triggered={"min_z"})
    d = _driver(fake, monkeypatch)
    result = d.retract_z(10.0)

    assert result["already_there"] is True
    assert result["at_top"] is True
    assert result["retracted_mm"] == 0.0
    assert not [c for c in fake.log if c.startswith("G0 Z")], "must not command a move"


def test_retract_z_stops_at_the_top_switch(monkeypatch):
    """The switch is the real "fully up" stop; stop there rather than counting to the bound."""
    fake = FakeBoard(pos={"X": 0.0, "Y": 0.0, "Z": 0.0}, switch_at={"Z": -5.0})
    d = _driver(fake, monkeypatch)
    result = d.retract_z(30.0)

    assert result["at_top"] is True
    assert result["retracted_mm"] < 30.0, "stopped early at the switch"
    assert fake.triggered >= {"min_z"}
    # Reaching an endstop mid-move latches a halt; for a retract that is success, so it is
    # cleared rather than surfaced as an error.
    assert fake.halted is False


def test_initialize_retract_is_off_by_default_and_opt_in(monkeypatch):
    fake = FakeBoard()
    d = _driver(fake, monkeypatch)
    report = d.initialize()
    assert "retract" not in report
    # Z's only moves are the wiggle, which returns to start.
    assert pytest.approx(fake.pos["Z"], abs=1e-6) == 0.0

    fake2 = FakeBoard()
    monkeypatch.setattr(ot, "open_transport", lambda port, baud=ot.BAUD: fake2)
    d2 = ot.OpentronsDriver("ot", {"port": "/dev/fake", "retract_z_on_init": True,
                                   "retract_z_mm": 10.0})
    d2.connect()
    report2 = d2.initialize()
    assert report2["retract"]["retracted_mm"] == 10.0
    assert pytest.approx(fake2.pos["Z"], abs=1e-6) == -10.0, "ends retracted, i.e. up"


# ---------------------------------------------------------------------------
# R-LH-4: position provenance
# ---------------------------------------------------------------------------


def test_position_states_that_it_is_dead_reckoned_and_unreferenced(monkeypatch):
    """The numbers must not be readable as measurements: no encoders, never homed."""
    fake = FakeBoard()
    d = _driver(fake, monkeypatch)
    prov = d.position()["provenance"]
    assert prov["encoders"] is False
    assert prov["referenced"] is False
    assert prov["source"] == "controller_step_counts"
    assert d.status()["provenance"] == prov, "provenance travels with the status numbers"


def test_position_separates_commanded_from_actual_and_reports_the_offset(monkeypatch):
    """The divergence is the diagnostic for a move that was cut short."""
    fake = FakeBoard()
    d = _driver(fake, monkeypatch)

    def skewed(line, wait):
        if line == "M114":
            return "ok C: X:0.000 Y:0.000 Z:2.000 x:0.000 y:0.000 z:0.006"
        return FakeBoard.converse(fake, line, wait)

    fake_converse, fake.converse = fake.converse, skewed
    d._refresh(force=True)
    pos = d.position()
    assert pos["commanded"]["Z"] == 2.0
    assert pos["actual"]["Z"] == 0.006
    assert pos["offset_mm"]["Z"] == 1.994


def test_position_keeps_commanded_and_actual_apart(monkeypatch):
    """The upper/lower split is the diagnostic for a move that was cut short."""
    fake = FakeBoard()
    d = _driver(fake, monkeypatch)
    fake.pos["Z"] = 2.0
    d._refresh(force=True)
    pos = d.status()["position"]
    assert pos["Z"] == 2.0 and pos["z"] == 2.0
