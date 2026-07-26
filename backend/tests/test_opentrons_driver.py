"""OT-One driver: the two rules that wedge the real machine, enforced without hardware.

Both failures behind these tests happened on the bench:

* A homing command (`G28.2`) blocked the firmware's main loop. Because Smoothieware reads
  serial from that same loop, every later command went unanswered — and was *queued*, not
  rejected, so a batch of moves ran later unsupervised. Hence: silence is fatal, and
  nothing may be sent after it.
* A move toward an already-triggered endstop latched a limit halt, after which the board
  refused everything. Hence: direction comes from `M119`, never from an assumption.

The board simulator these tests run against is `drivers.opentrons.transport.LoopbackTransport` —
**shipping code, not a test double** (D6/R-LH-5). That is deliberate: it records every line
written, in order, so a test can assert the exact wire conversation, which is the only way to
catch the driver's original defect (`connect()` assigned a dummy object and `_send()` returned
`None`, so every call reported success while doing nothing) without an instrument. No test can
reach real hardware (R-SIM-7).
"""
from __future__ import annotations

import pytest

from drivers.base import ConnectionState, DriverError
from drivers.capabilities.liquid_handler import RelativeMoveReport
from drivers.opentrons import driver as ot
from drivers.opentrons.transport import LoopbackTransport, NullTransport


def _driver(fake, monkeypatch, **config):
    """A connected driver talking to `fake` instead of a serial port."""
    monkeypatch.setattr(ot, "open_transport", lambda port, baud=ot.BAUD: fake)
    d = ot.OpentronsDriver("ot", {"port": "/dev/fake", **config})
    d.connect()
    return d


def test_connect_reads_firmware_and_state(monkeypatch):
    fake = LoopbackTransport()
    d = _driver(fake, monkeypatch)
    assert d.state is ConnectionState.CONNECTED
    status = d.status()
    assert "v1.0.3" in status["firmware"]
    assert status["endstops"] == {f"min_{a}": False for a in ("x", "y", "z", "a", "b")}
    assert status["homed"] is False


def test_connect_fails_when_board_opens_but_never_answers(monkeypatch):
    """Opening the port is not evidence of a working machine — this is what wedged looks like."""
    fake = LoopbackTransport()
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
    fake = LoopbackTransport(silent_after="G0 X+3 F600")
    d = _driver(fake, monkeypatch)
    with pytest.raises(DriverError, match="no reply"):
        d.initialize()
    after_silence = fake.lines[fake.lines.index("G0 X+3 F600") + 1:]
    # Only the recovery attempt may follow, and it must not be a move.
    assert not [c for c in after_silence if c.startswith("G0 ")], (
        f"commands were sent into the silence: {after_silence}")


def test_initialize_moves_each_axis_both_ways_and_returns(monkeypatch):
    """R-LH-2's first half: every axis, both directions, net zero.

    The retract is opted out of here so this is a test about the *wiggle* only; that it happens by
    default is `test_initialize_ends_with_z_retracted` below.
    """
    fake = LoopbackTransport()
    d = _driver(fake, monkeypatch, retract_z_on_init=False)
    report = d.initialize()
    for axis in ("X", "Y", "Z"):
        assert report["axes"][axis]["net_mm"] == 0.0
        assert pytest.approx(fake.position[axis], abs=1e-6) == 0.0
        moves = [c for c in fake.lines if c.startswith(f"G0 {axis}")]
        assert any("+" in m for m in moves), f"{axis} never moved positive"
        assert any("-" in m for m in moves), f"{axis} never moved negative"


def test_axis_clear_of_switch_but_close_to_it_moves_away_first(monkeypatch):
    """The exact state the bench was left in: Z open, but only ~1 mm from min_z.

    The step is 2 mm, so a leading toward-the-switch move re-latches the halt. This is a
    regression test for that bug, which this driver had until the move order was flipped.
    """
    fake = LoopbackTransport(position={"X": 0.0, "Y": 0.0, "Z": 1.0}, switch_at={"Z": 0.0})
    d = _driver(fake, monkeypatch, retract_z_on_init=False)
    report = d.initialize()

    first_z = next(c for c in fake.lines if c.startswith("G0 Z"))
    assert first_z.startswith("G0 Z+"), (
        f"must move away from a nearby switch first, sent {first_z}")
    assert not fake.halted, "drove into min_z despite it being only 1 mm away"
    assert report["axes"]["Z"]["net_mm"] == 0.0
    assert pytest.approx(fake.position["Z"], abs=1e-6) == 1.0, "must end where it started"


def test_initialize_never_touches_the_plungers(monkeypatch):
    """A and B are out of scope: driving a plunger into its stop is what started all this."""
    fake = LoopbackTransport(triggered={"min_z"})
    d = _driver(fake, monkeypatch)
    d.initialize()
    assert not [c for c in fake.lines if c.startswith("G0 A") or c.startswith("G0 B")]


def test_axis_on_endstop_moves_away_not_into_it(monkeypatch):
    """Rule 2, the exact bench failure: Z parked on min_z, and + moves toward it."""
    fake = LoopbackTransport(triggered={"min_z"})
    d = _driver(fake, monkeypatch, retract_z_on_init=False)
    report = d.initialize()

    first_z = next(c for c in fake.lines if c.startswith("G0 Z"))
    assert first_z.startswith("G0 Z+"), f"first Z move must be away from min_z, got {first_z}"
    assert report["axes"]["Z"]["started_on_endstop"] is True
    assert report["axes"]["Z"]["released"] is True
    assert not fake.halted, "must not latch a limit halt"
    # Left off the switch, not parked back on it: resting on a triggered limit is what
    # makes the NEXT move fail.
    assert report["endstops"]["min_z"] is False
    assert fake.position["Z"] > 0, "released downward, away from the top switch"


def test_wrong_assumed_direction_is_detected_not_repeated(monkeypatch):
    """If a release move does not release the switch, stop — do not push further.

    Simulates an axis whose polarity is opposite to AWAY_FROM_MIN by refusing to release.
    """
    fake = LoopbackTransport(triggered={"min_x"}, latch_on_touch=False)
    fake.away["X"] = -1.0          # driver assumes +1 for X; fake says otherwise

    def stuck_move(line):
        body = line.split()[1]
        axis, delta = body[0], float(body[1:])
        fake.position[axis] += delta
        return "ok"                # switch stays triggered

    fake._move = stuck_move        # type: ignore[method-assign]
    d = _driver(fake, monkeypatch)
    with pytest.raises(DriverError, match="still on min_x"):
        d.initialize()
    x_moves = [c for c in fake.lines if c.startswith("G0 X")]
    assert len(x_moves) == 1, f"must stop after the first failed release, sent {x_moves}"


def test_limit_halt_is_reported_and_not_left_latched(monkeypatch):
    """A latched halt must surface as an error, and be cleared so the next caller is not stuck.

    Clearing is for hygiene only — the error still propagates. Pushing through a protective
    halt is precisely what must not happen.
    """
    fake = LoopbackTransport()

    def halting_move(line):
        fake.halted = True
        return "Limit switch min_y was hit - reset or M999 required"

    fake._move = halting_move      # type: ignore[method-assign]
    d = _driver(fake, monkeypatch)
    with pytest.raises(DriverError, match="firmware fault"):
        d.initialize()
    assert "M999" in fake.lines, "must not leave the board latched"
    assert fake.halted is False


def test_relative_mode_is_always_restored(monkeypatch):
    """Leaving the board in G91 would reinterpret a later absolute move as relative."""
    fake = LoopbackTransport()
    d = _driver(fake, monkeypatch)
    d.initialize()
    assert fake.lines.index("G91") < fake.lines.index("G90")
    assert fake.relative is False


def test_homing_refuses_rather_than_wedging(monkeypatch):
    fake = LoopbackTransport()
    d = _driver(fake, monkeypatch)
    with pytest.raises(DriverError, match="homing is not implemented"):
        d.home()
    assert not [c for c in fake.lines if "G28" in c], "no homing code may reach the board"


def test_unimplemented_operations_fail_loudly(monkeypatch):
    """A stub that silently succeeds is worse than one that raises — the original bug here."""
    fake = LoopbackTransport()
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
    fake = LoopbackTransport()
    d = _driver(fake, monkeypatch)
    result = d.move_by(dx=2.0, dz=-1.5)

    assert result["moved"] is True
    assert result["achieved"] == {"X": 2.0, "Z": -1.5}
    assert not result["drift_mm"]
    moves = [c for c in fake.lines if c.startswith("G0 ")]
    assert moves == ["G0 X+2 F600", "G0 Z-1.5 F600"]
    assert "G91" in fake.lines and fake.relative is False, "must end back in absolute mode"


def test_move_by_zero_is_a_no_op(monkeypatch):
    fake = LoopbackTransport()
    d = _driver(fake, monkeypatch)
    assert d.move_by()["moved"] is False
    assert not [c for c in fake.lines if c.startswith("G0 ")]


def test_move_by_reports_shortfall_when_an_axis_does_not_follow(monkeypatch):
    """A commanded move the axis never made would make every later offset a lie."""
    fake = LoopbackTransport()
    d = _driver(fake, monkeypatch)
    fake._move = lambda line: "ok"          # accepts, moves nothing
    result = d.move_by(dx=5.0)
    assert result["drift_mm"] == {"X": -5.0}


# ---------------------------------------------------------------------------
# R-LH-3: refuse, never silently clamp
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kwargs", [{"dx": 999.0}, {"dy": -80.0}, {"dz": 31.0}])
def test_move_by_refuses_beyond_the_single_move_limit(monkeypatch, kwargs):
    fake = LoopbackTransport()
    d = _driver(fake, monkeypatch)
    with pytest.raises(DriverError, match="refusing rather than clamping"):
        d.move_by(**kwargs)
    assert not [c for c in fake.lines if c.startswith("G0 ")], "must not move at all"


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_move_by_refuses_non_finite(monkeypatch, bad):
    fake = LoopbackTransport()
    d = _driver(fake, monkeypatch)
    with pytest.raises(DriverError, match="finite"):
        d.move_by(dx=bad)


def test_move_by_refuses_outside_a_configured_envelope(monkeypatch):
    fake = LoopbackTransport(position={"X": 8.0, "Y": 0.0, "Z": 0.0})
    monkeypatch.setattr(ot, "open_transport", lambda port, baud=ot.BAUD: fake)
    d = ot.OpentronsDriver("ot", {"port": "/dev/fake", "envelope": {"X": [-10.0, 10.0]}})
    d.connect()
    with pytest.raises(DriverError, match="outside the configured envelope"):
        d.move_by(dx=5.0)                    # 8 + 5 = 13, past the +10 bound
    assert not [c for c in fake.lines if c.startswith("G0 ")]
    d.move_by(dx=1.0)                        # 9 is inside, still allowed


def test_move_by_refuses_to_drive_into_a_triggered_endstop(monkeypatch):
    """The bench failure, now blocked at the interface instead of by the firmware."""
    fake = LoopbackTransport(triggered={"min_z"})
    d = _driver(fake, monkeypatch)
    with pytest.raises(DriverError, match="moves toward it"):
        d.move_by(dz=-2.0)                   # -Z is up, toward the top switch
    assert not fake.halted
    d.move_by(dz=+2.0)                       # down, away from it, is fine


# ---------------------------------------------------------------------------
# R-LH-2 / R-VIS-11: bounded retract
# ---------------------------------------------------------------------------


def test_retract_z_moves_up_in_bounded_steps(monkeypatch):
    fake = LoopbackTransport()
    d = _driver(fake, monkeypatch)
    result = d.retract_z(12.0)

    assert result["retracted_mm"] == 12.0
    # Up is -Z: measured by camera (Z+ lowers the head) and by endstop (Z+ releases min_z).
    z_moves = [c for c in fake.lines if c.startswith("G0 Z")]
    assert all("Z-" in m for m in z_moves), f"retract must move up, sent {z_moves}"
    assert len(z_moves) == 6, "12 mm in 2 mm steps"
    assert pytest.approx(fake.position["Z"], abs=1e-6) == -12.0
    assert result["at_top"] is False, "never reached the switch, so cannot claim to be up"


def test_retract_z_refuses_an_unbounded_request(monkeypatch):
    """The bound is the backstop for a switch that never reports."""
    fake = LoopbackTransport()
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
    fake = LoopbackTransport(triggered={"min_z"})
    d = _driver(fake, monkeypatch)
    result = d.retract_z(10.0)

    assert result["already_there"] is True
    assert result["at_top"] is True
    assert result["retracted_mm"] == 0.0
    assert not [c for c in fake.lines if c.startswith("G0 Z")], "must not command a move"


def test_retract_z_stops_at_the_top_switch(monkeypatch):
    """The switch is the real "fully up" stop; stop there rather than counting to the bound."""
    fake = LoopbackTransport(position={"X": 0.0, "Y": 0.0, "Z": 0.0}, switch_at={"Z": -5.0})
    d = _driver(fake, monkeypatch)
    result = d.retract_z(30.0)

    assert result["at_top"] is True
    assert result["retracted_mm"] < 30.0, "stopped early at the switch"
    assert fake.triggered >= {"min_z"}
    # Reaching an endstop mid-move latches a halt; for a retract that is success, so it is
    # cleared rather than surfaced as an error.
    assert fake.halted is False


def test_initialize_ends_with_z_retracted(monkeypatch):
    """R-LH-2's second half, and it is on by default.

    It was off for a while, on the reasoning that a retract had nothing to stop against — which
    rested on the inverted Z convention this driver has since settled. `min_z` is the TOP switch,
    so a retract stops on a real hard stop, and finishing with the head clear of the deck is the
    safe end state R-LH-2 asks for.
    """
    fake = LoopbackTransport()
    d = _driver(fake, monkeypatch)
    report = d.initialize()

    retracted = report["retract"]["retracted_mm"]
    assert retracted > 0
    # Up is -Z on this machine, so a retracted head sits at a NEGATIVE controller Z, and the
    # wiggle is net zero — therefore the whole displacement is the retract, upwards. A positive Z
    # here would mean initialize just drove the pipette down into the deck.
    assert pytest.approx(fake.position["Z"], abs=1e-6) == -retracted
    retract_leg = fake.lines[len(fake.lines) - 1 - fake.lines[::-1].index("G91"):]
    assert all("Z-" in m for m in retract_leg if m.startswith("G0 ")), \
        f"the retract may only move up, sent {retract_leg}"


def test_initialize_retract_can_be_opted_out_of(monkeypatch):
    fake = LoopbackTransport()
    d = _driver(fake, monkeypatch, retract_z_on_init=False)
    report = d.initialize()
    assert "retract" not in report
    # Z's only moves are the wiggle, which returns to start.
    assert pytest.approx(fake.position["Z"], abs=1e-6) == 0.0

    fake2 = LoopbackTransport()
    monkeypatch.setattr(ot, "open_transport", lambda port, baud=ot.BAUD: fake2)
    d2 = ot.OpentronsDriver("ot", {"port": "/dev/fake", "retract_z_mm": 10.0})
    d2.connect()
    report2 = d2.initialize()
    assert report2["retract"]["retracted_mm"] == 10.0
    assert pytest.approx(fake2.position["Z"], abs=1e-6) == -10.0, "ends retracted, i.e. up"


# ---------------------------------------------------------------------------
# R-LH-4: position provenance
# ---------------------------------------------------------------------------


def test_position_states_that_it_is_dead_reckoned_and_unreferenced(monkeypatch):
    """The numbers must not be readable as measurements: no encoders, never homed."""
    fake = LoopbackTransport()
    d = _driver(fake, monkeypatch)
    prov = d.position()["provenance"]
    assert prov["encoders"] is False
    assert prov["referenced"] is False
    assert prov["source"] == "controller_step_counts"
    assert d.status()["provenance"] == prov, "provenance travels with the status numbers"


def test_position_separates_commanded_from_actual_and_reports_the_offset(monkeypatch):
    """The divergence is the diagnostic for a move that was cut short."""
    fake = LoopbackTransport()
    d = _driver(fake, monkeypatch)

    def skewed(line, wait):
        if line == "M114":
            return "ok C: X:0.000 Y:0.000 Z:2.000 x:0.000 y:0.000 z:0.006"
        return LoopbackTransport.converse(fake, line, wait)

    fake_converse, fake.converse = fake.converse, skewed
    d._refresh(force=True)
    pos = d.position()
    assert pos["commanded"]["Z"] == 2.0
    assert pos["actual"]["Z"] == 0.006
    assert pos["offset_mm"]["Z"] == 1.994


def test_position_keeps_commanded_and_actual_apart(monkeypatch):
    """The upper/lower split is the diagnostic for a move that was cut short."""
    fake = LoopbackTransport()
    d = _driver(fake, monkeypatch)
    fake.position["Z"] = 2.0
    d._refresh(force=True)
    pos = d.status()["position"]
    assert pos["Z"] == 2.0 and pos["z"] == 2.0


# ---------------------------------------------------------------------------
# D6 / R-LH-5: the transport split, and the exact wire lines
# ---------------------------------------------------------------------------


def test_the_exact_wire_conversation_for_a_relative_move(monkeypatch):
    """R-LH-5's acceptance criterion: assert every line, in order, including the mode framing.

    Not just the `G0`s. The framing is half the correctness: a move issued without a preceding
    `G91` is executed as an *absolute* target on an unhomed machine, which is a full-travel move
    to a coordinate nobody chose, and the reply is an indistinguishable `ok`. And leaving the
    board in `G91` afterwards would silently reinterpret the next absolute move as a relative one.
    """
    fake = LoopbackTransport()
    d = _driver(fake, monkeypatch)
    fake.lines.clear()                       # drop connect's version/M119/M114 handshake

    d.move_relative(dx=2.0, dz=1.5)          # task frame: +z is UP

    fake.assert_wire([
        "M119", "M114",                      # read state before deciding anything
        "G91", "G0 X+2 F600", "M400",        # relative mode, then one axis at a time,
        "G91", "G0 Z-1.5 F600", "M400",      # ... with Z inverted: task +1.5 up is wire -1.5
        "G90",                               # absolute mode restored
        "M119", "M114",                      # read back what actually happened
    ])


def test_a_null_transport_makes_every_call_fail_loudly(monkeypatch):
    """The defect this whole split exists for, made unrepresentable.

    `connect()` used to assign a dummy object whose calls returned `None`, so every liquid-handling
    call reported success while doing nothing. A transport that answers nothing must instead make
    every call raise — including `connect` itself, which is the earliest possible point to notice.
    """
    monkeypatch.setattr(ot, "open_transport", lambda port, baud=ot.BAUD: NullTransport())
    d = ot.OpentronsDriver("ot", {"transport": "null"})
    with pytest.raises(DriverError, match="did not answer"):
        d.connect()
    assert d.state is ConnectionState.ERROR

    # And a driver forced past connect still cannot pretend: nothing is silently accepted.
    d._io = NullTransport()
    for call in (lambda: d.move_relative(dz=1.0), lambda: d.retract_z(5.0),
                 lambda: d.initialize()):
        with pytest.raises(DriverError, match="no reply"):
            call()


def test_a_loopback_fleet_entry_needs_no_port():
    """`{"transport": "loopback"}` brings the driver up with no instrument and no invented tty."""
    d = ot.OpentronsDriver("ot", {"transport": "loopback"})
    d.connect()
    assert d.state is ConnectionState.CONNECTED
    assert isinstance(d._io, LoopbackTransport)
    report = d.move_relative(dz=+3.0)
    assert isinstance(report, RelativeMoveReport)
    assert report.moved is True
    d.disconnect()


def test_an_unknown_transport_is_refused_rather_than_defaulted():
    """Defaulting an unrecognised name to serial would open a real port by accident."""
    d = ot.OpentronsDriver("ot", {"transport": "lopback"})
    with pytest.raises(DriverError, match="unknown liquid-handler transport"):
        d.connect()


# ---------------------------------------------------------------------------
# The Z sign convention. A sign error here drives the pipette into the deck.
# ---------------------------------------------------------------------------


def test_the_z_constants_agree_with_each_other():
    """The inversion this driver had, caught by one assertion.

    Two measurements, both from the bench, both saying the same thing:

    * camera — `G0 Z+6` moved the head DOWN in frame, `G0 Z-6` moved it back UP;
    * endstop — with `min_z` triggered, `G0 Z+5` RELEASED it.

    Together: up is `-Z`, and `min_z` lies at the TOP of travel, so *away from min_z is down*.
    Which makes `AWAY_FROM_MIN["Z"]` the exact negation of `Z_UP_SIGN`. An earlier version of this
    driver had them equal — "up is away from min_z" — which is self-contradictory, and it produced
    both observed symptoms: `initialize` sent `G0 Z-2` into a triggered `min_z` and latched a halt,
    and `retract_z` moved *away* from the only switch that could stop it.
    """
    assert ot.AWAY_FROM_MIN["Z"] == -ot.Z_UP_SIGN
    assert ot.Z_UP_SIGN == -1.0, "up is -Z: measured twice, by camera and by endstop"


def test_commanding_up_moves_up_and_never_into_the_deck(monkeypatch):
    """The pinning test. Task-frame `+dz` must emit a NEGATIVE wire Z, on every path.

    This is the assertion that would have caught the inversion, and the one to run first if the
    pipette ever moves the wrong way: reversed, the plan's closing `dz=+40` retract becomes a
    40 mm descent into whatever is under the head, reported as a successful retract.
    """
    fake = LoopbackTransport()
    d = _driver(fake, monkeypatch, retract_z_on_init=False)

    d.move_relative(dz=+5.0)
    assert [m for m in fake.moves() if m.startswith("G0 Z")] == ["G0 Z-5 F600"]
    assert fake.position["Z"] == pytest.approx(-5.0)

    d.move_relative(dz=-2.0)                 # task-frame down
    assert fake.moves()[-1] == "G0 Z+2 F600"
    assert fake.position["Z"] == pytest.approx(-3.0)


def test_a_commanded_up_reports_up_in_the_task_frame(monkeypatch):
    """The report is task frame too, or the tab shows a retract as a descent.

    `applied_mm` and `position_after` must have the same sign as what was asked for. If they
    carried the controller's frame while `requested_mm` carried the task frame, every retract
    would render as "requested +40, applied -40" — which reads as a catastrophic axis inversion
    and is in fact only a units bug in the report.
    """
    fake = LoopbackTransport()
    d = _driver(fake, monkeypatch, retract_z_on_init=False)

    report = d.move_relative(dz=+4.0)

    assert report.frame == "task"
    assert report.applied_mm["z"] == pytest.approx(+4.0)
    assert report.position_before["z"] == pytest.approx(0.0)
    assert report.position_after["z"] == pytest.approx(+4.0)
    assert not report.drift_mm
    assert report.provenance == "dead_reckoned", "no encoders, never homed (R-LH-4)"


def test_retract_up_is_refused_when_the_top_switch_is_already_hit(monkeypatch):
    """`min_z` is the TOP stop, so "up" is the direction that must be refused when it is hit.

    The counter-intuitive half of the convention, and the reason it is asserted explicitly: on most
    machines a triggered `min_z` blocks *downward* motion. Here it blocks upward, and going the
    other way — down, away from it — is what releases it.
    """
    fake = LoopbackTransport(triggered={"min_z"})
    d = _driver(fake, monkeypatch, retract_z_on_init=False)

    with pytest.raises(DriverError, match="moves toward it"):
        d.move_relative(dz=+2.0)             # up, into the top switch
    assert not fake.halted, "refused at the interface, never latched by the firmware"

    d.move_relative(dz=-2.0)                 # down, away from it
    assert fake.moves() == ["G0 Z+2 F600"]


# ---------------------------------------------------------------------------
# R-LH-3 via the capability: refuse, state the reason, never clamp
# ---------------------------------------------------------------------------


def test_move_limits_bound_a_magnitude_and_state_no_envelope(monkeypatch):
    """`max_step_mm` is frame-agnostic; a converted envelope would not be, so none is published."""
    fake = LoopbackTransport()
    d = _driver(fake, monkeypatch)
    limits = d.move_limits()

    assert limits.max_step_mm == {"x": 50.0, "y": 50.0, "z": 30.0}
    assert limits.envelope_mm == {}, (
        "a converted Z envelope flips end for end; it is enforced in the controller's own frame")
    assert limits.refusal_for("z", 40.0) and "refusing rather than clamping" in \
        limits.refusal_for("z", 40.0)
    assert limits.refusal_for("z", 20.0) is None


def test_a_task_frame_move_past_the_step_limit_is_refused_not_clamped(monkeypatch):
    fake = LoopbackTransport()
    d = _driver(fake, monkeypatch, retract_z_on_init=False)
    fake.lines.clear()

    with pytest.raises(DriverError, match="refusing rather than clamping"):
        d.move_relative(dz=+45.0)
    assert not fake.moves(), "a refusal must not be a partial move"


def test_a_stalled_axis_is_reported_as_drift_in_the_task_frame(monkeypatch):
    """No encoders, so a stalled axis is invisible to the counter — except as a shortfall.

    The loopback's `stalled_axes` reproduces the measured bench case: a 10 mm retract advanced the
    step counter 10 mm while the camera saw ~2 mm and the motor skipped the rest.
    """
    fake = LoopbackTransport(stalled_axes={"Z"})
    d = _driver(fake, monkeypatch, retract_z_on_init=False)

    report = d.move_relative(dz=+6.0)

    assert report.applied_mm["z"] == pytest.approx(0.0)
    assert report.drift_mm["z"] == pytest.approx(-6.0), "reported in the task frame, signed"
